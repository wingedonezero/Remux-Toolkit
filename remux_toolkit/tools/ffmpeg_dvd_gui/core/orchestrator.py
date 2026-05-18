"""
Native title-rip orchestrator.

Architecture (final, 2026-05-18 — see project_disc_ripper_phase4c.md for the
rationale):

  disc → libdvdread → CellReader → ps_walker.iter_es_payloads
                                          │
                                          ↓
                            per-stream raw ES bytes
                                          │
                                          ↓
                ffmpeg (multi-fd input, one pipe per stream)
                        ─ video pipe  (-f mpegvideo)
                        ─ audio pipes (-f ac3, dts, ...)
                        ─ subpic pipes (-f dvd_subtitle) [optional]
                        ─ chapters (-i chapters.ffmetadata)
                                          │
                                          ↓
                                   .mkv on disk
                                  (streaming write)

**We never use ffmpeg's dvdvideo demuxer or its generic MPEG-PS demuxer.**
ffmpeg's only job is to take ES bytes we've already extracted + per-stream
start-offset hints + chapters + color metadata, and produce a streaming MKV.

A/V sync is handled by ONE scalar per stream — the delay between that
stream's first PES PTS and the video stream's first PES PTS. We compute
these in a pre-scan pass (first VOBU ≈ first 100 sectors), then pass them
via `-itsoffset` on the per-stream input. After that, ffmpeg infers PTS
from codec-implicit timing:
  * MPEG video — `-r <framerate>` tells it the constant frame rate
  * AC3/DTS    — fixed frame size at fixed sample rate, +genpts handles it

PTM/STC discontinuities on the wire (cell boundaries with `stc_discontinuity`)
are irrelevant in this architecture because we strip PES headers entirely.
The codec ES bytes are continuous across cells; there is no PTS in the output
ES to "go backwards." The bug we patched in vanilla ffmpeg dvdvideo (the AC3
dedup at PG boundaries) cannot occur because we never compare PTS values
during extraction.

For VFR sources (soft-telecined 24p inside 29.97 NTSC), this CFR model will
slightly mis-time frames. Detection + VFR-aware output is a Phase 4d concern.
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from ..bindings import libdvdread as dr
from ..bindings.libdvdread import _lang_code_to_str
from .demux.cell_reader import CellReader, Cell, _resolve_title_to_pgc
from .demux.chapters import extract_chapters, chapters_to_ffmetadata
from .demux.ps_walker import (
    STREAM_MPEG_VIDEO, STREAM_PRIVATE_1, iter_es_payloads, stream_key,
)


# ---------------------------------------------------------------------------
# Options / result types
# ---------------------------------------------------------------------------

@dataclass
class RipOptions:
    # Subpicture extraction uses ffmpeg's dvdvideo demuxer as a side-channel
    # (small temp .mkv next to the output, then included as an input to the
    # main mux). dvdvideo's AC3 frame-drop bug doesn't affect subpicture
    # extraction (different code path). Cross-validated against MakeMKV.
    # To skip subs entirely set this to False.
    include_subpictures: bool = True
    # When True, use ffmpeg's dvdvideo demuxer to extract the subpicture
    # stream(s). When False, no subs in output (until we implement a native
    # subpicture handler). Phase 4d.2 status: dvdvideo side-channel works.
    use_dvdvideo_for_subs: bool = True
    include_closed_captions: bool = False  # Phase 4d.3 — line21 CC extraction
    write_chapters: bool = True
    write_color_metadata: bool = True
    log_callback: Optional[Callable[[str, str], None]] = None
    progress_callback: Optional[Callable[[int, int], None]] = None
    cancel_check: Optional[Callable[[], bool]] = None


@dataclass
class StreamRipStats:
    """Per-stream observed during the rip."""
    key: tuple                         # (stream_id, substream_id_or_-1)
    codec_name: str                    # ffmpeg `-f` value
    language: str
    title: str
    first_pts_ticks: Optional[int]     # 90 kHz; from the pre-scan pass
    delay_seconds: float               # vs video stream; passed via -itsoffset
    packets_written: int = 0
    bytes_written: int = 0


@dataclass
class RipResult:
    output_path: Path
    ffmpeg_returncode: int
    sectors_read: int
    bytes_read: int
    streams: list[StreamRipStats] = field(default_factory=list)
    cancelled: bool = False
    error: str = ""
    ffmpeg_stderr_tail: str = ""
    elapsed_seconds: float = 0.0
    audio_dedup_drops: int = 0


# ---------------------------------------------------------------------------
# Color / codec helpers
# ---------------------------------------------------------------------------

@dataclass
class _ColorPlan:
    primaries: str
    trc: str
    space: str
    range_: str = "tv"


def _color_for_video_format(video_format: int) -> _ColorPlan:
    """BT.601 NTSC vs PAL — DVD-Video standard colorimetry."""
    if video_format == 0:  # NTSC
        return _ColorPlan(primaries="smpte170m", trc="smpte170m", space="smpte170m")
    return _ColorPlan(primaries="bt470bg",  trc="smpte170m", space="bt470bg")


# Match libdvdread `audio_attr_t.audio_format` to our internal codec name + ffmpeg `-f`.
_AUDIO_FORMAT_TABLE = {
    # audio_format: (our_name, substream_base, ffmpeg_input_format)
    # ffmpeg_input_format=None means we'll fill it in per-stream (LPCM needs
    # bit-depth-dependent format).
    0: ("ac3",  0x80, "ac3"),
    2: ("mp1",  0xC0, "mp2"),     # MPEG-1 layer 2 — uses 0xC0 stream_id, not private_1
    3: ("mp2",  0xC0, "mp2"),
    4: ("lpcm", 0xA0, None),      # filled in below based on quantization
    6: ("dts",  0x88, "dts"),
}

# DVD-Video audio_attr sample_frequency: 00=48 kHz, 01=96 kHz
_LPCM_SAMPLE_RATE = {0: 48000, 1: 96000}
# DVD-Video audio_attr quantization: 00=16-bit, 01=20-bit, 10=24-bit
_LPCM_QUANT_TO_FFMPEG_FORMAT = {
    0: "s16be",   # 16-bit big-endian PCM
    2: "s24be",   # 24-bit big-endian PCM
    # 20-bit (quant=1) needs custom unpacking; ffmpeg doesn't have a direct
    # demuxer. Rare on DVD; defer.
}


def _plan_lpcm_input(audio_attr) -> tuple[Optional[str], list[str]]:
    """For an LPCM audio_attr, return (ffmpeg_input_format, extra_input_args).
    extra args set sample rate (-ar) and channel count (-ac) since raw PCM
    input has no codec-level framing for them. Returns (None, []) if we
    can't handle this LPCM variant yet (20-bit)."""
    quant = int(audio_attr.quantization)
    fmt = _LPCM_QUANT_TO_FFMPEG_FORMAT.get(quant)
    if fmt is None:
        return (None, [])
    sr = _LPCM_SAMPLE_RATE.get(int(audio_attr.sample_frequency))
    if sr is None:
        return (None, [])
    channels = int(audio_attr.channels) + 1  # field is N-1
    return (fmt, ["-ar", str(sr), "-ac", str(channels)])


