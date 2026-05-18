"""
MkvWriter — Python orchestrator on top of the libmkv_shim native muxer.

This is the Python port of libmkv.cpp's main mux loop, simplified for our
first DVD parity target:

Initial implementation choices (first cut — iterate later to closer parity):
  * SimpleBlocks only (no KaxBlockGroup with reference blocks). Output is
    playable; we lose some seek-precision metadata for video P/B frames.
    Adding ref blocks is an enhancement once we measure the gap.
  * Cluster boundary heuristic: new cluster every N seconds of video PTS
    OR when an IMkvFrameSource frame carries CLUSTER_START flag.
  * No lacing (each frame in its own SimpleBlock).
  * Cue point per cluster (auto-emitted by the shim on first keyframe).
  * Chapters / attachments / segment title from IMkvTitleInfo.
  * TimecodeScale from MkvFormatInfo.profile.timestamp_scale.

The pull-based contract from libmkv.h is preserved: each stream's
IMkvFrameSource yields MkvChunk objects; we walk all streams in PTS order
and dispatch frames to the shim.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .protocols import IMkvFrameSource, IMkvTitleInfo, IMkvTrack
from .types import (
    AUTO_DURATION_TIMECODE,
    MkvChunkFlags,
    MkvFormatInfo,
    MkvTrackFlags,
    MkvTrackInfo,
    MkvTrackType,
)
from . import native as _native


DEFAULT_CLUSTER_INTERVAL_NS = 1_000_000_000     # 1 second cluster cadence


@dataclass(slots=True)
class StreamWriteStats:
    bytes_written: int = 0
    frames_written: int = 0
    cluster_starts: int = 0
    error: Optional[str] = None


@dataclass(slots=True)
class RipResult:
    output_path: Path
    success: bool
    elapsed_s: float = 0.0
    track_count: int = 0
    cluster_count: int = 0
    duration_ns: int = 0
    per_stream_stats: List[StreamWriteStats] = field(default_factory=list)
    error_message: Optional[str] = None


# ----------------------------------------------------------------------
# Per-track build helpers
# ----------------------------------------------------------------------

def _track_info_to_shim(info: MkvTrackInfo) -> _native.Shim.TrackInfo:
    """Map our high-level MkvTrackInfo to the C shim's struct."""
    sti = _native.Shim.TrackInfo()
    sti.codec_id = info.codec_id.encode("utf-8") if info.codec_id else None
    sti.codec_subid = info.codec_subid.encode("utf-8") if info.codec_subid else None
    sti.lang = (info.lang or "und").encode("ascii")
    sti.name = info.name.encode("utf-8") if info.name else None

    if info.codec_private:
        sti.codec_private = info.codec_private
        sti.codec_private_size = len(info.codec_private)
    else:
        sti.codec_private = None
        sti.codec_private_size = 0

    flags = 0
    if info.mkv_flags & MkvTrackFlags.DEFAULT:
        flags |= _native.TRACK_FLAG_DEFAULT
    if info.mkv_flags & MkvTrackFlags.FORCED:
        flags |= _native.TRACK_FLAG_FORCED
    if info.mkv_flags & MkvTrackFlags.LACING:
        flags |= _native.TRACK_FLAG_LACING
    sti.mkv_flags = flags

    sti.default_duration_ns = info.default_duration
    sti.min_cache = info.min_cache

    if info.video is not None:
        sti.pixel_h = info.video.pixel_h
        sti.pixel_v = info.video.pixel_v
        sti.display_h = info.video.display_h
        sti.display_v = info.video.display_v
        sti.stereo_mode = info.video.stereo_mode
        sti.color_primaries = info.video.color_primaries
        sti.color_transfer = info.video.color_transfer
        sti.color_matrix = info.video.color_matrix
        sti.color_range = info.video.color_range
    if info.audio is not None:
        sti.sample_rate = info.audio.sample_rate
        sti.channels_count = info.audio.channels_count
        sti.bits_per_sample = info.audio.bits_per_sample
    if info.subtitle is not None:
        sti.offset_sequence_id_ref = info.subtitle.offset_sequence_id_ref

    return sti


def _track_type_to_shim(t: MkvTrackType) -> int:
    if t == MkvTrackType.VIDEO:
        return _native.TRACK_TYPE_VIDEO
    if t == MkvTrackType.AUDIO:
        return _native.TRACK_TYPE_AUDIO
    if t == MkvTrackType.SUBTITLE:
        return _native.TRACK_TYPE_SUBTITLE
    return _native.TRACK_TYPE_UNKNOWN


