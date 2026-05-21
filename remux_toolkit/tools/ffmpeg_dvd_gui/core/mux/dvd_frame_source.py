"""
DVD adapter for the IMkvFrameSource / IMkvTrack / IMkvTitleInfo protocols.

Bridges our existing DVD demuxer stack (libdvdread + CellReader + ps_walker)
to the MkvWriter mux loop.

Architecture:

    open_dvd_title()
        ├─ resolve title → (vts_no, pgc_no)
        ├─ enumerate streams from VTSI + PGC
        ├─ pre-scan first PTS per stream (for timecode normalization)
        ├─ extract chapter list
        ├─ spawn producer thread:
        │       CellReader.iter_sectors()
        │       → ps_walker.iter_es_payloads()
        │       → per-stream MkvChunk queues
        └─ return DvdTrack + DvdTitleInfo

The producer thread walks the title once; each stream's chunks land in its
own bounded queue. MkvWriter pulls them via the IMkvFrameSource protocol
and drives the libmkv_shim backend.

This implementation pre-walks (streams the demux), which scales to
arbitrarily large titles — queues bound memory to ~64 frames per stream.
"""
from __future__ import annotations

import array
import logging
import queue
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ...bindings import libdvdread as dr
from ..demux.cell_reader import CellReader
from ..demux.chapters import extract_chapters
from ..demux.ps_walker import (
    iter_es_payloads, stream_key, stream_kind, STREAM_MPEG_VIDEO,
)
from ..demux.subpicture import (
    parse_subpic, build_vobsub_idx, DEFAULT_SUBPIC_DURATION_TICKS,
)
from ..analysis.cell_trim import (
    TrimDecision, cell_metadata_from_pgc, cells_for_angle, count_angles,
)
from ..demux.picturizers import mpeg2 as mpeg2_picturizer
# Re-export so existing callers (and tests) can still import these names
# from this module while the picturizer lives next door.
picture_coding_type = mpeg2_picturizer.picture_coding_type
PCT_FORBIDDEN = mpeg2_picturizer.PCT_FORBIDDEN
PCT_I = mpeg2_picturizer.PCT_I
PCT_P = mpeg2_picturizer.PCT_P
PCT_B = mpeg2_picturizer.PCT_B
PCT_D = mpeg2_picturizer.PCT_D
from ..orchestrator import (
    _resolve_title_to_pgc, _enumerate_streams, _prescan_first_pts,
)
from .types import (
    AudioInfo, MkvChapterInfo, MkvChunk, MkvChunkFlags, MkvTitleNameInfo,
    MkvTrackFlags, MkvTrackInfo, MkvTrackType, SubtitleInfo, VideoInfo,
)


# Picture size lookup matches libdvdread's VideoAttr.picture_size field
# (NTSC: 720x480/704x480/352x480/352x240; PAL: 720x576/704x576/352x576/352x288).
_PICTURE_SIZE_NTSC = {0: (720, 480), 1: (704, 480), 2: (352, 480), 3: (352, 240)}
_PICTURE_SIZE_PAL  = {0: (720, 576), 1: (704, 576), 2: (352, 576), 3: (352, 288)}


def _video_size(video_attr) -> tuple[int, int]:
    """Resolve (width, height) from libdvdread's VideoAttr."""
    if int(video_attr.video_format) == 0:
        return _PICTURE_SIZE_NTSC.get(int(video_attr.picture_size), (720, 480))
    return _PICTURE_SIZE_PAL.get(int(video_attr.picture_size), (720, 576))


def _pgc_palette_bytes(pgc) -> bytes:
    """PGC.palette (16 × u32) → 64-byte blob in [reserved, Y, Cr, Cb] layout
    expected by ``core.demux.subpicture.build_vobsub_idx``."""
    return b"".join(int(pgc.palette[i]).to_bytes(4, "big") for i in range(16))


_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# PTS → ns conversion
# ---------------------------------------------------------------------

def _pts_to_ns(pts_90khz: int) -> int:
    """DVD PTS (90 kHz ticks) → nanoseconds. Exact integer math."""
    return (pts_90khz * 100_000) // 9


