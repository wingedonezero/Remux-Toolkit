"""
Chapter extraction for a DVD title.

A title's chapter list lives in the PGC's program_map (an array mapping
1-based program number → 1-based starting cell). Combined with cell_playback
durations, we get accurate chapter start/end timestamps that match what
FFmpeg's dvdvideo demuxer and MakeMKV produce.

This module is self-contained and pure: given a `dvd_reader_t*` and a title
number, it returns a list of chapter dicts. No side effects, no file I/O
beyond what libdvdread does to read the IFO.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import Optional

from ...bindings import libdvdread as dr
from .cell_reader import _resolve_title_to_pgc


@dataclass(frozen=True)
class Chapter:
    index: int             # 1-based
    start_seconds: float   # cumulative within the title
    end_seconds: float
    start_cell: int        # 1-based cell that this chapter starts at
    title: str             # e.g. "Chapter 01"

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds

    def to_dict(self) -> dict:
        return {
            "index":          self.index,
            "start_seconds":  round(self.start_seconds, 3),
            "end_seconds":    round(self.end_seconds, 3),
            "duration_seconds": round(self.duration_seconds, 3),
            "start_cell":     self.start_cell,
            "title":          self.title,
        }


def extract_chapters(disc, title_num: int,
                     *, vts_no: Optional[int] = None,
                     pgc_no: Optional[int] = None) -> list[Chapter]:
    """Pull the chapter table for a title. Returns chapters in order (1..N).

    If `vts_no` and `pgc_no` are supplied, skips the VMG lookup (useful when
    the caller already resolved them via the analyzer).
    """
    if vts_no is None or pgc_no is None:
        vts_no, pgc_no = _resolve_title_to_pgc(disc, title_num)

    with dr.open_ifo(disc, vts_no) as vts:
        pgcit = vts.contents.vts_pgcit.contents
        if pgc_no < 1 or pgc_no > pgcit.nr_of_pgci_srp:
            return []
        pgc = pgcit.pgci_srp[pgc_no - 1].pgc.contents

        # program_map is a pointer to an array of uint8 (cell numbers, 1-based).
        # libdvdread's struct declares it as `pgc_program_map_t *` which is
        # `uint8 *`. Our binding types it as void_p so we cast here.
        nr_progs = int(pgc.nr_of_programs)
        nr_cells = int(pgc.nr_of_cells)
        if nr_progs == 0 or nr_cells == 0:
            return []
        if not pgc.program_map:
            return []
        prog_map_arr = ctypes.cast(pgc.program_map, ctypes.POINTER(ctypes.c_uint8))
        program_starts_at_cell = [int(prog_map_arr[i]) for i in range(nr_progs)]

        # Cumulative cell-start times, in seconds.
        cell_start_times: list[float] = [0.0]
        for i in range(nr_cells):
            cp = pgc.cell_playback[i]
            cell_start_times.append(cell_start_times[-1] + cp.playback_time.total_seconds)

        # Build chapters.
        chapters: list[Chapter] = []
        for i, start_cell in enumerate(program_starts_at_cell):
            if start_cell < 1 or start_cell > nr_cells:
                # Malformed program_map; skip gracefully.
                continue
            start_s = cell_start_times[start_cell - 1]
            if i + 1 < nr_progs:
                next_cell = program_starts_at_cell[i + 1]
                end_s = (cell_start_times[next_cell - 1]
                         if 1 <= next_cell <= nr_cells
                         else cell_start_times[-1])
            else:
                end_s = cell_start_times[-1]
            chapters.append(Chapter(
                index=i + 1,
                start_seconds=start_s,
                end_seconds=end_s,
                start_cell=start_cell,
                title=f"Chapter {i + 1:02d}",
            ))
        return chapters


def chapters_to_ffmetadata(chapters: list[Chapter]) -> str:
    """Serialize a chapter list to the FFmetadata1 format ffmpeg consumes:

        ;FFMETADATA1
        [CHAPTER]
        TIMEBASE=1/1000
        START=<ms>
        END=<ms>
        title=<name>
    """
    lines = [";FFMETADATA1", ""]
    for ch in chapters:
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={int(round(ch.start_seconds * 1000))}")
        lines.append(f"END={int(round(ch.end_seconds * 1000))}")
        # FFmetadata escapes for `\\` `\n` `=` `;` `#` — chapter titles are
        # plain "Chapter NN" so escaping is not needed in our generated form.
        lines.append(f"title={ch.title}")
        lines.append("")
    return "\n".join(lines)