# ISO 639-1 → ISO 639-2/B for MKV language tags
_ISO_639_1_TO_639_2 = {
    "en": "eng", "fr": "fre", "es": "spa", "de": "ger", "it": "ita",
    "ja": "jpn", "zh": "chi", "ko": "kor", "ru": "rus", "pt": "por",
    "nl": "dut", "sv": "swe", "no": "nor", "da": "dan", "fi": "fin",
    "pl": "pol", "ar": "ara", "he": "heb", "tr": "tur", "el": "gre",
    "cs": "cze", "hu": "hun", "ro": "rum", "uk": "ukr",
}


def _to_iso639_2(code: str) -> str:
    if not code: return ""
    code = code.lower()
    if len(code) == 3: return code
    return _ISO_639_1_TO_639_2.get(code, code)


# ---------------------------------------------------------------------------
# Stream-plan discovery
# ---------------------------------------------------------------------------

@dataclass
class _StreamPlan:
    key: tuple                # (stream_id, substream_id_or_-1)
    codec_name: str           # our internal name ("ac3", "mp2", "lpcm", ...)
    ffmpeg_input_format: str  # ffmpeg -f value
    language: str             # 2-letter ISO 639-1
    title: str                # human label for MKV
    is_video: bool = False
    extra_input_args: list = field(default_factory=list)
                              # extra ffmpeg input opts (-ar, -ac, etc.)
                              # placed AFTER `-fflags +genpts` and BEFORE -i

    # Codec parameters from libdvdread's audio_attr (used by the native
    # mux path; ffmpeg path auto-detects so these fields are ignored
    # there). Zero/empty for video streams.
    sample_rate: int = 0      # Hz
    channels: int = 0         # 1..8
    bits_per_sample: int = 0  # only meaningful for LPCM; 0 otherwise

    # DVD-Video display attributes for the video stream (only meaningful
    # when is_video=True). 0 = unspecified.
    pixel_w: int = 0
    pixel_h: int = 0
    display_w: int = 0           # DAR-adjusted display width
    display_h: int = 0
    color_primaries: int = 0     # H.273 codes: 5=bt470bg, 6=smpte170m, ...
    color_transfer: int = 0
    color_matrix: int = 0
    color_range: int = 0         # 1=tv (16-235), 2=full
    letterboxed: bool = False    # 4:3 frame with 16:9 letterboxed content

    # DVD-Video audio_attr / subp_attr extensions. 0 = unspecified.
    # Audio code_extension: 1=normal, 2=visually-impaired, 3=director's
    #   commentary, 4=alt director's commentary. Drives MKV track name
    #   + DEFAULT-flag decisions.
    # Subpicture code_extension: 9=forced, 13=director's commentary,
    #   14=director's notes; lower values are CC variants.
    code_extension: int = 0
    lang_extension: int = 0