# ---------------------------------------------------------------------
# STC discontinuity rebasing
# ---------------------------------------------------------------------

@dataclass
class _PtsRebaser:
    """Maps raw PES PTSes to a monotonic output timeline, handling STC
    resets at DVD-Video cell boundaries.

    A DVD cell can declare ``stc_discontinuity=True``, meaning the System
    Time Clock resets at the cell boundary. After such a reset, PTSes in
    the new cell are anchored to a different master clock — naively
    subtracting ``title_first_pts`` produces a garbage timestamp.

    The fix: at each STC boundary, re-anchor on the new cell's first
    PES PTS, and reset the cumulative-output-time to the sum of all prior
    cells' (IFO-declared) durations. Within an STC era, the math reduces
    to the pre-existing ``(raw - base)`` subtraction.

    Single instance per producer, shared across streams: STC is a
    PROGRAM STREAM-level clock, not per-stream, so a single rebaser keeps
    inter-stream offsets (audio-video skew) preserved.
    """
    base_ticks: int                         # subtracted from raw PTS in current era
    cumulative_ns: int = 0                  # added to (raw - base) ns post-conversion
    pending_cumulative_ticks: Optional[int] = None  # queued by STC boundary; consumed
                                                     # on the next PTS-bearing PES

    def queue_rebase(self, cumulative_ticks_at_cell_start: int) -> None:
        """Note that the next PTS-bearing PES will reopen the rebaser
        anchored at the given cumulative-ticks position."""
        self.pending_cumulative_ticks = cumulative_ticks_at_cell_start

    def adjust(self, raw_pts_ticks: int) -> int:
        """Map a raw PES PTS (90 kHz ticks) → output timecode (ns).

        On the first call after ``queue_rebase``, the raw PTS becomes the
        new era's base and the queued cumulative becomes the era's offset.
        """
        if self.pending_cumulative_ticks is not None:
            self.base_ticks = raw_pts_ticks
            self.cumulative_ns = _pts_to_ns(self.pending_cumulative_ticks)
            self.pending_cumulative_ticks = None
        rel_ticks = max(0, raw_pts_ticks - self.base_ticks)
        return _pts_to_ns(rel_ticks) + self.cumulative_ns


def _codec_id_for(codec_name: str) -> str:
    """Map our internal codec name → Matroska codec ID.

    LPCM note: DVD-Video LPCM is **big-endian** on the disc, so the
    spec-correct Matroska codec_id is ``A_PCM/INT/BIG``. MakeMKV
    nevertheless stores LPCM byte-swapped as ``A_PCM/INT/LIT``
    (decoded audio is identical; container bytes differ). To match
    MakeMKV's container bytes exactly, we use LIT here AND byte-swap
    the samples in the producer before queueing — see
    ``_lpcm_be_to_le``.
    """
    return {
        "mpeg2video": "V_MPEG2",
        "ac3":        "A_AC3",
        "dts":        "A_DTS",
        "mp2":        "A_MPEG/L2",
        "mp1":        "A_MPEG/L2",
        "lpcm":       "A_PCM/INT/LIT",
        "subpicture": "S_VOBSUB",
    }.get(codec_name, "")


