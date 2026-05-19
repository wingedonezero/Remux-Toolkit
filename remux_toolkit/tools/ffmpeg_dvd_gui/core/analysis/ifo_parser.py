"""
Pure-Python parser for DVD-Video IFO / BUP files.

libdvdread's ``ifoOpen()`` returns a single in-memory parsed handle from
either the main IFO or, on open-failure, the BUP. It exposes no way to
force-load the BUP, and no way to load BOTH for comparison. That's exactly
what we need for MakeMKV-class recovery: when the main IFO opens but its
content is corrupt (`Великий Мерлин` shows this on every VTS in our corpus),
we want to fall back to BUP for the affected fields.

This module parses the raw bytes (read via
``libdvdread.probe_ifo_blocks(...)``) and produces dataclasses we can
diff against the libdvdread-parsed handle.

Scope is intentionally narrow — only the fields the rip pipeline needs:

    * VMGI_MAT: title-set count, tt_srpt offset, vts_atrt offset,
      vmg_last_sector / vmgi_last_sector for offset-mismatch checks.
    * VTSI_MAT: vts_pgcit offset, vts_last_sector / vtsi_last_sector,
      audio + sub attribute arrays.
    * VTS_PGCIT: PGC count + offsets.
    * Each PGC: nr_of_programs, nr_of_cells, playback_time, audio_control,
      subp_control, cell_playback array (24 bytes per cell, the only piece
      the cell-walker needs).

Out of scope:
    * program_map / cell_position / palette / command tables
    * TT_SRPT, VTS_PTT_SRPT (title-set vs PTT mapping uses libdvdread)
    * Multi-angle navigation / VM commands

All multi-byte fields are big-endian per DVD-Video spec.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


_logger = logging.getLogger(__name__)

#: DVD logical block size.
LB_LEN = 2048

#: Magic strings at offset 0 of each IFO file.
VMG_IDENTIFIER = b"DVDVIDEO-VMG"
VTS_IDENTIFIER = b"DVDVIDEO-VTS"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ParsedDvdTime:
    """Decoded BCD playback-time field (4 bytes, identical to libdvdread)."""
    hour: int
    minute: int
    second: int
    frame_u: int

    @property
    def frame_rate(self) -> float:
        code = (self.frame_u >> 6) & 0x3
        if code == 0b01:
            return 25.0
        if code == 0b11:
            return 30000.0 / 1001
        return 0.0

    @property
    def frames(self) -> int:
        bcd = self.frame_u & 0x3F
        return ((bcd >> 4) * 10) + (bcd & 0xF)

    @property
    def total_seconds(self) -> float:
        hh = ((self.hour >> 4) * 10) + (self.hour & 0xF)
        mm = ((self.minute >> 4) * 10) + (self.minute & 0xF)
        ss = ((self.second >> 4) * 10) + (self.second & 0xF)
        sec = hh * 3600 + mm * 60 + ss
        fr = self.frame_rate
        if fr > 0:
            sec += self.frames / fr
        return sec


@dataclass(slots=True)
class ParsedAudioAttr:
    raw: bytes  # 8 bytes
    audio_format: int
    multichannel_extension: bool
    lang_type: int
    application_mode: int
    quantization: int
    sample_frequency: int
    channels: int      # 0-based (N-1 channels)
    lang_code: int
    lang_extension: int
    code_extension: int


@dataclass(slots=True)
class ParsedSubpAttr:
    raw: bytes  # 6 bytes
    code_mode: int
    type: int
    lang_code: int
    lang_extension: int
    code_extension: int


@dataclass(slots=True)
class ParsedCell:
    """One cell_playback entry — 24 bytes per spec."""
    cell_index: int       # 1-based
    block_mode: int
    block_type: int
    seamless_play: bool
    interleaved: bool
    stc_discontinuity: bool
    seamless_angle: bool
    cell_type: int
    still_time: int
    cell_cmd_nr: int
    playback_time: ParsedDvdTime
    first_sector: int
    first_ilvu_end_sector: int
    last_vobu_start_sector: int
    last_sector: int


@dataclass(slots=True)
class ParsedPgc:
    """One PGC (program chain) header + cells."""
    pgc_index: int  # 1-based within VTS_PGCIT
    nr_of_programs: int
    nr_of_cells: int
    playback_time: ParsedDvdTime
    audio_control: List[int]   # 8 uint16 entries
    subp_control: List[int]    # 32 uint32 entries
    cells: List[ParsedCell]


@dataclass(slots=True)
class ParsedVmgMat:
    identifier: str
    vmg_last_sector: int
    vmgi_last_sector: int
    specification_version: int
    vmg_category: int
    vmg_nr_of_title_sets: int
    tt_srpt_sector: int
    vts_atrt_sector: int


@dataclass(slots=True)
class ParsedVtsMat:
    identifier: str
    vts_last_sector: int
    vtsi_last_sector: int
    specification_version: int
    vts_category: int
    vtsm_vobs_sector: int
    vtstt_vobs_sector: int
    vts_ptt_srpt_sector: int
    vts_pgcit_sector: int
    nr_of_audio_streams: int
    nr_of_subp_streams: int
    audio_attrs: List[ParsedAudioAttr]
    subp_attrs: List[ParsedSubpAttr]


@dataclass(slots=True)
class ParsedVts:
    vts_mat: ParsedVtsMat
    pgcs: List[ParsedPgc]


# ---------------------------------------------------------------------------
# Reader primitives
# ---------------------------------------------------------------------------

def _u8(buf: bytes, off: int) -> int:
    return buf[off]


def _u16(buf: bytes, off: int) -> int:
    return struct.unpack_from(">H", buf, off)[0]


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from(">I", buf, off)[0]


def _dvdtime(buf: bytes, off: int) -> ParsedDvdTime:
    return ParsedDvdTime(
        hour=buf[off + 0], minute=buf[off + 1],
        second=buf[off + 2], frame_u=buf[off + 3],
    )


# ---------------------------------------------------------------------------
# Audio / sub attribute decoders (the same layout used by libdvdread; bit
# packing per DVD-Video spec, big-endian)
# ---------------------------------------------------------------------------

def parse_audio_attr(buf: bytes, off: int) -> ParsedAudioAttr:
    raw = bytes(buf[off:off + 8])
    b0 = buf[off + 0]
    b1 = buf[off + 1]
    audio_format = (b0 >> 5) & 0x7
    multichannel = bool((b0 >> 4) & 0x1)
    lang_type = (b0 >> 2) & 0x3
    application_mode = b0 & 0x3
    quantization = (b1 >> 6) & 0x3
    sample_frequency = (b1 >> 4) & 0x3
    channels = b1 & 0x7
    lang_code = _u16(buf, off + 2)
    lang_extension = buf[off + 4]
    code_extension = buf[off + 5]
    # buf[off+6..7] are application info / reserved bytes
    return ParsedAudioAttr(
        raw=raw, audio_format=audio_format,
        multichannel_extension=multichannel, lang_type=lang_type,
        application_mode=application_mode, quantization=quantization,
        sample_frequency=sample_frequency, channels=channels,
        lang_code=lang_code, lang_extension=lang_extension,
        code_extension=code_extension,
    )


def parse_subp_attr(buf: bytes, off: int) -> ParsedSubpAttr:
    raw = bytes(buf[off:off + 6])
    b0 = buf[off + 0]
    code_mode = (b0 >> 0) & 0x7
    type_ = (b0 >> 3) & 0x3
    lang_code = _u16(buf, off + 2)
    lang_extension = buf[off + 4]
    code_extension = buf[off + 5]
    return ParsedSubpAttr(
        raw=raw, code_mode=code_mode, type=type_,
        lang_code=lang_code, lang_extension=lang_extension,
        code_extension=code_extension,
    )


# ---------------------------------------------------------------------------
# VMGI_MAT
# ---------------------------------------------------------------------------

class IfoParseError(ValueError):
    """Raised for malformed IFO/BUP bytes."""


def parse_vmg_mat(buf: bytes) -> ParsedVmgMat:
    """Parse a VIDEO_TS.IFO (or .BUP) first sector. Requires at least
    one full sector of bytes."""
    if len(buf) < 0x100:
        raise IfoParseError(
            f"VMGI_MAT needs at least 256 bytes, got {len(buf)}")
    ident = bytes(buf[0:12])
    if not ident.startswith(b"DVDVIDEO-VMG"):
        raise IfoParseError(
            f"VMG identifier mismatch: {ident!r}")
    return ParsedVmgMat(
        identifier=ident.decode("ascii", errors="replace"),
        vmg_last_sector=_u32(buf, 0x0C),
        vmgi_last_sector=_u32(buf, 0x1C),
        specification_version=buf[0x21],
        vmg_category=_u32(buf, 0x22),
        vmg_nr_of_title_sets=_u16(buf, 0x3E),
        tt_srpt_sector=_u32(buf, 0xC4),
        vts_atrt_sector=_u32(buf, 0xD0),
    )


# ---------------------------------------------------------------------------
# VTSI_MAT
# ---------------------------------------------------------------------------

#: Offsets inside VTSI_MAT for the audio/sub attribute arrays. Per DVD-Video
#: spec the VTS attributes block starts at 0x100 (256). audio attrs at 0x202
#: (after 2-byte count zero-padded), subp at 0x254.
_VTSI_AUDIO_COUNT_OFF = 0x202
_VTSI_AUDIO_BLOCK_OFF = 0x204   # first audio_attr, 8 bytes, up to 8 entries
_VTSI_SUBP_COUNT_OFF = 0x254
_VTSI_SUBP_BLOCK_OFF = 0x256    # first subp_attr, 6 bytes, up to 32 entries


def parse_vts_mat(buf: bytes) -> ParsedVtsMat:
    """Parse a VTS_xx_0.IFO (or .BUP) first sector."""
    if len(buf) < 0x300:
        raise IfoParseError(
            f"VTSI_MAT needs at least 768 bytes, got {len(buf)}")
    ident = bytes(buf[0:12])
    if not ident.startswith(VTS_IDENTIFIER):
        raise IfoParseError(
            f"VTS identifier mismatch: {ident!r}")
    nr_audio = buf[_VTSI_AUDIO_COUNT_OFF + 1]   # uint8 in low byte
    nr_subp = buf[_VTSI_SUBP_COUNT_OFF + 1]
    nr_audio = min(nr_audio, 8)
    nr_subp = min(nr_subp, 32)
    audio_attrs = [
        parse_audio_attr(buf, _VTSI_AUDIO_BLOCK_OFF + i * 8)
        for i in range(nr_audio)
    ]
    subp_attrs = [
        parse_subp_attr(buf, _VTSI_SUBP_BLOCK_OFF + i * 6)
        for i in range(nr_subp)
    ]
    return ParsedVtsMat(
        identifier=ident.decode("ascii", errors="replace"),
        vts_last_sector=_u32(buf, 0x0C),
        vtsi_last_sector=_u32(buf, 0x1C),
        specification_version=buf[0x21],
        vts_category=_u32(buf, 0x22),
        vtsm_vobs_sector=_u32(buf, 0xC0),
        vtstt_vobs_sector=_u32(buf, 0xC4),
        vts_ptt_srpt_sector=_u32(buf, 0xC8),
        vts_pgcit_sector=_u32(buf, 0xCC),
        nr_of_audio_streams=nr_audio,
        nr_of_subp_streams=nr_subp,
        audio_attrs=audio_attrs,
        subp_attrs=subp_attrs,
    )


# ---------------------------------------------------------------------------
# VTS_PGCIT + PGC bodies
# ---------------------------------------------------------------------------

#: PGCIT header is 8 bytes; each PGCI_SRP is 8 bytes (entry_id+block_flags
#: +ptl_id_mask:u16 +pgc_start_byte:u32).
_PGCIT_HEADER_SIZE = 8
_PGCI_SRP_SIZE = 8

#: PGC header (excluding cell tables) is 0xE6 (230) bytes per spec.
_PGC_HEADER_SIZE = 0xE6
_CELL_PLAYBACK_SIZE = 0x18    # 24 bytes per cell


def _parse_pgc_header_and_cells(pgcit_bytes: bytes, pgc_start_in_pgcit: int,
                                pgc_index: int) -> ParsedPgc:
    """``pgcit_bytes`` is the whole VTS_PGCIT section (header + all PGCs).
    ``pgc_start_in_pgcit`` is the PGC_START_BYTE field — byte offset of
    the PGC within the PGCIT."""
    p = pgc_start_in_pgcit
    if p + _PGC_HEADER_SIZE > len(pgcit_bytes):
        raise IfoParseError(
            f"PGC {pgc_index} header runs past PGCIT end")
    # PGC header layout per spec (offsets relative to PGC start):
    #   0x00: zero(2)
    #   0x02: nr_of_programs (u8)
    #   0x03: nr_of_cells (u8)
    #   0x04: playback_time (4)
    #   0x08: prohibited_user_ops (4)
    #   0x0C: audio_control[8] (16)
    #   0x1C: subp_control[32] (128)
    #   0x9C: next_pgc_nr (u16)
    #   0x9E: prev_pgc_nr (u16)
    #   0xA0: goup_pgc_nr (u16)
    #   0xA2: pg_playback_mode (u8)
    #   0xA3: still_time (u8)
    #   0xA4: palette[16] (64)
    #   0xE4: command_tbl_offset (u16)  — relative to PGC start
    #   0xE6: program_map_offset (u16)
    #   0xE8: cell_playback_offset (u16)
    #   0xEA: cell_position_offset (u16)
    nr_of_programs = pgcit_bytes[p + 0x02]
    nr_of_cells = pgcit_bytes[p + 0x03]
    playback_time = _dvdtime(pgcit_bytes, p + 0x04)
    audio_control = [
        _u16(pgcit_bytes, p + 0x0C + i * 2) for i in range(8)
    ]
    subp_control = [
        _u32(pgcit_bytes, p + 0x1C + i * 4) for i in range(32)
    ]
    cell_playback_offset = _u16(pgcit_bytes, p + 0xE8)

    cells: List[ParsedCell] = []
    if cell_playback_offset != 0 and nr_of_cells > 0:
        cp_base = p + cell_playback_offset
        for ci in range(nr_of_cells):
            o = cp_base + ci * _CELL_PLAYBACK_SIZE
            if o + _CELL_PLAYBACK_SIZE > len(pgcit_bytes):
                _logger.warning(
                    "PGC %d cell %d truncated; PGCIT shorter than declared",
                    pgc_index, ci + 1)
                break
            b0 = pgcit_bytes[o + 0]
            b1 = pgcit_bytes[o + 1]
            block_mode = (b0 >> 6) & 0x3
            block_type = (b0 >> 4) & 0x3
            seamless_play = bool((b0 >> 3) & 0x1)
            interleaved = bool((b0 >> 2) & 0x1)
            stc_discontinuity = bool((b0 >> 1) & 0x1)
            seamless_angle = bool((b0 >> 0) & 0x1)
            cell_type = b1 & 0x1F
            cells.append(ParsedCell(
                cell_index=ci + 1,
                block_mode=block_mode, block_type=block_type,
                seamless_play=seamless_play, interleaved=interleaved,
                stc_discontinuity=stc_discontinuity,
                seamless_angle=seamless_angle, cell_type=cell_type,
                still_time=pgcit_bytes[o + 2],
                cell_cmd_nr=pgcit_bytes[o + 3],
                playback_time=_dvdtime(pgcit_bytes, o + 4),
                first_sector=_u32(pgcit_bytes, o + 8),
                first_ilvu_end_sector=_u32(pgcit_bytes, o + 12),
                last_vobu_start_sector=_u32(pgcit_bytes, o + 16),
                last_sector=_u32(pgcit_bytes, o + 20),
            ))

    return ParsedPgc(
        pgc_index=pgc_index,
        nr_of_programs=nr_of_programs,
        nr_of_cells=nr_of_cells,
        playback_time=playback_time,
        audio_control=audio_control,
        subp_control=subp_control,
        cells=cells,
    )


def parse_vts_pgcit(buf: bytes) -> List[ParsedPgc]:
    """Parse a VTS_PGCIT section (read via DVDReadBlocks from the sector
    offset stored in VTSI_MAT.vts_pgcit). Returns the PGCs in 1-based
    declaration order."""
    if len(buf) < _PGCIT_HEADER_SIZE:
        raise IfoParseError(
            f"VTS_PGCIT shorter than 8 bytes: {len(buf)}")
    nr_pgcs = _u16(buf, 0)
    if nr_pgcs == 0 or nr_pgcs > 256:
        # 256 is our soft limit; spec doesn't formally cap but anything
        # beyond is corruption.
        _logger.warning("VTS_PGCIT declares %d PGCs (suspicious)", nr_pgcs)
    out: List[ParsedPgc] = []
    for i in range(nr_pgcs):
        srp_off = _PGCIT_HEADER_SIZE + i * _PGCI_SRP_SIZE
        if srp_off + _PGCI_SRP_SIZE > len(buf):
            _logger.warning(
                "VTS_PGCIT SRP %d runs past buffer end; truncating", i + 1)
            break
        pgc_start_byte = _u32(buf, srp_off + 4)
        try:
            pgc = _parse_pgc_header_and_cells(buf, pgc_start_byte, i + 1)
        except IfoParseError as e:
            _logger.warning("PGC %d parse error: %s", i + 1, e)
            continue
        out.append(pgc)
    return out


# ---------------------------------------------------------------------------
# Diff / compare helpers
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class IfoDiff:
    """One observed difference between main IFO and BUP after parsing."""
    path: str      # e.g. "vts_pgcit.pgcs[2].cells[5].first_sector"
    main: object
    bup: object


def diff_vts_mat(main: ParsedVtsMat, bup: ParsedVtsMat) -> List[IfoDiff]:
    out: List[IfoDiff] = []
    for field in ("vts_last_sector", "vtsi_last_sector", "vtsm_vobs_sector",
                  "vtstt_vobs_sector", "vts_ptt_srpt_sector",
                  "vts_pgcit_sector", "nr_of_audio_streams",
                  "nr_of_subp_streams"):
        a = getattr(main, field)
        b = getattr(bup, field)
        if a != b:
            out.append(IfoDiff(f"vts_mat.{field}", a, b))
    for i, (am, bm) in enumerate(zip(main.audio_attrs, bup.audio_attrs)):
        if am.raw != bm.raw:
            out.append(IfoDiff(f"vts_mat.audio_attrs[{i}].raw",
                               am.raw.hex(), bm.raw.hex()))
    for i, (am, bm) in enumerate(zip(main.subp_attrs, bup.subp_attrs)):
        if am.raw != bm.raw:
            out.append(IfoDiff(f"vts_mat.subp_attrs[{i}].raw",
                               am.raw.hex(), bm.raw.hex()))
    return out


def diff_pgcs(main: List[ParsedPgc], bup: List[ParsedPgc]) -> List[IfoDiff]:
    out: List[IfoDiff] = []
    if len(main) != len(bup):
        out.append(IfoDiff("pgcs[].count", len(main), len(bup)))
    for i, (mp, bp) in enumerate(zip(main, bup)):
        for field in ("nr_of_programs", "nr_of_cells"):
            if getattr(mp, field) != getattr(bp, field):
                out.append(IfoDiff(f"pgcs[{i}].{field}",
                                   getattr(mp, field), getattr(bp, field)))
        # Compare playback_time by total seconds (BCD decode robust)
        if abs(mp.playback_time.total_seconds - bp.playback_time.total_seconds) > 0.05:
            out.append(IfoDiff(
                f"pgcs[{i}].playback_time_sec",
                round(mp.playback_time.total_seconds, 3),
                round(bp.playback_time.total_seconds, 3),
            ))
        # Per-cell comparison: sector ranges + duration.
        for ci, (mc, bc) in enumerate(zip(mp.cells, bp.cells)):
            for field in ("first_sector", "last_sector", "block_mode",
                          "block_type"):
                if getattr(mc, field) != getattr(bc, field):
                    out.append(IfoDiff(
                        f"pgcs[{i}].cells[{ci}].{field}",
                        getattr(mc, field), getattr(bc, field)))
            if abs(mc.playback_time.total_seconds - bc.playback_time.total_seconds) > 0.05:
                out.append(IfoDiff(
                    f"pgcs[{i}].cells[{ci}].playback_time_sec",
                    round(mc.playback_time.total_seconds, 3),
                    round(bc.playback_time.total_seconds, 3),
                ))
    return out


# ---------------------------------------------------------------------------
# High-level conveniences
# ---------------------------------------------------------------------------

def parse_ifo_or_bup(buf: bytes, *, is_vmg: bool):
    """Parse the first sector of an IFO/BUP file. Returns ParsedVmgMat
    or ParsedVtsMat depending on ``is_vmg``."""
    if is_vmg:
        return parse_vmg_mat(buf)
    return parse_vts_mat(buf)


def parse_full_vts(vtsi_mat_buf: bytes, pgcit_buf: bytes) -> ParsedVts:
    """Parse VTSI_MAT + VTS_PGCIT into a ParsedVts. Caller supplies the
    PGCIT bytes (read at sector ``vts_pgcit_sector`` from the IFO file)."""
    mat = parse_vts_mat(vtsi_mat_buf)
    pgcs = parse_vts_pgcit(pgcit_buf)
    return ParsedVts(vts_mat=mat, pgcs=pgcs)


# ---------------------------------------------------------------------------
# Serialisation for JSON output / tests
# ---------------------------------------------------------------------------

def diff_to_dict(d: IfoDiff) -> dict:
    return {"path": d.path, "main": d.main, "bup": d.bup}


__all__ = [
    "IfoParseError",
    "IfoDiff",
    "LB_LEN",
    "ParsedAudioAttr",
    "ParsedCell",
    "ParsedDvdTime",
    "ParsedPgc",
    "ParsedSubpAttr",
    "ParsedVmgMat",
    "ParsedVts",
    "ParsedVtsMat",
    "diff_pgcs",
    "diff_to_dict",
    "diff_vts_mat",
    "parse_audio_attr",
    "parse_full_vts",
    "parse_ifo_or_bup",
    "parse_subp_attr",
    "parse_vmg_mat",
    "parse_vts_mat",
    "parse_vts_pgcit",
]