#: DVD picture_size codes → (pixel_w, pixel_h) for NTSC.
_PICTURE_SIZE_NTSC_DIMS = {
    0: (720, 480), 1: (704, 480), 2: (352, 480), 3: (352, 240),
}
_PICTURE_SIZE_PAL_DIMS = {
    0: (720, 576), 1: (704, 576), 2: (352, 576), 3: (352, 288),
}


def _video_display_dims(video_attr) -> tuple[int, int, int, int]:
    """Resolve (pixel_w, pixel_h, display_w, display_h) from VideoAttr.

    DVD-Video display_aspect_ratio: 0 = 4:3, 3 = 16:9 (anamorphic — the
    pixel grid is 720 wide but stretched to 16:9 on playback). Players
    use DisplayWidth/Height to compute the rendered aspect.
    """
    video_format = int(video_attr.video_format)
    sizes = _PICTURE_SIZE_NTSC_DIMS if video_format == 0 else _PICTURE_SIZE_PAL_DIMS
    pw, ph = sizes.get(int(video_attr.picture_size), (720, 480))

    dar_code = int(video_attr.display_aspect_ratio)
    if dar_code == 3:        # 16:9 anamorphic
        dw, dh = 16, 9
    elif dar_code == 0:      # 4:3
        dw, dh = 4, 3
    else:                    # 1, 2 reserved; default to pixel grid
        dw, dh = pw, ph
    return pw, ph, dw, dh


def _video_color_codes(video_format: int) -> tuple[int, int, int, int]:
    """Return (primaries, transfer, matrix, range) H.273 codes for DVD-Video.

    All DVDs are SD BT.601 broadcast-range. The primaries/matrix differ
    between NTSC (SMPTE 170M = 6) and PAL (BT.470 BG = 5); transfer is
    SMPTE 170M for both (it's the BT.601 transfer); range is broadcast.
    """
    if video_format == 0:    # NTSC
        return 6, 6, 6, 1    # smpte170m / smpte170m / smpte170m / tv
    return 5, 6, 5, 1        # bt470bg / smpte170m / bt470bg / tv