def _lpcm_be_to_le(data: bytes, *, bits_per_sample: int) -> bytes:
    """Byte-swap DVD-Video LPCM samples from disc-original big-endian
    to little-endian for storage as Matroska ``A_PCM/INT/LIT`` blocks.

    Matches MakeMKV's storage convention. ``bits_per_sample`` is 16 or
    24 in practice on DVD-Video; 20-bit LPCM (rare) is currently not
    handled by the upstream stream-plan path.

    Trailing bytes that don't form a complete sample (shouldn't happen
    on well-authored discs) are passed through unswapped as a defensive
    measure rather than dropped.
    """
    if bits_per_sample == 16:
        pad = len(data) % 2
        a = array.array("h", data[: len(data) - pad] if pad else data)
        a.byteswap()
        return a.tobytes() + (data[-pad:] if pad else b"")
    if bits_per_sample == 24:
        n = (len(data) // 3) * 3
        ba = bytearray(data[:n])
        for i in range(0, n, 3):
            ba[i], ba[i + 2] = ba[i + 2], ba[i]
        return bytes(ba) + data[n:]
    return data


# DVD-Video audio_attr.code_extension → human-readable suffix for the MKV
# track name. Values per DVD-Video Part 3 §A.5.4.4.4.
_AUDIO_CODE_EXT_SUFFIX = {
    # 0 = unspecified, 1 = normal: no suffix (these are the main tracks)
    2: "Visually Impaired",
    3: "Director's Commentary",
    4: "Alternate Commentary",
}

# DVD-Video subp_attr.code_extension → human-readable suffix. Values per
# DVD-Video Part 3 §A.5.4.5.5.
_SUB_CODE_EXT_SUFFIX = {
    # 0 = unspecified, 1 = normal CC for big screen: no suffix
    2: "Large CC",
    3: "Children",
    5: "Normal CC",
    6: "Large CC for Small Screen",
    9: "Forced",
    13: "Director's Commentary",
    14: "Director's Notes",
}


def _framerate_to_default_duration_ns(framerate_str: str) -> int:
    """ffmpeg-style framerate string ("30000/1001" or "25") → ns per frame."""
    if not framerate_str:
        return 0
    if "/" in framerate_str:
        n, d = framerate_str.split("/", 1)
        try:
            return int(int(d) * 1_000_000_000 // int(n))
        except (ValueError, ZeroDivisionError):
            return 0
    try:
        f = float(framerate_str)
        return int(1_000_000_000 / f) if f > 0 else 0
    except ValueError:
        return 0


# picture_coding_type and PCT_* constants live in
# core.demux.picturizers.mpeg2; re-exported above for backward compat.


# ---------------------------------------------------------------------
# IMkvFrameSource implementations
# ---------------------------------------------------------------------

class DvdFrameSource:
    """Drains a bounded queue of MkvChunks produced by a background thread."""

    def __init__(self, q: queue.Queue, track_info: MkvTrackInfo,
                 fetch_timeout_s: float = 30.0) -> None:
        self._q = q
        self._track_info = track_info
        self._buffer: deque = deque()
        self._finished = False
        self._fetch_timeout_s = fetch_timeout_s

    def fetch_frames(self, count: int, force: bool) -> bool:
        if self._finished and not self._buffer:
            return True
        timeout = self._fetch_timeout_s if force else 0.0
        try:
            while len(self._buffer) < count:
                try:
                    item = self._q.get(timeout=timeout) if timeout > 0 else self._q.get_nowait()
                except queue.Empty:
                    return True  # nothing more right now; caller can retry
                if item is None:
                    self._finished = True
                    return True
                self._buffer.append(item)
        except Exception as e:
            _logger.exception("DvdFrameSource.fetch_frames: %s", e)
            return False
        return True

    def get_available_frames_count(self) -> int:
        return len(self._buffer)

    def peek_frame(self, index: int) -> MkvChunk:
        return self._buffer[index]

    def pop_frame(self) -> None:
        if self._buffer:
            self._buffer.popleft()

    def source_finished(self) -> bool:
        return self._finished and not self._buffer

    def update_track_info(self, info: MkvTrackInfo) -> bool:
        ti = self._track_info
        info.type = ti.type
        info.codec_id = ti.codec_id
        info.codec_subid = ti.codec_subid
        info.lang = ti.lang
        info.name = ti.name
        info.codec_private = ti.codec_private
        info.codec_private_extra = list(ti.codec_private_extra)
        info.header_comp_data = ti.header_comp_data
        info.mkv_flags = ti.mkv_flags
        info.default_duration = ti.default_duration
        info.dts_adjust = ti.dts_adjust
        info.bitrate = ti.bitrate
        info.min_cache = ti.min_cache
        info.video = ti.video
        info.audio = ti.audio
        info.subtitle = ti.subtitle
        return True


class DvdTrack:
    """IMkvTrack — fixed list of DvdFrameSources for one title."""

    def __init__(self, sources: list[DvdFrameSource], producer: threading.Thread) -> None:
        self._sources = sources
        self._producer = producer

    def mkv_get_stream_count(self) -> int:
        return len(self._sources)

    def mkv_get_stream(self, index: int) -> DvdFrameSource:
        return self._sources[index]

    def producer_thread(self) -> threading.Thread:
        return self._producer


class DvdTitleInfo:
    """IMkvTitleInfo — chapters + display name for one DVD title."""

    def __init__(self, name: str, chapters: list[MkvChapterInfo]) -> None:
        self._title = MkvTitleNameInfo(name=name)
        self._chapters = chapters

    def get_chapter_count(self) -> int:
        return len(self._chapters)

    def get_chapter_info(self, idx: int) -> MkvChapterInfo:
        return self._chapters[idx]

    def get_mkv_title_info(self) -> MkvTitleNameInfo:
        return self._title

    def get_attachment_count(self) -> int:
        return 0

    def get_attachment_info(self, idx: int) -> "MkvAttachmentInfo":
        raise IndexError(idx)


# ---------------------------------------------------------------------
# Build helpers
# ---------------------------------------------------------------------

def _build_track_info(plan, framerate_str: str, *, is_first_video: bool,
                      is_first_audio: bool,
                      subpic_codec_private: Optional[bytes] = None,
                      ) -> Optional[MkvTrackInfo]:
    """Map a _StreamPlan → MkvTrackInfo. Returns None for unsupported codecs."""
    codec_id = _codec_id_for(plan.codec_name)
    if not codec_id:
        return None

    ti = MkvTrackInfo()
    ti.codec_id = codec_id
    ti.lang = plan.language or "und"
    ti.name = plan.title or None

    if plan.is_video:
        ti.type = MkvTrackType.VIDEO
        # DVD-Video pixel + display dims and BT.601 colour (computed by
        # _enumerate_streams from VideoAttr). Threaded via _StreamPlan.
        ti.video = VideoInfo(
            pixel_h=plan.pixel_w or 720,
            pixel_v=plan.pixel_h or 480,
            display_h=plan.display_w or plan.pixel_w or 720,
            display_v=plan.display_h or plan.pixel_h or 480,
            fps_n=0, fps_d=1,
            color_primaries=plan.color_primaries,
            color_transfer=plan.color_transfer,
            color_matrix=plan.color_matrix,
            color_range=plan.color_range,
        )
        if framerate_str and "/" in framerate_str:
            n, d = framerate_str.split("/", 1)
            try:
                ti.video.fps_n = int(n)
                ti.video.fps_d = int(d)
            except ValueError:
                pass
        ti.default_duration = _framerate_to_default_duration_ns(framerate_str)
        if is_first_video:
            ti.mkv_flags = MkvTrackFlags.DEFAULT
    elif plan.codec_name in ("ac3", "dts", "mp2", "mp1", "lpcm"):
        ti.type = MkvTrackType.AUDIO
        # Real codec params from libdvdread's audio_attr (threaded via
        # _StreamPlan). DTS gets a zeroed bit-depth: per MakeMKV's libmkv
        # OLD-PLAYER quirk (LIBMKV_IMPL_NOTES.md §7.2), KaxAudioBitDepth
        # for DTS confuses some players. We always omit it for DTS, since
        # the value isn't meaningful for lossy compressed audio anyway.
        bits = plan.bits_per_sample if plan.codec_name == "lpcm" else 0
        ti.audio = AudioInfo(
            sample_rate=plan.sample_rate or 48000,
            channels_count=plan.channels or 2,
            bits_per_sample=bits,
        )
        # Audio code_extension: per DVD spec — 3 / 4 = director's commentary,
        # 2 = visually-impaired. Override the track name and skip the
        # default-flag for these (the main audio track stays the default).
        suffix = _AUDIO_CODE_EXT_SUFFIX.get(plan.code_extension, "")
        if suffix:
            ti.name = f"{ti.name or 'Audio'} ({suffix})" if ti.name else suffix
        # First-audio default only when this is a "main" audio
        # (code_extension 0 or 1). Commentary/VI tracks are never default.
        if is_first_audio and plan.code_extension in (0, 1):
            ti.mkv_flags = MkvTrackFlags.DEFAULT
    elif plan.codec_name == "subpicture":
        ti.type = MkvTrackType.SUBTITLE
        ti.subtitle = SubtitleInfo(offset_sequence_id_ref=0)
        # MKV's S_VOBSUB codec_private is the textual .idx blob with the
        # 16-entry global palette + screen size. Without it most players
        # render subs in a default palette (greyscale-ish). Caller builds
        # via core.demux.subpicture.build_vobsub_idx().
        if subpic_codec_private:
            ti.codec_private = subpic_codec_private
        # Subpicture code_extension: 9 = forced, 13 = director's commentary,
        # 14 = director's notes. Forced subs get the FORCED MKV flag so
        # players show them even when subs are nominally off.
        if plan.code_extension == 9:
            ti.mkv_flags |= MkvTrackFlags.FORCED
        suffix = _SUB_CODE_EXT_SUFFIX.get(plan.code_extension, "")
        if suffix:
            ti.name = f"{ti.name or 'Subtitle'} ({suffix})" if ti.name else suffix
    else:
        return None
    return ti


# ---------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------

def open_dvd_title(
    disc: object,
    title_num: int,
    *,
    title_name: Optional[str] = None,
    include_subpictures: bool = False,
    queue_maxsize: int = 64,
    start_producer: bool = True,
    trim: Optional[TrimDecision] = None,
    angle: int = 1,
) -> tuple[DvdTrack, DvdTitleInfo]:
    """Pre-walk a DVD title; return IMkvTrack + IMkvTitleInfo for muxing.

    The producer thread starts immediately (set ``start_producer=False``
    for metadata-only inspection without disc reads). It exits cleanly
    when all sectors are consumed; each stream's queue receives a
    ``None`` sentinel at EOF.

    ``disc`` is an open libdvdread handle (from ``dr.open_dvd``).

    When ``start_producer=False`` callers must call ``track.producer_thread().start()``
    themselves to begin demuxing; the streams' ``source_finished()`` will
    return False until then (no sentinel pushed).

    ``trim`` is an optional ``TrimDecision`` (from ``core.analysis.cell_trim``).
    When provided, the producer skips ``trim.start_trim`` cells from the
    start and ``trim.end_trim`` cells from the end of the PGC. Default
    None = read all cells (matches behaviour before Chunk 3).
    """
    vts_no, pgc_no = _resolve_title_to_pgc(disc, title_num)
    plans, _color, framerate = _enumerate_streams(
        disc, vts_no, pgc_no, include_subpictures=include_subpictures,
    )
    if not plans:
        raise RuntimeError(f"No streams found for title {title_num}")

    # First PTS per stream (90 kHz ticks) — used to normalize timecodes.
    plan_keys = {p.key for p in plans}
    first_pts = _prescan_first_pts(disc, title_num, vts_no, pgc_no, plan_keys)
    title_first_pts = min(first_pts.values()) if first_pts else 0

    # Chapters → MkvChapterInfo.
    chapters_list = extract_chapters(disc, title_num, vts_no=vts_no, pgc_no=pgc_no)
    mkv_chapters: list[MkvChapterInfo] = []
    for ch in chapters_list:
        mkv_chapters.append(MkvChapterInfo(
            names=[("und", ch.title or f"Chapter {ch.index:02d}")],
            timecode=int(ch.start_seconds * 1_000_000_000),
        ))

    # For subpicture tracks we need a single CodecPrivate blob containing
    # the global palette + screen size (mplayer-style .idx). All sub tracks
    # on the same disc share the PGC palette, so we build it once here.
    subpic_codec_private: Optional[bytes] = None
    if include_subpictures:
        with dr.open_ifo(disc, vts_no) as vts:
            m = vts.contents.vtsi_mat.contents
            pgc = vts.contents.vts_pgcit.contents.pgci_srp[pgc_no - 1].pgc.contents
            sw, sh = _video_size(m.vts_video_attr)
            subpic_codec_private = build_vobsub_idx(
                _pgc_palette_bytes(pgc), screen_w=sw, screen_h=sh,
            )

    # Build per-stream track info + queue + DvdFrameSource.
    queues_by_key: dict[tuple, queue.Queue] = {}
    sources: list[DvdFrameSource] = []
    plan_for_key: dict[tuple, object] = {}
    kind_by_key: dict[tuple, MkvTrackType] = {}

    is_first_video = True
    is_first_audio = True
    for plan in plans:
        ti = _build_track_info(
            plan, framerate,
            is_first_video=is_first_video,
            is_first_audio=is_first_audio,
            subpic_codec_private=subpic_codec_private,
        )
        if ti is None:
            _logger.warning("skipping unsupported codec: %s (key=%s)",
                            plan.codec_name, plan.key)
            continue
        if plan.is_video:
            is_first_video = False
        elif ti.type == MkvTrackType.AUDIO:
            is_first_audio = False

        q: queue.Queue = queue.Queue(maxsize=queue_maxsize)
        queues_by_key[plan.key] = q
        plan_for_key[plan.key] = plan
        sources.append(DvdFrameSource(q, ti))
        kind_by_key[plan.key] = ti.type

    # Default-duration lookup per stream key (for video PTS extrapolation).
    default_duration_by_key: dict[tuple, int] = {}
    for plan, src in zip(plans, sources):
        # src.update_track_info would copy; we already have it on the source
        default_duration_by_key[plan.key] = src._track_info.default_duration

    # Producer thread: walks ES payloads, splits video into per-picture
    # blocks, coalesces multi-PES subpicture units, and dispatches to
    # per-stream queues.
    #
    # MPEG-PS framing:
    #   * Each video PES with PTS marks the START of a "picture group" —
    #     multiple MPEG-2 pictures whose bytes follow, delimited by
    #     picture_start_code (0x00000100). Only the first picture has the
    #     PES PTS; subsequent pictures' timecodes are extrapolated via
    #     the track's default_duration (CFR assumption — fine for DVD).
    #   * Video PES with no PTS = continuation of the prior picture group's
    #     last picture (rare on DVD, but happens at VOBU boundaries).
    #   * Audio PES are self-contained: one chunk = one PES.
    #   * Subpicture PES with PTS = start of a new SPU (display start
    #     time). Continuation PES (no PTS) extend the current SPU. When a
    #     new SPU arrives, we parse the previous one's SP_DCSQ for
    #     duration; if STP_DSP wasn't found, we fall back to the gap to
    #     the next SPU. Last sub at EOF gets DEFAULT_SUBPIC_DURATION_TICKS.
    def producer() -> None:
        # Per-video-stream buffered "picture group": data bytes since last
        # PTS-bearing PES + the PTS that group starts at.
        group: dict = {}  # key -> {"pts_ns": int, "bufs": list[bytes]}
        # Per-subpicture-stream buffered SPU accumulator: similar to video
        # group, but stores raw PTS in 90 kHz ticks (we re-parse the SPU
        # to compute duration before emitting).
        sub_accum: dict = {}  # key -> {"pts": int (90 kHz), "bufs": list[bytes]}
        # Per-subpicture-stream "pending event" awaiting either a lookahead
        # next-event PTS (to bound the duration) or EOF.
        pending_sub: dict = {}  # key -> {"timecode_ns": int, "data": bytes,
                                #         "duration_ticks": int, "pts": int}
        sentinel_sent = False

        def _emit_sub(key, next_pts_or_none):
            """Emit a previously parsed sub event with its final duration.
            If duration_ticks was 0 (no STP_DSP) and we have a next PTS,
            use the gap as the duration; otherwise use the default."""
            ev = pending_sub.pop(key, None)
            if ev is None:
                return
            if ev["duration_ticks"] <= 0:
                if next_pts_or_none is not None:
                    gap = max(1, next_pts_or_none - ev["pts"])
                    ev["duration_ticks"] = gap
                else:
                    ev["duration_ticks"] = DEFAULT_SUBPIC_DURATION_TICKS
            duration_ns = _pts_to_ns(ev["duration_ticks"])
            q = queues_by_key.get(key)
            if q is None:
                return
            q.put(MkvChunk(
                data=ev["data"],
                timecode=ev["timecode_ns"],
                duration=duration_ns,
                flags=MkvChunkFlags.KEYFRAME,
            ))

        def _flush_sub_accum(key):
            """Parse the accumulated SPU and stash as pending (awaits next
            sub's PTS for STP-less duration lookahead)."""
            cur = sub_accum.pop(key, None)
            if cur is None:
                return
            data = b"".join(cur["bufs"])
            if not data:
                return
            ev = parse_subpic(data, cur["pts"])
            if ev is None:
                return
            # Flush whatever was already pending (use this event's PTS
            # as the lookahead bound).
            _emit_sub(key, cur["pts"])
            pending_sub[key] = {
                "timecode_ns": rebaser.adjust(cur["pts"]),
                "data": ev.data,
                "duration_ticks": ev.duration_ticks,
                "pts": cur["pts"],
            }

        def _emit_pictures(k):
            """Drain the buffered PES group through the MPEG-2 picturizer
            and push each emitted chunk to the stream's queue."""
            g = group.pop(k, None)
            if g is None:
                return
            data = b"".join(g["bufs"])
            if not data:
                return
            dd = default_duration_by_key.get(k, 33_366_667)
            q = queues_by_key[k]
            for chunk in mpeg2_picturizer.emit_pictures(
                data, g["pts_ns"], dd,
            ):
                q.put(chunk)

        # Always load cell metadata + raw STC flag table. Cell durations
        # feed the STC-discontinuity rebase anchor; trim/angle filtering
        # is computed in the same pass.
        with dr.open_ifo(disc, vts_no) as _vts:
            _pgc = _vts.contents.vts_pgcit.contents.pgci_srp[pgc_no - 1].pgc.contents
            _total = int(_pgc.nr_of_cells)
            _cells_meta = cell_metadata_from_pgc(_pgc)
            # stc_discontinuity isn't a CellMeta field; read direct from
            # the cell_playback array.
            stc_disc_by_cell = {
                i + 1: bool(_pgc.cell_playback[i].stc_discontinuity)
                for i in range(_total)
            }

        # Compute the cell_filter from trim + angle filters. None ↔
        # "include everything" (the CellReader default).
        cell_filter: Optional[set[int]] = None
        if (trim is not None and trim.any_trim) or angle != 1:
            # Trim window first (defaults to all cells).
            if trim is not None and trim.any_trim:
                lo = trim.start_trim + 1
                hi = _total - trim.end_trim
                trim_set = set(range(lo, hi + 1)) if hi >= lo else set()
            else:
                trim_set = set(range(1, _total + 1))
            # Then intersect with the selected angle (no-op for angle=1
            # on single-angle PGCs since cells_for_angle returns every
            # cell index in that case).
            angle_set = cells_for_angle(_cells_meta, angle=angle)
            cell_filter = trim_set & angle_set if angle_set else trim_set

        # Cumulative-duration anchor for STC-rebasing. Keyed by the
        # 1-based cell index in playback order; the value is the number
        # of 90 kHz ticks of (IFO-declared) duration consumed before this
        # cell starts. Computed over the SET of cells we'll actually
        # play (i.e., post-trim).
        cells_by_index = {c.index: c for c in _cells_meta}
        played_cells_in_order = sorted(
            c.index for c in _cells_meta
            if cell_filter is None or c.index in cell_filter
        )
        cumulative_ticks_at_cell_start: dict[int, int] = {}
        _acc_ticks = 0
        for _idx in played_cells_in_order:
            cumulative_ticks_at_cell_start[_idx] = _acc_ticks
            _acc_ticks += int(round(cells_by_index[_idx].duration_s * 90000))

        # Single rebaser shared across streams. Initial state matches the
        # pre-B1 behaviour: subtract title_first_pts, no cumulative offset.
        # Crossing into an STC-discontinuity cell queues a rebase that
        # the next PTS-bearing PES consumes.
        rebaser = _PtsRebaser(base_ticks=title_first_pts, cumulative_ns=0)
        last_seen_cell: Optional[int] = None

        try:
            with CellReader(disc, title_num, cell_filter=cell_filter) as cr:
                for payload in iter_es_payloads(cr.iter_sectors()):
                    # Track cell transitions for STC-discontinuity rebasing.
                    # Done before the is_nav skip so VOBUs led by a NAV pack
                    # still trigger the boundary queue.
                    if payload.cell_index != last_seen_cell:
                        if (last_seen_cell is not None
                                and stc_disc_by_cell.get(payload.cell_index, False)):
                            cum = cumulative_ticks_at_cell_start.get(payload.cell_index)
                            if cum is not None:
                                rebaser.queue_rebase(cum)
                        last_seen_cell = payload.cell_index

                    if payload.is_nav:
                        continue
                    key = stream_key(payload.stream_id, payload.substream_id)
                    q = queues_by_key.get(key)
                    if q is None:
                        continue
                    if not payload.es_bytes:
                        continue

                    kind = kind_by_key.get(key)

                    if kind == MkvTrackType.VIDEO:
                        if payload.pts is not None:
                            # New picture group — flush prior, start new
                            _emit_pictures(key)
                            group[key] = {
                                "pts_ns": rebaser.adjust(payload.pts),
                                "bufs": [payload.es_bytes],
                            }
                        else:
                            # Continuation of current group
                            g = group.get(key)
                            if g is None:
                                continue  # orphan continuation pre-first-PTS
                            g["bufs"].append(payload.es_bytes)
                    elif kind == MkvTrackType.SUBTITLE:
                        # Subpicture: PTS marks the start of a new SPU;
                        # subsequent PES (no PTS) extend it.
                        if payload.pts is not None:
                            _flush_sub_accum(key)
                            sub_accum[key] = {
                                "pts": payload.pts,
                                "bufs": [payload.es_bytes],
                            }
                        else:
                            cur = sub_accum.get(key)
                            if cur is None:
                                continue  # orphan continuation pre-first-PTS
                            cur["bufs"].append(payload.es_bytes)
                    else:
                        # Audio — each PES is one self-contained chunk.
                        if payload.pts is None:
                            continue
                        data = payload.es_bytes
                        # LPCM: byte-swap from disc-original big-endian to
                        # little-endian, matching MakeMKV's storage as
                        # A_PCM/INT/LIT. Decoded audio is identical; this
                        # is purely a container-byte convention.
                        plan_obj = plan_for_key.get(key)
                        if plan_obj is not None and plan_obj.codec_name == "lpcm":
                            data = _lpcm_be_to_le(
                                data, bits_per_sample=plan_obj.bits_per_sample,
                            )
                        q.put(MkvChunk(
                            data=data,
                            timecode=rebaser.adjust(payload.pts),
                            duration=0,
                            flags=MkvChunkFlags.KEYFRAME,
                        ))

            # Flush any pending video at EOF
            for k in list(group):
                _emit_pictures(k)
            # Flush any accumulating sub SPUs then their pending events
            # at EOF (last sub has no lookahead — gets default duration).
            for k in list(sub_accum):
                _flush_sub_accum(k)
            for k in list(pending_sub):
                _emit_sub(k, None)
        except Exception:
            _logger.exception("dvd producer thread crashed")
        finally:
            if not sentinel_sent:
                for q in queues_by_key.values():
                    try:
                        q.put_nowait(None)
                    except queue.Full:
                        q.put(None)
                sentinel_sent = True

    prod = threading.Thread(target=producer, name=f"dvd-producer-T{title_num}",
                            daemon=True)
    if start_producer:
        prod.start()

    track = DvdTrack(sources, prod)
    name = title_name or f"Title {title_num}"
    title_info = DvdTitleInfo(name=name, chapters=mkv_chapters)
    return track, title_info


__all__ = [
    "DvdFrameSource", "DvdTrack", "DvdTitleInfo",
    "open_dvd_title",
]
