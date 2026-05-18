"""
DVD-Video subpicture handling.

Two responsibilities:

1. **SP_DCSQ parser** — read a DVD subpicture unit (SPU), walk its Display
   Control Sequence Table (DCSQT), and compute the display duration from
   the STA_DSP → STP_DSP timing. The full SPU bytes are returned verbatim
   for embedding into the MKV S_VOBSUB stream.

2. **VobSub .idx builder** — assemble the codec_private text block that
   MKV's S_VOBSUB requires (palette + screen size in mplayer-style .idx
   format). Pulls colour data from the PGC's 16-entry YCbCr palette.

References:
- DVD-Video Part 3 (Video specifications) §VI.4.2 — SPU layout
- Matroska spec: https://www.matroska.org/technical/codec_specs.html#S_VOBSUB

This module is pure-Python and has no libdvdread dependency. Callers feed
it raw bytes + the relevant disc-side metadata.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# DCSQ command opcodes (DVD-Video Part 3 §VI.4.2.3)
# ---------------------------------------------------------------------------

DCSQ_FSTA_DSP   = 0x00   # Forced Start Display: 0 arg bytes
DCSQ_STA_DSP    = 0x01   # Start Display:        0 arg bytes
DCSQ_STP_DSP    = 0x02   # Stop Display:         0 arg bytes
DCSQ_SET_COLOR  = 0x03   # 2 bytes (4 × 4-bit palette indices)
DCSQ_SET_CONTR  = 0x04   # 2 bytes (4 × 4-bit contrasts)
DCSQ_SET_DAREA  = 0x05   # 6 bytes (display area X1 X2 Y1 Y2)
DCSQ_SET_DSPXA  = 0x06   # 4 bytes (top + bottom field RLE offsets)
DCSQ_CHG_COLCON = 0x07   # variable: change color/contrast (1st 2 bytes
                         # are the payload length including the 2 length bytes)
DCSQ_END        = 0xFF   # end of commands in this DCSQ

# Fixed sizes for the simple opcodes. CHG_COLCON is variable; END terminates.
_DCSQ_FIXED_LEN = {
    DCSQ_FSTA_DSP:   0,
    DCSQ_STA_DSP:    0,
    DCSQ_STP_DSP:    0,
    DCSQ_SET_COLOR:  2,
    DCSQ_SET_CONTR:  2,
    DCSQ_SET_DAREA:  6,
    DCSQ_SET_DSPXA:  4,
}

# Each delta_pts unit in a DCSQ header is 1024 ticks of 90 kHz
# (DVD-Video Part 3 §VI.4.2.2: "1 unit = 1024/90 000 s").
DCSQ_DELTA_TO_90KHZ = 1024

# Default sub display duration when STP_DSP is missing and there's no next
# event to bound the time. 5 seconds.
DEFAULT_SUBPIC_DURATION_TICKS = 5 * 90000


@dataclass(slots=True)
class SubpicEvent:
    """One parsed SPU, ready to feed into an MKV S_VOBSUB block."""
    pts: int                  # 90 kHz ticks — the PES PTS (start of display)
    duration_ticks: int       # 90 kHz ticks; 0 if no STP_DSP found
    data: bytes               # the full SPU bytes (raw, embedded as-is in MKV)
    dcsq_count: int = 0       # number of DCSQs parsed (for diagnostics)
    has_forced: bool = False  # FSTA_DSP seen


def parse_subpic(spu_bytes: bytes, pes_pts: int) -> Optional[SubpicEvent]:
    """Parse a DVD subpicture unit. Returns ``None`` if the SPU is too
    short or its DCSQT pointer is invalid."""
    if len(spu_bytes) < 4:
        return None

    spu_size = int.from_bytes(spu_bytes[0:2], "big")
    dcsq_start = int.from_bytes(spu_bytes[2:4], "big")

    # Sanity: declared SPU size should match (or exceed) what we have.
    # Some authoring tools pad/trim; tolerate spu_size <= len.
    if dcsq_start < 4 or dcsq_start >= len(spu_bytes):
        return None
    if spu_size > 0 and spu_size > len(spu_bytes):
        return None

    sta_delta: Optional[int] = None  # delta_pts (units) of STA_DSP / FSTA_DSP
    stp_delta: Optional[int] = None  # delta_pts of STP_DSP
    has_forced = False
    dcsq_count = 0
    seen_offsets: set[int] = set()

    cur = dcsq_start
    while 0 <= cur < len(spu_bytes) - 4:
        if cur in seen_offsets:
            break  # malformed loop
        seen_offsets.add(cur)
        delta_pts = int.from_bytes(spu_bytes[cur:cur + 2], "big")
        next_off  = int.from_bytes(spu_bytes[cur + 2:cur + 4], "big")
        dcsq_count += 1

        p = cur + 4
        while p < len(spu_bytes):
            op = spu_bytes[p]
            p += 1
            if op == DCSQ_END:
                break
            if op == DCSQ_FSTA_DSP:
                has_forced = True
                if sta_delta is None:
                    sta_delta = delta_pts
                continue
            if op == DCSQ_STA_DSP:
                if sta_delta is None:
                    sta_delta = delta_pts
                continue
            if op == DCSQ_STP_DSP:
                stp_delta = delta_pts   # always last STP wins
                continue
            if op == DCSQ_CHG_COLCON:
                # Length-prefixed variable payload (2-byte length includes
                # the 2 length bytes themselves).
                if p + 2 > len(spu_bytes):
                    break
                length = int.from_bytes(spu_bytes[p:p + 2], "big")
                if length < 2:
                    break
                p += length
                continue
            fixed = _DCSQ_FIXED_LEN.get(op)
            if fixed is None:
                # Unknown opcode — bail out of this DCSQ (the rest of
                # this DCSQ is opaque to us, but we keep the durations
                # we've collected so far).
                break
            p += fixed

        # next_off pointing back to or before cur signals end-of-list.
        if next_off <= cur:
            break
        cur = next_off

    sta = sta_delta if sta_delta is not None else 0
    if stp_delta is not None and stp_delta >= sta:
        duration_ticks = (stp_delta - sta) * DCSQ_DELTA_TO_90KHZ
    else:
        duration_ticks = 0   # caller fills via lookahead or default

    return SubpicEvent(
        pts=pes_pts,
        duration_ticks=duration_ticks,
        data=bytes(spu_bytes),
        dcsq_count=dcsq_count,
        has_forced=has_forced,
    )


# ---------------------------------------------------------------------------
# VobSub .idx codec_private builder
# ---------------------------------------------------------------------------

def _ycbcr_to_rgb_hex(y: int, cb: int, cr: int) -> str:
    """ITU-R BT.601 inverse to 24-bit RGB hex (no '#' prefix)."""
    yf = y - 16
    cbf = cb - 128
    crf = cr - 128
    r = 1.164 * yf + 1.596 * crf
    g = 1.164 * yf - 0.392 * cbf - 0.813 * crf
    b = 1.164 * yf + 2.017 * cbf
    r = max(0, min(255, round(r)))
    g = max(0, min(255, round(g)))
    b = max(0, min(255, round(b)))
    return f"{r:02x}{g:02x}{b:02x}"


def build_vobsub_idx(
    pgc_palette: bytes,
    screen_w: int = 720,
    screen_h: int = 480,
    *,
    lang_index: int = 0,
) -> bytes:
    """Build the codec_private blob for an S_VOBSUB MKV track.

    ``pgc_palette`` is the 64-byte per-PGC YCbCr palette (16 entries × 4
    bytes; layout per DVD-Video spec: byte0=reserved, byte1=Y, byte2=Cr,
    byte3=Cb). Output is the .idx textual blob mkvmerge / mplayer parse.

    Notes:
    - Only the global palette + screen size are written; per-event SET_DAREA
      / SET_COLOR / SET_CONTR live inside each SPU's DCSQ and are processed
      by the player at render time.
    - The first palette entry is conventionally the "transparent" colour;
      we still emit its computed RGB value — players honour SET_CONTR for
      alpha.
    """
    palette_hex = []
    for i in range(16):
        off = i * 4
        if off + 4 > len(pgc_palette):
            palette_hex.append("000000")
            continue
        y  = pgc_palette[off + 1]
        cr = pgc_palette[off + 2]
        cb = pgc_palette[off + 3]
        palette_hex.append(_ycbcr_to_rgb_hex(y, cb, cr))

    body = (
        "# VobSub index file, v7 -- Do not edit!\n"
        "#\n"
        "# To repair desyncronization, you can insert gaps into this file:\n"
        "#\n"
        "# Settings\n"
        f"size: {screen_w}x{screen_h}\n"
        "org: 0, 0\n"
        "scale: 100%\n"
        "alpha: 100%\n"
        "smooth: OFF\n"
        "fadein/out: 50, 50\n"
        "align: OFF at LEFT TOP\n"
        "time offset: 0\n"
        "forced subs: OFF\n"
        "palette: " + ", ".join(palette_hex) + "\n"
        f"langidx: {lang_index}\n"
    )
    return body.encode("ascii")


__all__ = [
    # Constants
    "DCSQ_FSTA_DSP", "DCSQ_STA_DSP", "DCSQ_STP_DSP",
    "DCSQ_SET_COLOR", "DCSQ_SET_CONTR", "DCSQ_SET_DAREA",
    "DCSQ_SET_DSPXA", "DCSQ_CHG_COLCON", "DCSQ_END",
    "DCSQ_DELTA_TO_90KHZ", "DEFAULT_SUBPIC_DURATION_TICKS",
    # Classes / functions
    "SubpicEvent",
    "parse_subpic",
    "build_vobsub_idx",
]