def _enumerate_streams(disc, vts_no: int, pgc_no: int,
                        *, include_subpictures: bool) -> tuple[list[_StreamPlan], _ColorPlan, str]:
    """Returns (streams_in_emission_order, color_plan, video_framerate_str).
    Stream order: video first, then audio (slot order), then subpictures."""
    plans: list[_StreamPlan] = []

    with dr.open_ifo(disc, vts_no) as vts:
        m = vts.contents.vtsi_mat.contents
        pgcit = vts.contents.vts_pgcit.contents
        pgc = pgcit.pgci_srp[pgc_no - 1].pgc.contents

        video_format = int(m.vts_video_attr.video_format)
        framerate = "30000/1001" if video_format == 0 else "25"

        # Video plan: gather pixel dims + DAR-adjusted display + color.
        pw, ph, dw, dh = _video_display_dims(m.vts_video_attr)
        cp, ct, cm, cr = _video_color_codes(video_format)
        plans.append(_StreamPlan(
            key=stream_key(STREAM_MPEG_VIDEO, None),
            codec_name="mpeg2video", ffmpeg_input_format="mpegvideo",
            language="", title="", is_video=True,
            pixel_w=pw, pixel_h=ph,
            display_w=dw, display_h=dh,
            color_primaries=cp, color_transfer=ct,
            color_matrix=cm, color_range=cr,
            letterboxed=bool(m.vts_video_attr.letterboxed),
        ))

        for slot in range(min(int(m.nr_of_vts_audio_streams), 8)):
            if not (pgc.audio_control[slot] & 0x8000):
                continue
            attr = m.vts_audio_attr[slot]
            entry = _AUDIO_FORMAT_TABLE.get(int(attr.audio_format))
            if entry is None:
                continue
            our_name, sub_base, ff_fmt = entry
            extra_args: list[str] = []
            # DVD-Video forces 48 kHz for AC3/DTS/MP1/MP2; LPCM may be
            # 96 kHz. Channels field is N-1.
            sample_rate = 48000
            channels = int(attr.channels) + 1
            bits_per_sample = 0
            if our_name == "lpcm":
                # Bit-depth/sample-rate/channels are in the VTS audio_attr; we
                # tell ffmpeg via `-ar`/`-ac` since raw PCM has no framing.
                ff_fmt, extra_args = _plan_lpcm_input(attr)
                if ff_fmt is None:
                    continue  # unsupported LPCM (20-bit)
                sample_rate = _LPCM_SAMPLE_RATE.get(int(attr.sample_frequency), 48000)
                bits_per_sample = {0: 16, 1: 20, 2: 24}.get(int(attr.quantization), 16)
            if ff_fmt is None:
                continue
            substream_id = sub_base + slot
            # MPEG audio uses 0xC0+slot directly (not private_stream_1)
            stream_id_outer = STREAM_PRIVATE_1 if sub_base != 0xC0 else (0xC0 + slot)
            substream_for_key = substream_id if sub_base != 0xC0 else None
            lang = _lang_code_to_str(attr.lang_code) or ""
            plans.append(_StreamPlan(
                key=stream_key(stream_id_outer, substream_for_key),
                codec_name=our_name, ffmpeg_input_format=ff_fmt,
                language=lang,
                title=f"Audio {slot+1} ({lang or '?'})",
                extra_input_args=extra_args,
                sample_rate=sample_rate,
                channels=channels,
                bits_per_sample=bits_per_sample,
                code_extension=int(attr.code_extension),
                lang_extension=int(attr.lang_extension),
            ))

        # NOTE: subpictures are NOT added to the multi-fd pipe plan for
        # the ffmpeg path — ffmpeg has no `-f dvd_subtitle` raw demuxer.
        # The ffmpeg path uses `_extract_subpictures_via_dvdvideo`.
        # The NATIVE mux path (codec_name="subpicture") consumes the
        # private_stream_1 + substream_id 0x20+slot PES directly and
        # parses SP_DCSQ inline (see core/demux/subpicture.py). We emit
        # plans for it here when requested; callers that don't want
        # subpictures (ffmpeg path) pass include_subpictures=False.
        if include_subpictures:
            for slot in range(min(int(m.nr_of_vts_subp_streams), 32)):
                ctrl = pgc.subp_control[slot]
                if not (ctrl & 0x80000000):
                    continue
                subp_attr = m.vts_subp_attr[slot]
                lang = _lang_code_to_str(subp_attr.lang_code) or ""
                substream_id = 0x20 + slot
                plans.append(_StreamPlan(
                    key=stream_key(STREAM_PRIVATE_1, substream_id),
                    codec_name="subpicture",
                    ffmpeg_input_format="",   # not used on native path
                    language=lang,
                    title=f"Subtitle {slot+1} ({lang or '?'})",
                    code_extension=int(subp_attr.code_extension),
                    lang_extension=int(subp_attr.lang_extension),
                ))

    color = _color_for_video_format(video_format)
    return plans, color, framerate


