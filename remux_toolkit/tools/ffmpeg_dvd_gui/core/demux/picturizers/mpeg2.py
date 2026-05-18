"""
MPEG-1 / MPEG-2 picturizer.

Splits a buffered group of MPEG video PES bytes into one chunk per coded
picture. Each chunk carries an MPEG-2 picture's bytes verbatim (including
any leading sequence_header / GOP_header for keyframes) and is annotated
with the codec's picture_coding_type — I, P, B, or D — so the muxer can
flag KEYFRAME / DISCARDABLE correctly.

The chunk timecodes are extrapolated from a single base PTS via the
track's default_duration (CFR assumption). DVD-Video is CFR within a
single GOP, so this is exact.

This module is pure-Python and has no libdvdread / disc dependency.
"""
from __future__ import annotations

from typing import Iterator

from ...mux.types import MkvChunk, MkvChunkFlags


# ---------------------------------------------------------------------------
# MPEG-2 start codes (ISO 13818-2 §6.2.1)
# ---------------------------------------------------------------------------

#: 4-byte byte-string for picture_start_code (0x00000100).
PICTURE_START_CODE = b"\x00\x00\x01\x00"


# ISO 13818-2 picture_coding_type values (Table 6-12).
PCT_FORBIDDEN = 0
PCT_I = 1
PCT_P = 2
PCT_B = 3
PCT_D = 4   # MPEG-1 only


def picture_coding_type(pic_bytes: bytes) -> int:
    """Read picture_coding_type from the picture_header inside an MPEG-2 picture.

    The picture_header layout (ISO 13818-2 §6.2.3) after picture_start_code:
        temporal_reference   10 bits
        picture_coding_type   3 bits   ← what we want
        vbv_delay            16 bits

    The 3-bit picture_coding_type lives at bits 5-3 of the 6th byte
    (i.e. data[psc + 5] >> 3 & 0x7) where psc is the byte offset of
    picture_start_code (0x000001 00) within pic_bytes.

    Returns 0 if no picture_header is found or pic_bytes is truncated.
    """
    psc = pic_bytes.find(PICTURE_START_CODE)
    if psc < 0 or psc + 6 > len(pic_bytes):
        return PCT_FORBIDDEN
    return (pic_bytes[psc + 5] >> 3) & 0x7


def emit_pictures(group_bytes: bytes, base_pts_ns: int,
                  default_duration_ns: int) -> Iterator[MkvChunk]:
    """Walk an MPEG-2 PES group and yield one MkvChunk per picture.

    Algorithm:
        1. Find every byte offset where ``PICTURE_START_CODE`` (4 bytes)
           appears in ``group_bytes``. These are picture boundaries.
        2. Build slice boundaries: ``[0, starts[1], ..., starts[-1], len]``.
           Picture 0 spans from byte 0 (so any leading sequence_header or
           gop_header attaches to it); subsequent pictures span from one
           picture_start_code to the next.
        3. For each slice, classify via ``picture_coding_type``: I/D →
           KEYFRAME, B → DISCARDABLE, P → no flag.
        4. Extrapolate timecodes by ``i * default_duration_ns`` from
           ``base_pts_ns``.

    Yields chunks with ``duration = default_duration_ns``. The caller is
    free to override (e.g. for variable-rate streams) but on DVD the
    extrapolated values are exact since GOPs are CFR.

    Notes:
    - MPEG-2 doesn't use start_code_emulation_prevention bytes — start
      codes are always byte-aligned and can be located by naive byte
      search. (H.264/HEVC need NALU-aware parsing; that's a separate
      picturizer module.)
    - If no picture_start_code is found, the whole group is emitted as
      one chunk; this gracefully handles micro-PES groups that span a
      single picture with no extra headers.
    """
    starts: list[int] = []
    pos = 0
    while True:
        idx = group_bytes.find(PICTURE_START_CODE, pos)
        if idx < 0:
            break
        starts.append(idx)
        pos = idx + 4

    # Build picture boundaries.
    #   * 0 picture starts found → one chunk for the whole group.
    #   * 1 picture start → still one chunk (the group is one picture +
    #     headers; the start code marks where the picture_header lives).
    #   * ≥2 → multiple pictures. Picture 0 absorbs any pre-start-code
    #     prefix (sequence_header / GOP_header). Subsequent pictures
    #     start at their own picture_start_code.
    if len(starts) <= 1:
        boundaries: list[int] = [0, len(group_bytes)]
    else:
        boundaries = [0] + starts[1:] + [len(group_bytes)]

    for i in range(len(boundaries) - 1):
        s, e = boundaries[i], boundaries[i + 1]
        if e <= s:
            continue
        pic_bytes = group_bytes[s:e]
        flags = MkvChunkFlags.NONE
        pct = picture_coding_type(pic_bytes)
        if pct in (PCT_I, PCT_D):
            flags |= MkvChunkFlags.KEYFRAME
        elif pct == PCT_B:
            flags |= MkvChunkFlags.DISCARDABLE
        yield MkvChunk(
            data=pic_bytes,
            timecode=base_pts_ns + i * default_duration_ns,
            duration=default_duration_ns,
            flags=flags,
        )


__all__ = [
    "PICTURE_START_CODE",
    "PCT_FORBIDDEN", "PCT_I", "PCT_P", "PCT_B", "PCT_D",
    "picture_coding_type",
    "emit_pictures",
]
