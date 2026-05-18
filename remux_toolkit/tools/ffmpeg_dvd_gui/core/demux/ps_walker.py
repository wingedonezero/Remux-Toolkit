"""
MPEG-PS pack & PES walker for DVD-Video sectors.

DVD-Video stores content as a stream of 2048-byte sectors, each holding
exactly one MPEG-2 Program Stream pack. A pack contains:

  * pack_header        (14 bytes incl. SCR, mux rate)        — always present
  * system_header      (optional, start code 0x000001BB)     — usually only
                                                                 in the first
                                                                 pack of a VOBU
  * PES_packet ...     (one or more)                          — until end of
                                                                 sector

PES stream IDs of interest on DVD:

  0xE0          MPEG-2 video (DVD always uses 0xE0)
  0xC0-0xCF     MPEG audio (rare on commercial discs)
  0xBD          private_stream_1 (AC3, DTS, LPCM, subpicture)
                — first byte of payload is substream_id:
                    0x20-0x3F   subpicture stream
                    0x80-0x87   AC3
                    0x88-0x8F   DTS
                    0xA0-0xA7   LPCM
                    0xC0-0xCF   MLP (very rare)
  0xBE          padding stream
  0xBF          private_stream_2 (DVD NAV pack — DSI/PCI data)
  0xBB          system header start code (not a PES)

We expose two views:

  * `iter_pes_in_sector(sector)` — yields raw PES records *within one sector*,
    each tagged with offset/length but no substream parsing.
  * `iter_es_payloads(sector_iter)` — higher-level: yields ESPayload records
    keyed by (stream_id, substream_id) with PTS/DTS decoded and substream
    framing headers stripped.

The lower-level view is what the diagnostic CLI prints; the higher-level view
is what the muxer will hand to ffmpeg.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional


PACK_START_CODE   = b"\x00\x00\x01\xBA"
SYSTEM_HEADER_SC  = b"\x00\x00\x01\xBB"
PROGRAM_END_CODE  = b"\x00\x00\x01\xB9"


# Stream-id ranges
STREAM_MPEG_VIDEO = 0xE0       # actually 0xE0-0xEF, but DVD uses 0xE0
STREAM_PRIVATE_1  = 0xBD
STREAM_PADDING    = 0xBE
STREAM_PRIVATE_2  = 0xBF


# ---------------------------------------------------------------------------
# PTS / DTS decoding
# ---------------------------------------------------------------------------

def _decode_pts_field(buf: bytes, offset: int) -> int:
    """Decode a 33-bit PTS/DTS timestamp from a 5-byte PES header field.
    Returns 90-kHz ticks. Caller has already validated the marker nibble."""
    b0, b1, b2, b3, b4 = buf[offset:offset + 5]
    pts = ((b0 >> 1) & 0x07) << 30
    pts |= b1 << 22
    pts |= ((b2 >> 1) & 0x7F) << 15
    pts |= b3 << 7
    pts |= (b4 >> 1) & 0x7F
    return pts


# ---------------------------------------------------------------------------
# Pack-level structures
# ---------------------------------------------------------------------------

@dataclass
class RawPES:
    """A PES packet as found within one DVD sector. `payload` is the bytes
    after the PES header (so for private_stream_1, it still starts with the
    substream_id)."""
    stream_id: int
    pes_length: int          # value from PES_packet_length field
    pts: Optional[int]       # 90-kHz units; None if not present
    dts: Optional[int]
    payload_offset: int      # offset in the sector where payload begins
    payload: bytes


def _parse_pack_header_length(sector: bytes) -> int:
    """Return the offset within `sector` where pack-level content begins
    (i.e. just past pack_header + stuffing). Raises if start code is wrong."""
    if sector[0:4] != PACK_START_CODE:
        raise ValueError("Sector does not start with pack_start_code")
    # MPEG-2 pack header is 14 bytes; the low 3 bits of byte 13 hold stuffing
    pack_stuffing = sector[13] & 0x07
    return 14 + pack_stuffing


def iter_pes_in_sector(sector: bytes) -> Iterator[RawPES]:
    """Walk all PES packets contained in a single 2048-byte sector.

    Skips: pack header, system header (if present), padding stream (0xBE).
    Yields private_stream_2 (NAV) as well — callers filter by stream_id.
    """
    if len(sector) < 14:
        return
    pos = _parse_pack_header_length(sector)

    # Optional system header
    if sector[pos:pos + 4] == SYSTEM_HEADER_SC:
        if pos + 6 > len(sector):
            return
        sys_len = (sector[pos + 4] << 8) | sector[pos + 5]
        pos += 6 + sys_len

    while pos + 6 <= len(sector):
        if sector[pos:pos + 3] != b"\x00\x00\x01":
            # Padding tail of pack; nothing more here.
            return
        stream_id = sector[pos + 3]
        if stream_id == 0xB9:  # program end code (unlikely mid-stream)
            return
        pes_length = (sector[pos + 4] << 8) | sector[pos + 5]
        body_end = pos + 6 + pes_length
        if body_end > len(sector):
            # Spans into the next pack/sector. Truncate at sector boundary;
            # caller can stitch via a higher-level continuation tracker.
            body_end = len(sector)

        if stream_id == STREAM_PADDING:
            pos = body_end
            continue

        pts, dts, payload_off = _parse_pes_header(sector, pos, pes_length)
        yield RawPES(
            stream_id=stream_id,
            pes_length=pes_length,
            pts=pts,
            dts=dts,
            payload_offset=payload_off,
            payload=sector[payload_off:body_end],
        )
        pos = body_end


def _parse_pes_header(sector: bytes, pos: int, pes_length: int) -> tuple[Optional[int], Optional[int], int]:
    """Parse the optional PES header beyond the 6-byte length prefix.
    Returns (pts, dts, payload_offset_in_sector).

    For MPEG-2 (which DVDs use), the header bytes immediately after the
    length field are:
        byte 6: '10' marker, scrambling, priority, alignment, copyright, original
        byte 7: PTS_DTS_flags(2), ESCR_flag, ES_rate_flag, DSM_trick_flag,
                additional_copy_info_flag, PES_CRC_flag, PES_extension_flag
        byte 8: PES_header_data_length (length of the optional fields)
        bytes 9..9+header_data_length: optional fields (PTS, DTS, etc.)
    """
    stream_id = sector[pos + 3]
    # private_stream_2 (NAV packs) and a few other types don't have the MPEG-2
    # PES extension header — payload starts right after the 6 length bytes.
    if stream_id == STREAM_PRIVATE_2:
        return (None, None, pos + 6)

    if pos + 9 > len(sector):
        return (None, None, pos + 6)

    flag_byte = sector[pos + 7]
    pts_dts_flags = (flag_byte >> 6) & 0x03
    header_data_length = sector[pos + 8]
    payload_offset = pos + 9 + header_data_length

    pts: Optional[int] = None
    dts: Optional[int] = None
    if pts_dts_flags & 0x02 and pos + 14 <= len(sector):
        pts = _decode_pts_field(sector, pos + 9)
        if pts_dts_flags & 0x01 and pos + 19 <= len(sector):
            dts = _decode_pts_field(sector, pos + 14)

    return (pts, dts, payload_offset)


# ---------------------------------------------------------------------------
# Elementary-stream-level view
# ---------------------------------------------------------------------------

@dataclass
class ESPayload:
    """One PES packet's payload, with substream framing stripped where
    applicable, ready to feed to a downstream consumer (or ffmpeg)."""
    stream_id: int                 # 0xE0 = video, 0xBD = private_1, etc.
    substream_id: Optional[int]    # only set when stream_id == 0xBD
    pts: Optional[int]             # 90-kHz ticks
    dts: Optional[int]
    cell_index: int                # 1-based PGC cell that supplied this packet
    sector_offset: int             # disc sector this PES came from
    is_nav: bool                   # True for private_stream_2
    es_bytes: bytes                # the data to write into the elementary
                                    # stream output (substream header stripped)


def _split_private_stream_1(payload: bytes) -> tuple[Optional[int], bytes]:
    """For a private_stream_1 PES payload (AC3/DTS/LPCM/subpicture):
       byte 0       — substream_id
       bytes 1..3   — frame counter + first-frame-offset (AC3/DTS) OR
       bytes 1..6   — LPCM header (sample rate, bits, channels)
       bytes ..N    — actual ES bytes
    For subpictures (0x20-0x3F), the substream is just substream_id + raw subpicture payload."""
    if not payload:
        return (None, b"")
    substream_id = payload[0]
    if 0x20 <= substream_id <= 0x3F:
        # Subpicture: 1-byte ID + raw subpicture data
        return (substream_id, payload[1:])
    if 0x80 <= substream_id <= 0x87:
        # AC3: substream_id + 3-byte framing header (frames in pack +
        # first-access-unit pointer)
        return (substream_id, payload[4:])
    if 0x88 <= substream_id <= 0x8F:
        # DTS: same 4-byte header as AC3
        return (substream_id, payload[4:])
    if 0xA0 <= substream_id <= 0xA7:
        # LPCM: 7-byte header (incl substream_id)
        return (substream_id, payload[7:])
    # Unknown / less-common substream: pass through as-is, drop the 1-byte id
    return (substream_id, payload[1:])


def iter_es_payloads(
    sectors: Iterator[tuple],  # (cell, sector_bytes) from CellReader
) -> Iterator[ESPayload]:
    """Walk sectors from CellReader and yield ESPayload records ready for
    elementary-stream output. NAV packs (private_stream_2) are emitted with
    `is_nav=True` so the caller can use them for cell-boundary diagnostics
    but omit from the actual output stream."""
    for cell, sector in sectors:
        try:
            packets = list(iter_pes_in_sector(sector))
        except ValueError:
            # Corrupt or non-MPEG-PS sector; skip without crashing the rip.
            continue
        for pes in packets:
            sub_id: Optional[int] = None
            es_bytes = pes.payload
            is_nav = (pes.stream_id == STREAM_PRIVATE_2)
            if pes.stream_id == STREAM_PRIVATE_1:
                sub_id, es_bytes = _split_private_stream_1(pes.payload)
            yield ESPayload(
                stream_id=pes.stream_id,
                substream_id=sub_id,
                pts=pes.pts,
                dts=pes.dts,
                cell_index=cell.index,
                sector_offset=cell.first_sector,  # batch offset
                is_nav=is_nav,
                es_bytes=es_bytes,
            )


# ---------------------------------------------------------------------------
# Stream identity helpers
# ---------------------------------------------------------------------------

def stream_kind(stream_id: int, substream_id: Optional[int]) -> str:
    """Human-readable name for a (stream_id, substream_id) pair."""
    if stream_id == STREAM_PRIVATE_2:
        return "nav"
    if 0xE0 <= stream_id <= 0xEF:
        return "video"
    if 0xC0 <= stream_id <= 0xDF:
        return "mp2_audio"
    if stream_id == STREAM_PRIVATE_1 and substream_id is not None:
        if 0x20 <= substream_id <= 0x3F:
            return "subpicture"
        if 0x80 <= substream_id <= 0x87:
            return "ac3"
        if 0x88 <= substream_id <= 0x8F:
            return "dts"
        if 0xA0 <= substream_id <= 0xA7:
            return "lpcm"
        return f"private1_{substream_id:#04x}"
    return f"stream_{stream_id:#04x}"


def stream_key(stream_id: int, substream_id: Optional[int]) -> tuple[int, int]:
    """A hashable identifier for a logical elementary stream. Lets us bucket
    PES packets per stream during the demux."""
    return (stream_id, substream_id if substream_id is not None else -1)