# ---------------------------------------------------------------------------
# Subpicture side-channel via ffmpeg dvdvideo demuxer
# ---------------------------------------------------------------------------

def _extract_subpictures_via_dvdvideo(disc_path: str, title_num: int,
                                       output_path: Path,
                                       *, ffmpeg_bin: str = "ffmpeg",
                                       log_callback: Optional[Callable[[str, str], None]] = None,
                                       ) -> Optional[Path]:
    """Use ffmpeg's `dvdvideo` demuxer to extract just subpicture streams
    from a title into a small MKV. We use dvdvideo here because there's no
    `-f dvd_subtitle` raw demuxer that would let us pipe subs through the
    main multi-fd pipeline. dvdvideo's AC3-PG-boundary bug doesn't affect
    subpictures (different code path); cross-validated against MakeMKV.

    Returns the temp MKV path on success, or None if extraction failed
    (no subs present, or ffmpeg error)."""
    cmd = [
        ffmpeg_bin, "-hide_banner", "-y", "-loglevel", "error",
        "-f", "dvdvideo", "-title", str(title_num), "-i", disc_path,
        "-map", "0:s", "-c", "copy",
        str(output_path),
    ]
    if log_callback:
        log_callback("info", "subs side-channel: " + " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        if log_callback:
            log_callback("warn", f"sub side-channel rc={proc.returncode}; stderr: {proc.stderr[:500]}")
        return None
    if not output_path.exists() or output_path.stat().st_size < 100:
        return None
    return output_path


# ---------------------------------------------------------------------------
# Pre-scan: find first PTS per stream
# ---------------------------------------------------------------------------

def _prescan_first_pts(disc, title_num: int, vts_no: int, pgc_no: int,
                        plan_keys: set[tuple],
                        *, max_sectors: int = 1024) -> dict[tuple, int]:
    """Walk up to `max_sectors` from the start of the title, return first PTS
    seen per stream key. Returns 90 kHz ticks per key (key absent if no PTS
    was seen for that stream)."""
    first_pts: dict[tuple, int] = {}
    remaining_keys = set(plan_keys)
    seen_sectors = 0

    with CellReader(disc, title_num, vts_no=vts_no, pgc_no=pgc_no) as reader:
        for cell, sector in reader.iter_sectors():
            for pkt in iter_es_payloads(iter([(cell, sector)])):
                if pkt.is_nav or pkt.pts is None:
                    continue
                key = stream_key(pkt.stream_id, pkt.substream_id)
                if key in remaining_keys and key not in first_pts:
                    first_pts[key] = pkt.pts
                    remaining_keys.discard(key)
            seen_sectors += 1
            if not remaining_keys or seen_sectors >= max_sectors:
                break
    return first_pts


# ---------------------------------------------------------------------------
# Per-stream pipe management
# ---------------------------------------------------------------------------

@dataclass
class _PipeState:
    """One ES → ffmpeg pipe + its writer thread."""
    plan: _StreamPlan
    read_fd: int            # the fd ffmpeg inherits
    write_fd: int           # we write to this; gets closed when stream ends
    q: queue.Queue          # bounded; backpressure via maxsize
    thread: Optional[threading.Thread] = None
    bytes_written: int = 0
    packets_written: int = 0


def _writer_loop(state: _PipeState, proc: subprocess.Popen) -> None:
    """Drain the queue to the pipe. Returns on sentinel (None) or broken pipe."""
    try:
        while True:
            item = state.q.get()
            if item is None:
                break
            try:
                view = memoryview(item)
                while view:
                    written = os.write(state.write_fd, view)
                    view = view[written:]
            except (BrokenPipeError, OSError):
                return
    finally:
        try:
            os.close(state.write_fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# ffmpeg command builder
# ---------------------------------------------------------------------------

def _build_ffmpeg_cmd(ffmpeg_bin: str, output: Path,
                       pipes: list[_PipeState],
                       framerate: str,
                       stream_delays: dict[tuple, float],
                       chapters_path: Optional[Path],
                       subs_mkv: Optional[Path],
                       color: _ColorPlan,
                       opts: RipOptions) -> list[str]:
    """Build the ffmpeg invocation. Input layout:
       0: chapters (if present)
       N: each ES pipe (video then audios)
       last: subs MKV from dvdvideo side-channel (if present)
    """
    cmd: list[str] = [ffmpeg_bin, "-hide_banner", "-y", "-loglevel", "warning"]

    meta_offset = 0
    if chapters_path is not None:
        cmd += ["-i", str(chapters_path)]
        meta_offset = 1

    # Compensate for ffmpeg's +genpts artifact: when ffmpeg synthesizes PTS
    # for raw video ES, it numbers the first frame at PTS = 1 * frame_duration
    # (not 0). Without correction this leaves the video output 33 ms (NTSC) or
    # 40 ms (PAL) "later" than disc intended, skewing A/V sync — audio (which
    # naturally lands at PTS=0) ends up effectively EARLIER than video.
    # Subtracting one frame_duration from the video input's itsoffset puts the
    # first video frame at PTS=0, matching MakeMKV and the disc's intent.
    video_genpts_compensation = 1001 / 30000 if framerate == "30000/1001" else 1 / 25

    for p in pipes:
        # Per-stream input options BEFORE -i
        if p.plan.is_video:
            cmd += ["-r", framerate, "-itsoffset", f"-{video_genpts_compensation:.6f}"]
        else:
            # itsoffset: the start delay of this stream vs video. Positive
            # value means stream plays later than video in the output.
            delay = stream_delays.get(p.plan.key, 0.0)
            if delay:
                cmd += ["-itsoffset", f"{delay:.6f}"]
        # Plan-level extra args (e.g. -ar / -ac for raw LPCM)
        cmd += list(p.plan.extra_input_args)
        cmd += ["-fflags", "+genpts", "-f", p.plan.ffmpeg_input_format,
                "-i", f"pipe:{p.read_fd}"]

    # Subs MKV input MUST come before any -map flag. All inputs first,
    # then all output options (-map, -metadata, -c copy, etc.). Mixing
    # them produces "Option map ... cannot be applied to input url" errors.
    subs_input_index: Optional[int] = None
    if subs_mkv is not None:
        subs_input_index = meta_offset + len(pipes)
        cmd += ["-i", str(subs_mkv)]

    # Now safe to start output options: -map for every ES stream input
    for i, _ in enumerate(pipes):
        cmd += ["-map", f"{i + meta_offset}:0"]
    if subs_input_index is not None:
        cmd += ["-map", f"{subs_input_index}:s"]

    if chapters_path is not None:
        cmd += ["-map_chapters", "0"]

    # Per-stream MKV metadata (language + title)
    a_idx = s_idx = 0
    for i, p in enumerate(pipes):
        if p.plan.is_video:
            continue
        kind = "a" if p.plan.codec_name != "dvd_subtitle" else "s"
        idx = a_idx if kind == "a" else s_idx
        lang3 = _to_iso639_2(p.plan.language)
        if lang3:
            cmd += [f"-metadata:s:{kind}:{idx}", f"language={lang3}"]
        cmd += [f"-metadata:s:{kind}:{idx}", f"title={p.plan.title}"]
        if kind == "a": a_idx += 1
        else: s_idx += 1

    cmd += ["-c", "copy"]

    if opts.write_color_metadata:
        cmd += [
            "-color_primaries:v", color.primaries,
            "-color_trc:v",       color.trc,
            "-colorspace:v",      color.space,
            "-color_range:v",     color.range_,
        ]

    cmd += [str(output)]
    return cmd


# ---------------------------------------------------------------------------
# Top-level rip
# ---------------------------------------------------------------------------

def rip_title(disc, title_num: int, output_path: Path,
              *, options: Optional[RipOptions] = None,
              ffmpeg_bin: str = "ffmpeg",
              disc_source_path: Optional[str] = None) -> RipResult:
    """
    `disc_source_path` is the on-disk path string that ffmpeg's `-f dvdvideo`
    accepts (folder containing VIDEO_TS, or ISO file). Required only when
    `RipOptions.include_subpictures` AND `use_dvdvideo_for_subs` are both True,
    since the subpicture side-channel uses ffmpeg dvdvideo. Pass it through
    from the CLI / GUI alongside `disc`.
    """
    opts = options or RipOptions()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    vts_no, pgc_no = _resolve_title_to_pgc(disc, title_num)
    plans, color, framerate = _enumerate_streams(
        disc, vts_no, pgc_no, include_subpictures=opts.include_subpictures,
    )
    if not plans:
        return RipResult(output_path=output_path, ffmpeg_returncode=-1,
                         sectors_read=0, bytes_read=0,
                         error="No rip-able streams found in PGC")

    # Pre-scan first PTS per stream
    plan_keys = {p.key for p in plans}
    first_pts = _prescan_first_pts(disc, title_num, vts_no, pgc_no, plan_keys)
    video_first_pts = first_pts.get(plans[0].key, 0)
    stream_delays: dict[tuple, float] = {}
    for p in plans:
        if p.is_video:
            continue
        if p.key in first_pts and video_first_pts is not None:
            delta_ticks = first_pts[p.key] - video_first_pts
            stream_delays[p.key] = delta_ticks / 90000.0

    if opts.log_callback:
        opts.log_callback("info", f"pre-scan: video first PTS = {video_first_pts}")
        for p in plans:
            if p.is_video: continue
            d = stream_delays.get(p.key, 0.0)
            opts.log_callback("info", f"  stream {p.codec_name} key={p.key}: delay = {d*1000:+.1f} ms")

    # Build chapters ffmetadata (next to output, disk-backed)
    chapters_path: Optional[Path] = None
    if opts.write_chapters:
        chs = extract_chapters(disc, title_num, vts_no=vts_no, pgc_no=pgc_no)
        if chs:
            chapters_path = output_path.with_name(output_path.stem + ".chapters.ffmetadata")
            chapters_path.write_text(chapters_to_ffmetadata(chs))

    # Subpicture side-channel via ffmpeg dvdvideo (disk-backed temp next
    # to the output; small file, typically 1–5 MB per title).
    subs_mkv_path: Optional[Path] = None
    if (opts.include_subpictures and opts.use_dvdvideo_for_subs
            and disc_source_path is not None):
        candidate = output_path.with_name(output_path.stem + ".subs.mkv")
        try:
            subs_mkv_path = _extract_subpictures_via_dvdvideo(
                disc_source_path, title_num, candidate,
                ffmpeg_bin=ffmpeg_bin,
                log_callback=opts.log_callback,
            )
        except subprocess.TimeoutExpired:
            if opts.log_callback:
                opts.log_callback("warn", "sub side-channel timed out; continuing without subs")
    elif opts.include_subpictures and opts.log_callback:
        opts.log_callback("info", "subs requested but disc_source_path not supplied; skipping")

    # Set up per-stream pipes
    pipes: list[_PipeState] = []
    for plan in plans:
        r, w = os.pipe()
        os.set_inheritable(r, True)
        pipes.append(_PipeState(
            plan=plan, read_fd=r, write_fd=w,
            q=queue.Queue(maxsize=128),
        ))

    cmd = _build_ffmpeg_cmd(ffmpeg_bin, output_path, pipes, framerate,
                              stream_delays, chapters_path, subs_mkv_path,
                              color, opts)
    if opts.log_callback:
        opts.log_callback("info", "ffmpeg cmd: " + " ".join(cmd))

    pipe_by_key = {p.plan.key: p for p in pipes}

    # Pre-count sectors for progress
    sectors_total = 0
    with dr.open_ifo(disc, vts_no) as vts:
        pgcit = vts.contents.vts_pgcit.contents
        pgc = pgcit.pgci_srp[pgc_no - 1].pgc.contents
        for i in range(pgc.nr_of_cells):
            cp = pgc.cell_playback[i]
            sectors_total += int(cp.last_sector) - int(cp.first_sector) + 1

    # Spawn ffmpeg with all the read-fds inherited
    proc = subprocess.Popen(
        cmd,
        pass_fds=tuple(p.read_fd for p in pipes),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    # Close read-ends in parent so EOF propagates to child when we close writes
    for p in pipes:
        os.close(p.read_fd)
        p.read_fd = -1

    # Start writer threads
    for p in pipes:
        p.thread = threading.Thread(
            target=_writer_loop, args=(p, proc), daemon=True,
            name=f"writer-{p.plan.codec_name}",
        )
        p.thread.start()

    # Drain ffmpeg stderr in a thread so it doesn't fill the pipe
    stderr_lines: list[str] = []
    stderr_lock = threading.Lock()
    def _drain_stderr():
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                with stderr_lock:
                    stderr_lines.append(line)
                    if len(stderr_lines) > 200:
                        stderr_lines.pop(0)
                if opts.log_callback:
                    sev = "error" if "Error" in line or "error" in line[:30] else "info"
                    opts.log_callback(sev, line)
        except Exception:
            pass
    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    sectors_done = 0
    bytes_read = 0
    cancelled = False
    err = ""
    last_progress = 0.0

    # NOTE on AC3 dedup: ffmpeg's patched dvdvideo demuxer drops a handful
    # of AC3 frames per stream at STC-discontinuity boundaries via a combined
    # `pkt->pts < prev_pts` check AND a `pkt->size != ac3_frame_size` check
    # (see libavformat/dvdvideodec.c lines ~1700-1713). Implementing the size
    # check requires parsing the AC3 sync frame header to validate sizes.
    # Cross-rip comparison vs patched-ffmpeg-dvdvideo on ANGEL T1 shows we
    # keep ~35 frames more per stream (0.04% of total) — these are partial
    # AC3 frames at PG boundaries the disc author left for seamless playback.
    # Keeping them produces a marginally more conservative rip with no
    # audible artifacts in practice; tightening to match patched dvdvideo
    # exactly is Phase 4d work that needs the AC3 parser.
    audio_dedup_drops = 0

    try:
        with CellReader(disc, title_num, vts_no=vts_no, pgc_no=pgc_no) as reader:
            for cell, sector in reader.iter_sectors():
                if opts.cancel_check and opts.cancel_check():
                    cancelled = True
                    break
                # Fail fast if ffmpeg died
                if proc.poll() is not None:
                    err = f"ffmpeg exited early (rc={proc.returncode})"
                    break

                for pkt in iter_es_payloads(iter([(cell, sector)])):
                    if pkt.is_nav:
                        continue
                    key = stream_key(pkt.stream_id, pkt.substream_id)
                    p = pipe_by_key.get(key)
                    if p is None:
                        continue
                    p.q.put(pkt.es_bytes)
                    p.bytes_written += len(pkt.es_bytes)
                    p.packets_written += 1
                sectors_done += 1
                bytes_read += len(sector)
                now = time.monotonic()
                if opts.progress_callback and now - last_progress > 0.1:
                    opts.progress_callback(sectors_done, sectors_total)
                    last_progress = now
    except Exception as e:
        err = f"{type(e).__name__}: {e}"

    # Signal EOF on every writer
    for p in pipes:
        p.q.put(None)
    for p in pipes:
        if p.thread:
            p.thread.join(timeout=30)

    rc = -1
    try:
        rc = proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        if not err:
            err = "ffmpeg did not exit within 120s — killed"

    stderr_thread.join(timeout=5)

    if opts.progress_callback:
        opts.progress_callback(sectors_done, sectors_total)

    if chapters_path is not None:
        try: chapters_path.unlink()
        except OSError: pass
    if subs_mkv_path is not None:
        try: subs_mkv_path.unlink()
        except OSError: pass

    with stderr_lock:
        stderr_tail = "\n".join(stderr_lines[-30:])

    return RipResult(
        output_path=output_path,
        ffmpeg_returncode=rc,
        sectors_read=sectors_done,
        bytes_read=bytes_read,
        cancelled=cancelled,
        error=err,
        ffmpeg_stderr_tail=stderr_tail,
        elapsed_seconds=round(time.monotonic() - t0, 2),
        audio_dedup_drops=audio_dedup_drops,
        streams=[
            StreamRipStats(
                key=p.plan.key, codec_name=p.plan.codec_name,
                language=p.plan.language, title=p.plan.title,
                first_pts_ticks=first_pts.get(p.plan.key),
                delay_seconds=stream_delays.get(p.plan.key, 0.0),
                packets_written=p.packets_written,
                bytes_written=p.bytes_written,
            )
            for p in pipes
        ],
    )