def _chunk_flags_to_shim(f: MkvChunkFlags) -> int:
    out = 0
    if f & MkvChunkFlags.KEYFRAME:      out |= _native.FRAME_KEYFRAME
    if f & MkvChunkFlags.CLUSTER_START: out |= _native.FRAME_CLUSTER_START
    if f & MkvChunkFlags.CHAPTER_MARK:  out |= _native.FRAME_CHAPTER_MARK
    if f & MkvChunkFlags.DISCARDABLE:   out |= _native.FRAME_DISCARDABLE
    if f & MkvChunkFlags.OLD_BLOCK:     out |= _native.FRAME_OLD_BLOCK
    if f & MkvChunkFlags.AUTO_DURATION: out |= _native.FRAME_AUTO_DURATION
    return out


# ----------------------------------------------------------------------
# MkvWriter
# ----------------------------------------------------------------------

class MkvWriter:
    """Pull-based MKV writer driven by IMkvTrack / IMkvFrameSource / IMkvTitleInfo.

    Usage:

        writer = MkvWriter(path, format_info=MkvFormatInfo())
        result = writer.write_track(track, title_info, writing_app="...")
    """

    def __init__(
        self,
        output_path: Path | str,
        format_info: Optional[MkvFormatInfo] = None,
        *,
        cluster_interval_ns: int = DEFAULT_CLUSTER_INTERVAL_NS,
        writing_app: str = "remux-toolkit",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.output_path = Path(output_path)
        self.format_info = format_info or MkvFormatInfo()
        self.cluster_interval_ns = cluster_interval_ns
        self.writing_app = writing_app
        self.logger = logger or logging.getLogger(__name__)

    # ------------------- top-level rip -------------------

    def write_track(
        self,
        track: IMkvTrack,
        title_info: Optional[IMkvTitleInfo] = None,
    ) -> RipResult:
        import time

        n_streams = track.mkv_get_stream_count()
        if n_streams < 1:
            return RipResult(self.output_path, success=False,
                             error_message="no streams in IMkvTrack")

        try:
            shim = _native.load_shim()
        except _native.ShimUnavailable as e:
            return RipResult(self.output_path, success=False,
                             error_message=f"native shim unavailable: {e}")

        # --- collect per-stream track info ---
        track_infos: List[MkvTrackInfo] = []
        for i in range(n_streams):
            info = MkvTrackInfo()
            src = track.mkv_get_stream(i)
            src.update_track_info(info)
            track_infos.append(info)

        # --- open writer + write headers ---
        w = shim.open(str(self.output_path).encode("utf-8"),
                      self.writing_app.encode("utf-8"))
        per_stats = [StreamWriteStats() for _ in range(n_streams)]
        cluster_count = 0
        max_duration_ns = 0
        t0 = time.time()
        try:
            ts_scale = self.format_info.profile.timestamp_scale
            shim.set_timestamp_scale(w, ts_scale)

            # MKV spec: block-relative timecodes are int16_t (signed, ±32767).
            # Cluster span = max_block_relative * timestamp_scale ns.
            # Clamp the requested cluster interval so blocks never overflow.
            # Leave ~5% safety margin.
            # int16_t block-relative timecode is ±32,767 units. We
            # leave a margin so half-tick rounding in the shim
            # (``scale_ns`` adds scale/2 before division) can't push
            # us over even on the worst input. 28,000 was empirically
            # chosen to clear the 1µs scale crash on ANGEL T7 where
            # AC3 PES boundaries land near ~32 ms.
            max_safe_cluster_ns = int(28000 * ts_scale)
            effective_cluster_interval_ns = min(
                self.cluster_interval_ns, max_safe_cluster_ns,
            )

            if title_info is not None:
                tname = title_info.get_mkv_title_info().name
                if tname:
                    shim.set_title(w, tname.encode("utf-8"))

            shim_tracks: List[int] = []
            for i, info in enumerate(track_infos):
                sti = _track_info_to_shim(info)
                handle = shim.add_track(w, _track_type_to_shim(info.type), sti)
                shim_tracks.append(handle)

            shim.write_headers(w)

            # --- chapter atoms (static; passed up front) ---
            if title_info is not None:
                for ci in range(title_info.get_chapter_count()):
                    chap = title_info.get_chapter_info(ci)
                    name = ""
                    lang = "und"
                    if chap.names:
                        lang, name = chap.names[0]
                    tc_start = max(0, chap.timecode)
                    shim.add_chapter(
                        w, tc_start, -1,  # end auto-filled at finalize
                        name.encode("utf-8"),
                        lang.encode("ascii"),
                    )

            # --- attachments ---
            if title_info is not None:
                for ai in range(title_info.get_attachment_count()):
                    att = title_info.get_attachment_info(ai)
                    shim.add_attachment(w,
                                        att.name.encode("utf-8"),
                                        att.mime_type.encode("ascii"),
                                        att.data)

            # --- mux loop ---
            # Strategy: round-robin streams; for each, drain frames in PTS
            # order. We start a new cluster every cluster_interval_ns OR
            # when a frame has CLUSTER_START explicitly set.
            cluster_start_tc = -1  # ns; -1 means no cluster open

            # Pre-fetch one frame per stream to bootstrap.
            for i in range(n_streams):
                track.mkv_get_stream(i).fetch_frames(1, force=True)

            while True:
                # Pick the stream with the smallest next-frame timecode.
                best_i = -1
                best_tc = None
                for i in range(n_streams):
                    src = track.mkv_get_stream(i)
                    if src.get_available_frames_count() == 0:
                        if src.source_finished():
                            continue
                        src.fetch_frames(4, force=True)
                        if src.get_available_frames_count() == 0:
                            continue
                    tc = src.peek_frame(0).timecode
                    if best_tc is None or tc < best_tc:
                        best_tc = tc
                        best_i = i

                if best_i < 0:
                    break  # all sources drained

                src = track.mkv_get_stream(best_i)
                chunk = src.peek_frame(0)

                # Decide cluster boundary.
                want_new_cluster = False
                rel = chunk.timecode - cluster_start_tc
                if cluster_start_tc < 0:
                    want_new_cluster = True
                elif bool(chunk.flags & MkvChunkFlags.CLUSTER_START):
                    want_new_cluster = True
                elif rel < 0 or rel >= max_safe_cluster_ns:
                    # Force a new cluster when the block-relative timecode
                    # would overflow int16_t in either direction. Negative
                    # ``rel`` shouldn't happen with our DVD producer (we
                    # pick min-timecode each iteration), but defensive
                    # check is cheap and protects against future producers
                    # with non-monotonic streams.
                    want_new_cluster = True
                elif rel >= effective_cluster_interval_ns:
                    # Preferred boundary: only video starts a cluster for
                    # best seek-precision cue placement.
                    if track_infos[best_i].type == MkvTrackType.VIDEO:
                        want_new_cluster = True

                if want_new_cluster:
                    if cluster_start_tc >= 0:
                        shim.end_cluster(w)
                    shim.start_cluster(w, chunk.timecode)
                    cluster_start_tc = chunk.timecode
                    cluster_count += 1
                    per_stats[best_i].cluster_starts += 1

                # Emit chapter mark if flagged (chapters were declared up
                # front; this flag is a no-op for our static-chapter model).
                # Future enhancement: dynamic chapter atoms here.

                # Add block. Treat 0-byte data as a benign skip (some
                # demuxers emit empty marker chunks).
                if chunk.data:
                    shim_flags = _chunk_flags_to_shim(chunk.flags)
                    # Subtitles need a KaxBlockGroup so we can carry an
                    # explicit BlockDuration — SimpleBlocks rely on the
                    # track's DefaultDuration which is wrong for subs
                    # (each event has its own display time, computed from
                    # SP_DCSQ or lookahead by the producer).
                    needs_duration = (
                        track_infos[best_i].type == MkvTrackType.SUBTITLE
                        and chunk.duration > 0
                    )
                    if needs_duration:
                        shim.add_block_group(
                            w, shim_tracks[best_i],
                            chunk.data, len(chunk.data),
                            chunk.timecode, chunk.duration,
                            shim_flags,
                        )
                    else:
                        shim.add_simple_block(
                            w, shim_tracks[best_i],
                            chunk.data, len(chunk.data),
                            chunk.timecode,
                            shim_flags,
                        )
                    per_stats[best_i].bytes_written += len(chunk.data)
                    per_stats[best_i].frames_written += 1

                end_tc = chunk.timecode + max(0, chunk.duration)
                if end_tc > max_duration_ns:
                    max_duration_ns = end_tc

                src.pop_frame()

            # Feed per-track statistics to the shim for KaxTags emission
            # at finalize. mkvmerge / MakeMKV write BPS / DURATION /
            # NUMBER_OF_FRAMES / NUMBER_OF_BYTES via this mechanism.
            for i, st in enumerate(per_stats):
                if st.frames_written > 0:
                    shim.set_track_stats(
                        w, shim_tracks[i],
                        st.bytes_written, st.frames_written,
                        max_duration_ns,
                    )

            shim.finalize(w, max_duration_ns)
        except Exception as e:
            shim.close(w)
            return RipResult(
                self.output_path,
                success=False,
                elapsed_s=time.time() - t0,
                track_count=n_streams,
                cluster_count=cluster_count,
                duration_ns=max_duration_ns,
                per_stream_stats=per_stats,
                error_message=f"{type(e).__name__}: {e}",
            )

        shim.close(w)

        return RipResult(
            self.output_path,
            success=True,
            elapsed_s=time.time() - t0,
            track_count=n_streams,
            cluster_count=cluster_count,
            duration_ns=max_duration_ns,
            per_stream_stats=per_stats,
        )


__all__ = ["MkvWriter", "RipResult", "StreamWriteStats"]
