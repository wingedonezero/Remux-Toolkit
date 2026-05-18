"""MKV muxer foundations.

``types`` carries dataclasses and enums that mirror MakeMKV's libmkv.h data
structures. ``protocols`` carries the Python ``Protocol`` interfaces that
disc-format adapters implement to feed the muxer.

This package is import-side-effect-free and stdlib-only; downstream
muxer implementations (e.g. the libmatroska C-shim binding) will live
alongside it in submodules.
"""

from .protocols import (
    IMkvFrameSource,
    IMkvTitleInfo,
    IMkvTrack,
    IMkvWriteTarget,
)
from .writer import MkvWriter, RipResult, StreamWriteStats
from .types import (
    AUTO_DURATION_TIMECODE,
    AudioInfo,
    BAD_TIMECODE,
    CodecPrivateExtra,
    MAX_TIMECODE,
    MAX_TIMECODE_SIZE_BYTES,
    MkvAttachmentInfo,
    MkvChapterInfo,
    MkvChunk,
    MkvChunkFlags,
    MkvCompression,
    MkvDebugInfo,
    MkvFormatInfo,
    MkvProfileInfo,
    MkvProfileTrackInfo,
    MkvTitleNameInfo,
    MkvTrackFlags,
    MkvTrackInfo,
    MkvTrackType,
    SubtitleInfo,
    TIMECODE_SCALE,
    VideoInfo,
    scale_timecode,
    timecode_from_clock,
)


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
    # protocols
    "IMkvWriteTarget", "IMkvFrameSource", "IMkvTrack", "IMkvTitleInfo",
    # writer
    "MkvWriter", "RipResult", "StreamWriteStats",
]
