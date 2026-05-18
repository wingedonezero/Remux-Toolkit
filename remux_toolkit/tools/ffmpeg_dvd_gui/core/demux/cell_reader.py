"""
Stream DVD VOB sectors for a title's PGC cells via libdvdread.

A DVD title is composed of one or more *cells*; each cell is a contiguous run
of 2048-byte sectors inside a VOB file. libdvdread handles UDF + CSS
decryption transparently — we just hand it (vts_number, domain=TITLE_VOBS,
offset, count) and get back decrypted sector blocks.

This module exposes the cells of a title as a stream of (cell_index, sector)
tuples so downstream consumers (MPEG-PS walker, diagnostics) can react to
cell boundaries without re-querying the IFO.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from ...bindings import libdvdread as dr


DVD_VIDEO_LB_LEN = dr.DVD_VIDEO_LB_LEN  # 2048


@dataclass(frozen=True)
class Cell:
    index: int          # 1-based cell number within the PGC
    first_sector: int   # inclusive
    last_sector: int    # inclusive
    block_type: int     # 0=normal, 1=angle block
    block_mode: int     # 0=not in block, 1=first, 2=in, 3=last
    seamless_play: bool
    stc_discontinuity: bool
    still_time: int

    @property
    def num_sectors(self) -> int:
        return self.last_sector - self.first_sector + 1


def _cells_for_pgc(vts_ifo, pgc_index: int) -> list[Cell]:
    """Pull the cell list for a specific PGC out of an open VTS IFO handle.
    pgc_index is 1-based (matches libdvdread/MakeMKV convention)."""
    pgcit = vts_ifo.contents.vts_pgcit.contents
    if pgc_index < 1 or pgc_index > pgcit.nr_of_pgci_srp:
        raise ValueError(
            f"pgc_index {pgc_index} out of range (1..{pgcit.nr_of_pgci_srp})"
        )
    pgc = pgcit.pgci_srp[pgc_index - 1].pgc.contents
    out: list[Cell] = []
    for i in range(pgc.nr_of_cells):
        cp = pgc.cell_playback[i]
        out.append(Cell(
            index=i + 1,
            first_sector=int(cp.first_sector),
            last_sector=int(cp.last_sector),
            block_type=int(cp.block_type),
            block_mode=int(cp.block_mode),
            seamless_play=bool(cp.seamless_play),
            stc_discontinuity=bool(cp.stc_discontinuity),
            still_time=int(cp.still_time),
        ))
    return out


def _resolve_title_to_pgc(disc, title_num: int) -> tuple[int, int]:
    """Given a libdvdread `dvd_reader_t*` and a libdvdread global title number,
    resolve to (vts_number, pgc_index_within_vts) via VMG.tt_srpt and the
    VTS_PTT_SRPT first-PTT entry. Returns (vts_no, pgc_no)."""
    with dr.open_ifo(disc, 0) as vmg:
        tt = vmg.contents.tt_srpt.contents
        if title_num < 1 or title_num > tt.nr_of_srpts:
            raise ValueError(
                f"title_num {title_num} out of range (1..{tt.nr_of_srpts})"
            )
        title_info = tt.title[title_num - 1]
        vts_no = int(title_info.title_set_nr)
        vts_ttn = int(title_info.vts_ttn)

    with dr.open_ifo(disc, vts_no) as vts:
        ptt = vts.contents.vts_ptt_srpt.contents
        ttu = ptt.title[vts_ttn - 1]
        pgc_no = int(ttu.ptt[0].pgcn)
    return vts_no, pgc_no


class CellReader:
    """Iterate sectors for a title's cells via libdvdread.

    Typical use:

        with dr.open_disc(path) as disc:
            with CellReader(disc, title_num=2) as r:
                for cell, sector in r.iter_sectors():
                    ...

    The reader opens the title's VTS VOBs once and reads sectors in big
    batches (default 64 = 128 KiB), yielding them one at a time."""

    def __init__(self, disc, title_num: int, *,
                 vts_no: Optional[int] = None,
                 pgc_no: Optional[int] = None,
                 batch_sectors: int = 64,
                 cell_filter: Optional[set[int]] = None):
        """``cell_filter`` is an optional set of 1-based cell indices to
        INCLUDE in iteration. When None (the default), every cell in the
        PGC is read. When provided, cells whose ``index`` is not in the
        set are silently skipped — this is the integration point for
        the analyzer's trim decisions.
        """
        self.disc = disc
        self.title_num = title_num
        self.batch_sectors = batch_sectors
        # Allow caller to override the (vts, pgc) resolution if they already
        # have it from the analyzer; otherwise resolve from IFO.
        if vts_no is None or pgc_no is None:
            self.vts_no, self.pgc_no = _resolve_title_to_pgc(disc, title_num)
        else:
            self.vts_no, self.pgc_no = vts_no, pgc_no

        with dr.open_ifo(disc, self.vts_no) as vts:
            all_cells = _cells_for_pgc(vts, self.pgc_no)
        if cell_filter is None:
            self.cells = all_cells
        else:
            self.cells = [c for c in all_cells if c.index in cell_filter]

        self._vob: Optional[dr.DvdFileP] = None

    def __enter__(self) -> "CellReader":
        self._vob = dr._lib.DVDOpenFile(
            self.disc, self.vts_no, dr.DvdReadDomain.TITLE_VOBS
        )
        if not self._vob:
            raise dr.DvdReadError(
                f"DVDOpenFile failed for VTS {self.vts_no} TITLE_VOBS"
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._vob is not None:
            dr._lib.DVDCloseFile(self._vob)
            self._vob = None

    def iter_sectors(self) -> Iterator[tuple[Cell, bytes]]:
        """Yield (cell, sector_bytes) for every 2048-byte sector in this PGC,
        in playback order. Sector bytes are decrypted by libdvdread if the
        disc was CSS-encrypted."""
        if self._vob is None:
            raise RuntimeError("CellReader must be used as a context manager")

        for cell in self.cells:
            remaining = cell.num_sectors
            offset = cell.first_sector
            while remaining > 0:
                want = min(self.batch_sectors, remaining)
                got = dr.read_blocks(self._vob, offset, want)
                if not got:
                    # libdvdread occasionally short-reads near bad sectors;
                    # advance by one sector and report a zero-filled gap so
                    # the caller can record it.
                    yield (cell, b"\x00" * DVD_VIDEO_LB_LEN)
                    offset += 1
                    remaining -= 1
                    continue
                blocks_got = len(got) // DVD_VIDEO_LB_LEN
                for i in range(blocks_got):
                    yield (cell, got[i * DVD_VIDEO_LB_LEN: (i + 1) * DVD_VIDEO_LB_LEN])
                offset += blocks_got
                remaining -= blocks_got


@contextmanager
def open_title(disc, title_num: int, **kwargs) -> Iterator[CellReader]:
    """Sugar: `with open_title(disc, 2) as r: ...`"""
    with CellReader(disc, title_num, **kwargs) as r:
        yield r
