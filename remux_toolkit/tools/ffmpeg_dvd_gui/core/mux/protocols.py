"""
Protocol definitions for the MakeMKV-aligned MKV muxer.

These mirror libmkv.h's C++ abstract interfaces in Pythonic form. All are
``@runtime_checkable`` so ``isinstance()`` works for safety checks, but
implementations don't need to inherit — duck typing suffices.

C++ ↔ Python mapping:
    IMkvWriteTarget  → IMkvWriteTarget
    IMkvFrameSource  → IMkvFrameSource
    IMkvTrack        → IMkvTrack
    IMkvTitleInfo    → IMkvTitleInfo
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .types import (
    MkvAttachmentInfo,
    MkvChapterInfo,
    MkvChunk,
    MkvTitleNameInfo,
    MkvTrackInfo,
)


@runtime_checkable
class IMkvWriteTarget(Protocol):
    """Sink for serialized MKV bytes.

    The muxer calls ``write`` for sequential appends and ``overwrite`` to
    update earlier-written values (segment Duration, segment size, seek
    head positions, AUTO_DURATION fill-ins).
    """

    def write(self, data: bytes) -> bool:
        """Append ``data`` at the current write cursor. Returns ``False`` on error."""
        ...

    def overwrite(self, offset: int, data: bytes) -> bool:
        """Write ``data`` at an absolute ``offset`` without moving the write
        cursor. Returns ``False`` on error."""
        ...


@runtime_checkable
class IMkvFrameSource(Protocol):
    """Pull-based frame producer; one per stream.

    The muxer asks for frames via ``fetch_frames`` (which may block on I/O),
    peeks at them via ``peek_frame``, then consumes them via ``pop_frame``.
    ``source_finished`` signals that no further frames will ever arrive
    on this stream.
    """

    def fetch_frames(self, count: int, force: bool) -> bool:
        """Try to make ``count`` frames available. If ``force`` is ``True``,
        do whatever I/O is necessary (i.e. block on a read); otherwise this
        is best-effort. Returns ``False`` on hard error — NOT on EOF, which
        is signalled via ``source_finished``."""
        ...

    def get_available_frames_count(self) -> int:
        """Number of frames currently buffered and ready to peek/pop."""
        ...

    def peek_frame(self, index: int) -> MkvChunk:
        """Look at the frame at position ``index`` (0 = next frame). Does
        not consume. Caller must have ensured at least ``index+1`` frames
        are available via ``fetch_frames``."""
        ...

    def pop_frame(self) -> None:
        """Consume the head frame. Safe to call only when at least one
        frame is available."""
        ...

    def source_finished(self) -> bool:
        """``True`` if the underlying stream has reached EOF and no further
        calls to ``fetch_frames`` will produce new frames."""
        ...

    def update_track_info(self, info: MkvTrackInfo) -> bool:
        """Allow the source to amend track metadata at finalize time. Some
        codecs only know ``codec_private`` after decoding a frame or two.
        Most sources can return ``False`` to skip — the muxer will keep the
        original info."""
        ...


@runtime_checkable
class IMkvTrack(Protocol):
    """Container of streams for one title (the input side of the muxer)."""

    def mkv_get_stream_count(self) -> int:
        ...

    def mkv_get_stream(self, index: int) -> IMkvFrameSource:
        ...


@runtime_checkable
class IMkvTitleInfo(Protocol):
    """Title-level metadata: chapters, attachments, and display name."""

    def get_chapter_count(self) -> int:
        ...

    def get_chapter_info(self, chapter_id: int) -> MkvChapterInfo:
        ...

    def get_mkv_title_info(self) -> MkvTitleNameInfo:
        ...

    def get_attachment_count(self) -> int:
        ...

    def get_attachment_info(self, attachment_id: int) -> MkvAttachmentInfo:
        ...


__all__ = [
    "IMkvWriteTarget",
    "IMkvFrameSource",
    "IMkvTrack",
    "IMkvTitleInfo",
]
