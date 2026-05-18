"""
Data carriers + enums for the MakeMKV-aligned MKV muxer.

These mirror MakeMKV's libmkv.h API shape in Pythonic form. Each carrier is
a dataclass; flags are IntFlag enums so bitwise ops work naturally.

C++ ↔ Python mapping:
    IMkvChunk           → MkvChunk           (one frame of stream data)
    MkvTrackInfo        → MkvTrackInfo       (per-track metadata)
    MkvChapterInfo      → MkvChapterInfo
    MkvAttachmentInfo   → MkvAttachmentInfo
    MkvTitleInfo (data) → MkvTitleNameInfo   (renamed to free the
                                              ``IMkvTitleInfo`` name for the
                                              interface protocol)
    MkvFormatInfo       → MkvFormatInfo
    MkvProfileInfo      → MkvProfileInfo
    MkvDebugInfo        → MkvDebugInfo
    MkvProfileTrackInfo → MkvProfileTrackInfo

This module is pure-Python and stdlib-only; it imports nothing from our
existing demuxer or analyzer so it can be consumed from any layer.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ----------------------------------------------------------------------
# Timing constants (mirrored verbatim from libmkv.cpp)
# ----------------------------------------------------------------------

#: Nanoseconds per MKV TimecodeScale unit. 1,000,000 ns = 1 ms granularity.
TIMECODE_SCALE: int = 1_000_000

#: Max bytes used for a MKV timecode field. 6 bytes ⇒ 2^48 units ≈ 89 years.
MAX_TIMECODE_SIZE_BYTES: int = 6

#: Largest representable timecode in MKV TimecodeScale units.
MAX_TIMECODE: int = (1 << (8 * MAX_TIMECODE_SIZE_BYTES)) - 1

#: AUTO_DURATION sentinel. Frames with unknown duration (typical for
#: subtitles) write this 4.5 sec placeholder; the muxer retroactively
#: overwrites it once the next frame's timecode is known.
AUTO_DURATION_TIMECODE: int = 4_500_000_000

#: Sentinel meaning "no reference exists" — used in video reference-block
#: bookkeeping. Never appears in valid output.
BAD_TIMECODE: int = (1 << 62) + 1


def timecode_from_clock(stc_90khz: int) -> int:
    """Convert a 90 kHz MPEG STC tick to a libmatroska timecode (ns).

    This is the exact formula libmkv.cpp uses (``(stc * 25) / 27``). Kept
    verbatim so our output is byte-identical to MakeMKV's at the muxer
    level.
    """
    return (stc_90khz * 25) // 27


def scale_timecode(timecode_ns: int) -> int:
    """Convert nanoseconds to MKV TimecodeScale units (1 ms each)."""
    return timecode_ns // TIMECODE_SCALE


# ----------------------------------------------------------------------
# Track types
# ----------------------------------------------------------------------

class MkvTrackType(enum.IntEnum):
    """Mirrors ``MkvTrackType`` in libmkv.h."""
    UNKNOWN = 0
    VIDEO = 1
    AUDIO = 2
    SUBTITLE = 3


# ----------------------------------------------------------------------
# Flag enums
# ----------------------------------------------------------------------

class MkvChunkFlags(enum.IntFlag):
    """Per-frame flags. Corresponds to the ``MKV_CHUNK_*`` constants in libmkv.h."""
    NONE = 0
    KEYFRAME = 1
    CLUSTER_START = 2
    CHAPTER_MARK = 4
    DISCARDABLE = 8
    OLD_BLOCK = 16
    AUTO_DURATION = 32


class MkvTrackFlags(enum.IntFlag):
    """Per-track behaviour flags. Corresponds to the ``MKV_TRACK_FLAG_*`` constants."""
    NONE = 0
    DEFAULT = 1
    FORCED = 2
    LACING = 128


class MkvCompression(enum.IntEnum):
    """Per-track compression mode. Mirrors ``MKV_TRACK_COMPRESSION_*``.

    ``HEADERS`` is the AC3-style frame-header-stripping optimization: the
    fixed prefix is stored once in ``ContentCompSettings`` and removed from
    every block. Saves ~3% on AC3 audio.
    """
    ZLIB = 0
    BZ2 = 1
    LZO = 2
    HEADERS = 3
    NONE = 4


# ----------------------------------------------------------------------
# Per-type stream info
# ----------------------------------------------------------------------

@dataclass(slots=True)
class VideoInfo:
    pixel_h: int = 0
    pixel_v: int = 0
    display_h: int = 0
    display_v: int = 0
    fps_n: int = 0
    fps_d: int = 1
    stereo_mode: int = 0
    priv: Tuple[int, int] = (0, 0)

    # Colour metadata (KaxVideoColour). Integer codes follow ITU-T H.273
    # (the same codes Matroska + ffmpeg use):
    #   primaries:     5 = BT.470 BG (PAL), 6 = SMPTE 170M (NTSC SD)
    #   transfer:      6 = SMPTE 170M (BT.601), 1 = BT.709
    #   matrix:        5 = BT.470 BG (PAL), 6 = SMPTE 170M (NTSC SD)
    #   color_range:   1 = broadcast / TV (16-235), 2 = full (0-255)
    # 0 means "unspecified" — the shim only writes the element when ≠ 0.
    color_primaries: int = 0
    color_transfer: int = 0
    color_matrix: int = 0
    color_range: int = 0


@dataclass(slots=True)
class AudioInfo:
    sample_rate: int = 0
    channels_count: int = 0
    bits_per_sample: int = 0
    channel_layout: int = 0


@dataclass(slots=True)
class SubtitleInfo:
    offset_sequence_id_ref: int = 0


# ----------------------------------------------------------------------
# Codec-private extras (BlockAdditionMapping — HDR, alpha, dvcc, etc.)
# ----------------------------------------------------------------------

@dataclass(slots=True)
class CodecPrivateExtra:
    """One extra ``BlockAdditionMapping`` entry. Up to 4 per track."""
    tag: int        # BlockAddIDType value
    data: bytes


# ----------------------------------------------------------------------
# Per-track compression profile
# ----------------------------------------------------------------------

@dataclass(slots=True)
class MkvProfileTrackInfo:
    compression_type: MkvCompression = MkvCompression.NONE
    compression_level: int = 0


# ----------------------------------------------------------------------
# Track metadata (the big one)
# ----------------------------------------------------------------------

@dataclass(slots=True)
class MkvTrackInfo:
    """Everything needed to write a Matroska TrackEntry.

    Defaults make this constructible without specifying every field — only
    ``type`` and ``codec_id`` need a non-default value to be useful.
    """
    type: MkvTrackType = MkvTrackType.UNKNOWN
    codec_id: str = ""                     # e.g. "V_MPEG2", "A_AC3", "S_VOBSUB"
    codec_subid: Optional[str] = None
    lang: str = "und"                      # ISO 639 code
    metadata_lang: Optional[str] = None
    name: Optional[str] = None

    codec_private: Optional[bytes] = None
    codec_private_extra: List[CodecPrivateExtra] = field(default_factory=list)

    # For ``MkvCompression.HEADERS``: the bytes that get stripped from every
    # block (and stored once in ContentCompSettings). For AC3, this is the
    # 5-byte AC3 sync header.
    header_comp_data: Optional[bytes] = None

    mkv_flags: MkvTrackFlags = MkvTrackFlags.NONE

    default_duration: int = 0              # ns
    dts_adjust: int = 0                    # ns
    bitrate: int = 0

    stream_flags: int = 0                  # AP_AVStream-style flags (used for
                                           # the offset_sequence_id_ref tag)
    stream_subtype: int = 0

    min_cache: int = 0

    profile_track_info: Optional[MkvProfileTrackInfo] = None

    video: Optional[VideoInfo] = None
    audio: Optional[AudioInfo] = None
    subtitle: Optional[SubtitleInfo] = None

    # Up to 8 bytes. The first byte is the length of the rest. Drives the
    # ``SOURCE_ID`` per-track statistics tag in the finalised MKV.
    source_id: bytes = b""

    def __post_init__(self) -> None:
        # Per-type info must match track type.
        if self.type == MkvTrackType.VIDEO:
            if self.audio is not None or self.subtitle is not None:
                raise ValueError("Video track must not carry audio/subtitle info")
        elif self.type == MkvTrackType.AUDIO:
            if self.video is not None or self.subtitle is not None:
                raise ValueError("Audio track must not carry video/subtitle info")
        elif self.type == MkvTrackType.SUBTITLE:
            if self.video is not None or self.audio is not None:
                raise ValueError("Subtitle track must not carry video/audio info")
        if len(self.codec_private_extra) > 4:
            raise ValueError("at most 4 codec_private_extra entries allowed")


# ----------------------------------------------------------------------
# Chapters
# ----------------------------------------------------------------------

@dataclass(slots=True)
class MkvChapterInfo:
    """One chapter's display names + timing.

    Deviates from MakeMKV's ``MkvChapterInfo`` struct (which had no timecode):
    MakeMKV computes the timecode at mux time from per-frame ``CHAPTER_MARK``
    flags. We support the same model when desired, but static-mux backends
    (mkvmerge, ffmpeg with metadata file) need timecodes upfront, so we carry
    them on the chapter info itself.
    """
    #: list of (ISO 639 lang code, display text in UTF-8).
    names: List[Tuple[str, str]] = field(default_factory=list)
    #: absolute timecode in ns from segment start. ``-1`` means "fill from
    #: a CHAPTER_MARK flag at mux time" (dynamic model).
    timecode: int = -1


# ----------------------------------------------------------------------
# Attachments
# ----------------------------------------------------------------------

@dataclass(slots=True)
class MkvAttachmentInfo:
    """One attached file (cover art, BD-J disc info, etc.)."""
    name: str
    mime_type: str
    data: bytes


# ----------------------------------------------------------------------
# Title-level metadata
# ----------------------------------------------------------------------

@dataclass(slots=True)
class MkvTitleNameInfo:
    """Title-level metadata for the segment Info element.

    Named to avoid colliding with ``IMkvTitleInfo`` (the interface protocol).
    """
    name: Optional[str] = None
    metadata_lang: Optional[str] = None


# ----------------------------------------------------------------------
# Format-level config / quirks
# ----------------------------------------------------------------------

@dataclass(slots=True)
class MkvProfileInfo:
    """High-level format profile — controls MKV-spec choices.

    ``timestamp_scale`` is the MKV ``TimecodeScale`` (in nanoseconds per
    container time-unit). Two useful values:

    * ``1_000_000`` (default, 1 ms units) — matches MakeMKV and most existing
      MKVs. Frame timestamps with fractional ms get rounded (half-tick up)
      during muxing. Universal player compatibility.

    * ``1`` (1 ns units) — full precision. No rounding occurs at the container
      level, so DVD/BD/UHD timestamps land at their exact values. Required for
      BD HD-audio frame boundaries that don't divide evenly into ms
      (DTS-HD MA / TrueHD have ~10.667 ms or 21.333 ms frames). Adds 1-3 bytes
      per timecode (negligible for multi-GB output). Very wide player support;
      a handful of very old / embedded players may baulk.

    Intermediate values (e.g. ``1_000`` for µs) are also valid.
    """
    version: int = 1
    use_iso639_type2T: bool = False
    set_parent_subtitle_track_as_default_if_empty: bool = False
    timestamp_scale: int = 1_000_000


@dataclass(slots=True)
class MkvDebugInfo:
    """Output-byte-affecting debug knobs.

    ``compat_flags`` (mirrors libmkv.cpp behavior):
        bit 0 (0x01): old-player compatibility — 32-bit TrackUID, 3-byte
                      display dims, no ``KaxAudioBitDepth`` for DTS, 3-byte
                      cluster size header.
        bit 1 (0x02): omit ``KaxTrackName`` even if a name was provided.
    """
    evoid: List[int] = field(default_factory=lambda: [0] * 8)
    compat_flags: int = 0


@dataclass(slots=True)
class MkvFormatInfo:
    profile: MkvProfileInfo = field(default_factory=MkvProfileInfo)
    debug: MkvDebugInfo = field(default_factory=MkvDebugInfo)


# ----------------------------------------------------------------------
# Frame
# ----------------------------------------------------------------------

@dataclass(slots=True)
class MkvChunk:
    """One frame of stream data.

    Mirrors ``IMkvChunk`` in libmkv.h. The C++ version exposed
    ``compress_start``/``compress_wait``/``compress_srcsize`` because
    compression was async on its side; in our pipeline compression is
    handled by the C shim per track. So the chunk here is just raw
    bytes + timing + flags.
    """
    data: bytes
    timecode: int            # absolute ns since title start (NOT yet scaled)
    duration: int            # ns; 0 + AUTO_DURATION flag if unknown
    flags: MkvChunkFlags = MkvChunkFlags.NONE

    # -- convenience accessors (read-only views of flags) ---------------
    @property
    def keyframe(self) -> bool:
        return bool(self.flags & MkvChunkFlags.KEYFRAME)

    @property
    def cluster_start(self) -> bool:
        return bool(self.flags & MkvChunkFlags.CLUSTER_START)

    @property
    def chapter_mark(self) -> bool:
        return bool(self.flags & MkvChunkFlags.CHAPTER_MARK)

    @property
    def discardable(self) -> bool:
        return bool(self.flags & MkvChunkFlags.DISCARDABLE)

    @property
    def old_block(self) -> bool:
        return bool(self.flags & MkvChunkFlags.OLD_BLOCK)

    @property
    def auto_duration(self) -> bool:
        return bool(self.flags & MkvChunkFlags.AUTO_DURATION)


__all__ = [
    # constants
    "TIMECODE_SCALE", "MAX_TIMECODE_SIZE_BYTES", "MAX_TIMECODE",
    "AUTO_DURATION_TIMECODE", "BAD_TIMECODE",
    # helpers
    "timecode_from_clock", "scale_timecode",
    # enums
    "MkvTrackType", "MkvChunkFlags", "MkvTrackFlags", "MkvCompression",
    # per-type info
    "VideoInfo", "AudioInfo", "SubtitleInfo",
    # extras + profile
    "CodecPrivateExtra", "MkvProfileTrackInfo",
    # main carriers
    "MkvTrackInfo", "MkvChapterInfo", "MkvAttachmentInfo", "MkvTitleNameInfo",
    "MkvProfileInfo", "MkvDebugInfo", "MkvFormatInfo",
    "MkvChunk",
]
