# remux_toolkit/tools/audio_comparison_analysis/audio_comparison_analysis_core.py

from __future__ import annotations

from dataclasses import asdict, dataclass
import gc
import json
import math
import os
import re
import subprocess
import tempfile
from typing import Iterable
import warnings

import librosa
import numpy as np
import scipy.signal
import soundfile as sf
try:
    import soxr
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    soxr = None

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def check_dependencies() -> tuple[bool, str]:
    missing = []
    for tool in ("ffprobe", "ffmpeg"):
        try:
            subprocess.check_output([tool, "-version"], text=True, stderr=subprocess.STDOUT)
        except (subprocess.CalledProcessError, FileNotFoundError):
            missing.append(tool)
    if missing:
        return False, f"Missing dependencies: {', '.join(missing)}"
    return True, "Dependencies available."


@dataclass
class AnalysisSettings:
    target_sample_rate: int
    fft_size: int
    hop_length: int
    cutoff_db_below_peak: float
    shelf_low_hz: float
    shelf_high_hz: float
    shelf_drop_db: float
    clip_threshold: float
    clip_ratio_warn: float
    phase_inversion_threshold: float
    bitrate_reference_kbps: float
    dr_reference_db: float
    dr_block_seconds: float
    dr_silence_db: float
    dr_top_percent: float
    lra_target_min: float
    lra_target_max: float
    lra_high_penalty_db: float
    nr_cutoff_hz: float
    nr_drop_db: float
    nr_ratio_db: float
    eq_muffle_drop_db: float
    eq_boom_boost_db: float
    eq_muffle_low_hz: float
    eq_muffle_high_hz: float
    eq_boom_center_hz: float
    eq_boom_band_hz: float
    f0_segment_seconds: float
    f0_segment_offset_ratio: float
    pal_speed_ratio: float
    pitch_semitone_shift: float
    pitch_tolerance_ratio: float
    scc_min_match_confidence: float
    channel_swap_corr_threshold: float
    lfe_rolloff_hz: float
    lfe_high_ratio_db: float
    limiting_window_ms: int
    limiting_ratio: float
    limiting_heatmap_block_seconds: float
    limiting_waveform_segments: int
    glitch_diff_threshold: float
    glitch_max_count: int
    clip_heatmap_block_seconds: float
    true_peak_dbfs: float
    brickwall_dr_db: float
    target_dr_min: float
    target_dr_max: float
    dialog_band_low_hz: float
    dialog_band_high_hz: float
    presence_band_low_hz: float
    presence_band_high_hz: float
    dialog_balance_warn_db: float
    loudness_diff_warn_db: float
    mastering_diff_penalty_db: float
    dialogue_clarity_penalty: float
    decode_error_limit: int
    decode_duration_tolerance_s: float
    pair_corr_fake_threshold: float
    lfe_dead_rms_db: float
    sync_step_warn_ms: float
    mel_bins: int
    weight_frequency: float
    weight_dynamic_range: float
    weight_cleanliness: float
    weight_efficiency: float
    weight_format: float
    weight_dialogue: float
    weight_mastering: float


@dataclass
class AudioAnalysisResult:
    path: str
    duration_s: float
    sample_rate: int
    channels: int
    codec_name: str | None
    codec_profile: str | None
    is_lossless: bool | None
    peak: float
    rms: float
    dr_db: float
    dr_blocks_used: int
    loudness_db: float
    loudness_range_db: float
    dialog_balance_db: float
    loudness_offset_db: float
    true_peak_db: float
    freq_cutoff_hz: float
    shelf_detected: bool
    reencode_detected: bool
    nr_filtered: float
    eq_muffle_db: float
    eq_boom_db: float
    eq_warnings: list[str]
    clipping_ratio: float
    clipping_detected: bool
    center_clipping_ratio: float
    center_clipping_detected: bool
    center_nr_filtered: float
    phase_inversion: bool
    fake_multichannel: bool
    surround_swap_detected: bool
    lfe_rolloff_error: bool
    pitch_ratio: float | None
    speed_shift_detected: bool
    pitch_shift_detected: bool
    alignment_offset_s: float
    alignment_confidence: float
    aligned_duration_s: float
    alignment_method: str
    alignment_failed: bool
    alignment_warning: str | None
    score_explain: dict
    bitrate_kbps: float | None
    bitrate_bloat: bool
    file_size_mb: float
    freq_score: float
    dr_score: float
    cleanliness_score: float
    efficiency_score: float
    format_score: float
    dialogue_score: float
    mastering_score: float
    quality_grade: str
    score: float
    summary: str
    spectrogram_path: str | None
    diff_spectrum_path: str | None
    clipping_heatmap_path: str | None
    delta_eq_path: str | None
    limiting_heatmap_path: str | None
    limiting_waveform_paths: list[str]
    glitch_timestamps: list[float]
    limiting_segments: list[tuple[float, float]]
    reference_path: str | None
    stereo_width: float
    lr_level_imbalance_db: float
    lr_noise_floor_diff_db: float
    lr_imbalance_detected: bool
    center_bass_loss_db: float
    decode_errors: int
    container_duration_s: float
    duration_mismatch_s: float
    decode_damaged: bool
    damaged_regions: list[tuple[float, float]]
    channel_corr_pairs: dict[str, float]
    channel_rms_db: list[float]
    lfe_dead: bool
    fake_reasons: list[str]
    sync_step_detected: bool
    sync_step_time_s: float | None
    sync_step_delta_ms: float
    disqualified: bool
    disqualify_reasons: list[str]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["path"] = self.path
        return data


# Chunk sizes for streaming analysis: movie-length multichannel audio is far too
# large to hold decoded in RAM (a 2 h 7.1 track is ~11 GB as float32), so every
# whole-signal operation below works block-by-block over a disk-backed memmap.
_CHUNK_SAMPLES = 4_000_000
_CORR_CHUNK_SAMPLES = 1_000_000
_LARGE_DECODE_BYTES = 512 * 1024 * 1024
_DELTA_EQ_TIME_BUCKETS = 1024


def _iter_ranges(start: int, stop: int, step: int):
    for s in range(start, stop, step):
        yield s, min(stop, s + step)


def _remove_files(paths: Iterable[str]) -> None:
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def _probe_stream_basics(path: str) -> tuple[float | None, int | None, int | None]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=channels,sample_rate",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        path,
    ]
    try:
        output = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
        data = json.loads(output)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return None, None, None
    streams = data.get("streams") or [{}]
    stream = streams[0] if isinstance(streams[0], dict) else {}
    fmt = data.get("format", {}) if isinstance(data.get("format", {}), dict) else {}
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError):
        duration = None
    try:
        channels = int(stream.get("channels"))
    except (TypeError, ValueError):
        channels = None
    try:
        sample_rate = int(stream.get("sample_rate"))
    except (TypeError, ValueError):
        sample_rate = None
    return duration, channels, sample_rate


def _estimated_decoded_bytes(path: str) -> int | None:
    duration, channels, sample_rate = _probe_stream_basics(path)
    if not duration or not channels or not sample_rate:
        return None
    return int(duration * channels * sample_rate * 4)


# Substrings (lowercase) that mark a decoder-error line in ffmpeg stderr.
# ffmpeg exits 0 and silently drops the bad frames, so counting these lines
# is the only way to know the decode was incomplete.
_DECODE_ERROR_PATTERNS = (
    "error submitting packet",
    "invalid data found",
    "invalid frame type",
    "invalid sample rate",
    "error while decoding",
    "header missing",
    "frame sync error",
    "corrupt",
    "invalid nal",
)


def _count_decoder_errors(stderr_text: str) -> int:
    count = 0
    last_was_error = False
    for line in stderr_text.splitlines():
        low = line.lower()
        # ffmpeg collapses identical consecutive messages; credit the batch to
        # the preceding error line so counts stay exact even without repeat+.
        repeat = re.search(r"last message repeated (\d+) times", low)
        if repeat:
            if last_was_error:
                count += int(repeat.group(1))
            continue
        if any(p in low for p in _DECODE_ERROR_PATTERNS):
            count += 1
            last_was_error = True
        else:
            last_was_error = False
    return count


def _null_decode_error_scan(path: str) -> int:
    """Decode the first audio stream to null and count decoder-error lines."""
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "repeat+error",
        "-i",
        path,
        "-map",
        "0:a:0",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return 0
    return _count_decoder_errors(proc.stderr or "")


def _locate_damage_regions(
    path: str, duration_s: float | None, n_segments: int = 10
) -> list[tuple[float, float]]:
    """Null-decode the stream in segments and report which time ranges error."""
    if not duration_s or duration_s <= 0:
        return []
    seg_len = duration_s / n_segments
    damaged: list[int] = []
    for i in range(n_segments):
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-v",
            "repeat+error",
            "-ss",
            f"{i * seg_len:.3f}",
            "-t",
            f"{seg_len:.3f}",
            "-i",
            path,
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            return []
        if _count_decoder_errors(proc.stderr or "") > 0:
            damaged.append(i)
    regions: list[tuple[float, float]] = []
    for i in damaged:
        start, end = i * seg_len, (i + 1) * seg_len
        if regions and regions[-1][1] >= start - 1e-6:
            regions[-1] = (regions[-1][0], end)
        else:
            regions.append((start, end))
    return regions


def _container_duration(metadata: dict) -> float | None:
    """Container-claimed audio duration: stream duration first, format fallback."""
    for key in ("stream_duration", "duration"):
        try:
            value = float(metadata.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _decode_to_memmap(
    path: str, temp_dir: str, target_sr: int
) -> tuple[np.ndarray, int, list[str], int]:
    """Decode the first audio stream to raw float32 on disk and memory-map it.

    Also returns the decoder-error count parsed from ffmpeg stderr: ffmpeg
    exits 0 even when it drops undecodable frames, so the caller must check
    this to know the decode was complete.
    """
    _, channels, sample_rate = _probe_stream_basics(path)
    if not channels or not sample_rate:
        raise RuntimeError(f"ffprobe could not describe the audio stream in: {path}")
    os.makedirs(temp_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(path))[0]
    raw_path = os.path.join(temp_dir, f"{base}_{os.getpid()}_{abs(hash(path)) % 10**8}.f32raw")
    cmd = [
        "ffmpeg",
        "-v",
        "repeat+error",
        "-nostdin",
        "-y",
        "-i",
        path,
        "-map",
        "0:a:0",
        "-c:a",
        "pcm_f32le",
        "-f",
        "f32le",
        raw_path,
    ]
    try:
        proc = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as exc:
        _remove_files([raw_path])
        raise RuntimeError(f"ffmpeg failed to decode audio from: {path}") from exc
    decode_errors = _count_decoder_errors(proc.stderr or "")
    if proc.returncode != 0:
        _remove_files([raw_path])
        raise RuntimeError(f"ffmpeg failed to decode audio from: {path}")
    frames = os.path.getsize(raw_path) // (4 * channels)
    if frames == 0:
        _remove_files([raw_path])
        raise RuntimeError(f"ffmpeg produced no samples for: {path}")
    audio = np.memmap(raw_path, dtype=np.float32, mode="r", shape=(frames, channels)).T
    if sample_rate == target_sr:
        # Unlink immediately: the mapping stays valid on POSIX and the disk
        # space is reclaimed automatically once the memmap is released.
        try:
            os.remove(raw_path)
            return audio, sample_rate, [], decode_errors
        except OSError:
            return audio, sample_rate, [raw_path], decode_errors
    res_path = raw_path + ".resampled"
    out = None
    for ch in range(channels):
        resampled = _resample_audio(
            np.ascontiguousarray(audio[ch], dtype=np.float32), sample_rate, target_sr
        ).astype(np.float32, copy=False)
        if out is None:
            out = np.memmap(res_path, dtype=np.float32, mode="w+", shape=(channels, resampled.size))
        out[ch, : out.shape[1]] = resampled[: out.shape[1]]
    out.flush()
    del audio
    _remove_files([raw_path])
    try:
        os.remove(res_path)
        return out, target_sr, [], decode_errors
    except OSError:
        return out, target_sr, [res_path], decode_errors


def _load_audio(
    path: str, target_sr: int, temp_dir: str | None = None
) -> tuple[np.ndarray, int, list[str], int | None]:
    """Load audio as a (channels, samples) array.

    Files whose decoded size exceeds _LARGE_DECODE_BYTES are decoded to a
    disk-backed memmap instead of RAM; the returned temp-file list must be
    removed by the caller once analysis is done. The final element is the
    ffmpeg decoder-error count, or None when the file was loaded by
    soundfile/librosa and no error count is available.
    """
    if temp_dir:
        estimated = _estimated_decoded_bytes(path)
        if estimated is not None and estimated > _LARGE_DECODE_BYTES:
            return _decode_to_memmap(path, temp_dir, target_sr)
    try:
        audio, sr = sf.read(path, always_2d=True, dtype="float32")
        audio = audio.T
    except (RuntimeError, sf.LibsndfileError, ValueError):
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="PySoundFile failed.*",
                category=UserWarning,
                module="librosa",
            )
            warnings.filterwarnings(
                "ignore",
                message="librosa.core.audio.__audioread_load.*",
                category=FutureWarning,
                module="librosa",
            )
            audio, sr = librosa.load(path, sr=None, mono=False)
        if audio.ndim == 1:
            audio = audio[np.newaxis, :]

    if sr != target_sr:
        audio = np.stack([_resample_audio(channel, sr, target_sr) for channel in audio], axis=0)
        sr = target_sr

    return audio, sr, [], None


def _mono_mix(y: np.ndarray) -> np.ndarray:
    """Downmix to a mono float32 array held in RAM, reading chunk by chunk."""
    n = y.shape[1]
    if y.shape[0] == 1:
        return np.asarray(y[0], dtype=np.float32)
    out = np.empty(n, dtype=np.float32)
    for s, e in _iter_ranges(0, n, _CHUNK_SAMPLES):
        out[s:e] = np.mean(y[:, s:e], axis=0, dtype=np.float64)
    return out


def _stream_gram(rows: list[np.ndarray], n: int) -> tuple[np.ndarray, np.ndarray]:
    """Accumulate per-row sums and the cross-product matrix chunk by chunk."""
    k = len(rows)
    s1 = np.zeros(k, dtype=np.float64)
    s2 = np.zeros((k, k), dtype=np.float64)
    for s, e in _iter_ranges(0, n, _CORR_CHUNK_SAMPLES):
        seg = np.empty((k, e - s), dtype=np.float64)
        for i, row in enumerate(rows):
            seg[i] = row[s:e]
        s1 += seg.sum(axis=1)
        s2 += seg @ seg.T
    return s1, s2


def _corr_from_gram(s1: np.ndarray, s2: np.ndarray, n: int) -> np.ndarray:
    """Exact correlation matrix from streamed sums (matches np.corrcoef)."""
    cov = s2 / n - np.outer(s1, s1) / (float(n) * n)
    d = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    denom = np.outer(d, d)
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = cov / denom
    return np.nan_to_num(corr)


def _clipped_fraction(rows: np.ndarray, threshold: float) -> float:
    data = rows if rows.ndim > 1 else rows[np.newaxis, :]
    n = data.shape[1]
    if n == 0 or data.shape[0] == 0:
        return 0.0
    clipped = 0
    for s, e in _iter_ranges(0, n, _CHUNK_SAMPLES):
        seg = np.asarray(data[:, s:e])
        clipped += int(np.count_nonzero(np.abs(seg) >= threshold))
    return clipped / float(data.size)


def _sosfilt_stream(
    sos: np.ndarray, row: np.ndarray, out: np.ndarray | None = None
) -> tuple[np.ndarray | None, float]:
    """Chunked sosfilt from rest (zero initial state); returns (out, sum of squares)."""
    zi = np.zeros((sos.shape[0], 2))
    sq = 0.0
    for s, e in _iter_ranges(0, int(row.size), _CHUNK_SAMPLES):
        seg = np.asarray(row[s:e], dtype=np.float64)
        filtered, zi = scipy.signal.sosfilt(sos, seg, zi=zi)
        if out is not None:
            out[s:e] = filtered
        sq += float(np.dot(filtered, filtered))
    return out, sq


def _calculate_dynamic_range(
    y: np.ndarray, sr: int, settings: AnalysisSettings
) -> tuple[float, float, float, int]:
    if y.size == 0:
        return 0.0, 0.0, 0.0, 0
    if y.shape[0] >= 2:
        stereo = y[:2, :]
    else:
        stereo = y[:1, :]
    block_size = int(settings.dr_block_seconds * sr)
    if block_size <= 0:
        return 0.0, 0.0, 0.0, 0
    total_blocks = max(1, stereo.shape[1] // block_size)
    rms_blocks = []
    for idx in range(total_blocks):
        start = idx * block_size
        end = start + block_size
        block = stereo[:, start:end]
        if block.size == 0:
            continue
        block_rms = float(np.sqrt(np.mean(block**2)))
        if block_rms <= 0:
            continue
        block_db = 20 * math.log10(block_rms)
        if block_db >= settings.dr_silence_db:
            rms_blocks.append(block_rms)
    if not rms_blocks:
        return 0.0, 0.0, 0.0, 0
    rms_blocks.sort(reverse=True)
    top_count = max(1, int(math.ceil(len(rms_blocks) * settings.dr_top_percent)))
    top_rms = float(np.mean(rms_blocks[:top_count]))
    peak = float(np.max(np.abs(stereo)))
    dr_db = float(20 * math.log10(max(peak, 1e-12) / max(top_rms, 1e-12)))
    return peak, top_rms, dr_db, top_count


def _calculate_loudness_metrics(
    y_mono: np.ndarray, sr: int, settings: AnalysisSettings
) -> tuple[float, float]:
    if y_mono.size == 0:
        return -120.0, 0.0
    block_size = int(settings.dr_block_seconds * sr)
    if block_size <= 0:
        return -120.0, 0.0
    total_blocks = max(1, y_mono.shape[0] // block_size)
    rms_db_blocks = []
    for idx in range(total_blocks):
        start = idx * block_size
        end = start + block_size
        block = y_mono[start:end]
        if block.size == 0:
            continue
        block_rms = float(np.sqrt(np.mean(block**2)))
        if block_rms <= 0:
            continue
        block_db = 20 * math.log10(block_rms)
        if block_db >= settings.dr_silence_db:
            rms_db_blocks.append(block_db)
    if not rms_db_blocks:
        return -120.0, 0.0
    rms_db_blocks.sort()
    p10 = np.percentile(rms_db_blocks, 10)
    p95 = np.percentile(rms_db_blocks, 95)
    lra_db = float(max(0.0, p95 - p10))
    loudness_db = float(np.mean(rms_db_blocks))
    return loudness_db, lra_db


def _resample_audio(y: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    """Resample with SOXR HQ if available, fallback to librosa."""
    if orig_sr == target_sr:
        return y
    if soxr is not None:
        return soxr.resample(y, orig_sr, target_sr, quality="HQ")
    return librosa.resample(y, orig_sr=orig_sr, target_sr=target_sr)


def _bandpass_mono(y: np.ndarray, sr: int, low_hz: float, high_hz: float) -> np.ndarray:
    """Bandpass filter used for robust alignment correlation."""
    if y.size == 0:
        return y
    low = max(1.0, low_hz) / (sr / 2)
    high = min(high_hz, sr / 2 - 1.0) / (sr / 2)
    if high <= low:
        return y
    sos = scipy.signal.butter(4, [low, high], btype="bandpass", output="sos")
    out = np.empty(int(y.size), dtype=np.float32)
    _sosfilt_stream(sos, y, out)
    return out


def _align_offset(
    ref: np.ndarray, cand: np.ndarray, sr: int
) -> tuple[float, float]:
    """Cross-correlation with peak interpolation for sub-sample offset."""
    if ref.size == 0 or cand.size == 0:
        return 0.0, 0.0
    corr = scipy.signal.fftconvolve(cand, ref[::-1], mode="full")
    peak_idx = int(np.argmax(corr))
    if 1 <= peak_idx < len(corr) - 1:
        left = corr[peak_idx - 1]
        mid = corr[peak_idx]
        right = corr[peak_idx + 1]
        denom = max(1e-12, (left - 2 * mid + right))
        peak_adjust = 0.5 * (left - right) / denom
    else:
        peak_adjust = 0.0
    peak_idx = peak_idx + peak_adjust
    offset_samples = peak_idx - (len(ref) - 1)
    offset_s = float(offset_samples / sr)
    confidence = float(np.max(corr) / (np.mean(np.abs(corr)) + 1e-12))
    return offset_s, confidence


def _apply_offset(
    ref: np.ndarray, cand: np.ndarray, offset_s: float, sr: int
) -> tuple[np.ndarray, np.ndarray]:
    """Trim arrays so aligned sections overlap."""
    offset_samples = int(round(offset_s * sr))
    if offset_samples > 0:
        cand = cand[offset_samples:]
    elif offset_samples < 0:
        ref = ref[abs(offset_samples):]
    min_len = min(ref.size, cand.size)
    return ref[:min_len], cand[:min_len]


def _detect_sync_step(
    offsets_s: np.ndarray,
    confidences: np.ndarray,
    times_s: np.ndarray,
    min_confidence: float,
    warn_ms: float,
) -> dict | None:
    """Find a sustained offset step across the runtime from per-chunk offsets.

    A single global delay silently mis-aligns everything after a mid-stream
    splice; comparing the median offset on each side of every split point
    catches both hard steps and steady drift. Requires enough confident
    chunks on both sides so scene-change noise cannot fake a step.
    """
    mask = confidences >= min_confidence
    if int(mask.sum()) < 12:
        return None
    off_ms = offsets_s[mask] * 1000.0
    times = times_s[mask]
    min_side = max(5, off_ms.size // 20)
    deltas = np.zeros(off_ms.size)
    for i in range(min_side, off_ms.size - min_side):
        deltas[i] = abs(float(np.median(off_ms[i:])) - float(np.median(off_ms[:i])))
    best_delta = float(deltas.max()) if deltas.size else 0.0
    if best_delta < warn_ms:
        return None
    # Every split strictly inside a plateau pair scores the same delta, so
    # break ties by side coherence: the true boundary is where both sides are
    # tightest around their own medians. Mean deviation (not median) so that
    # even a minority of wrong-side chunks pulls the split toward the boundary.
    candidates = np.where(deltas >= best_delta - 0.5)[0]
    best_idx = None
    best_spread = None
    for i in candidates:
        left, right = off_ms[:i], off_ms[i:]
        spread = float(np.mean(np.abs(left - np.median(left)))) + float(
            np.mean(np.abs(right - np.median(right)))
        )
        if best_spread is None or spread < best_spread:
            best_spread = spread
            best_idx = int(i)
    if best_idx is None:
        return None
    before = float(np.median(off_ms[:best_idx]))
    after = float(np.median(off_ms[best_idx:]))
    left = off_ms[:best_idx]
    right = off_ms[best_idx:]
    mad_left = float(np.median(np.abs(left - before)))
    mad_right = float(np.median(np.abs(right - after)))
    # A genuine splice has two tight plateaus (sub-ms spread). Wildly
    # scattered offsets mean the chunk alignment itself is unreliable —
    # report that honestly instead of inventing a step time.
    coherent = mad_left <= 5.0 * warn_ms and mad_right <= 5.0 * warn_ms
    return {
        "kind": "step" if coherent else "inconsistent",
        "time_s": float(times[best_idx]),
        "delta_ms": after - before,
        "before_ms": before,
        "after_ms": after,
    }


def _scc_align_offset(
    ref: np.ndarray,
    cand: np.ndarray,
    sr: int,
    min_confidence: float,
    sync_step_warn_ms: float,
) -> tuple[float, float, bool, str | None, dict | None]:
    """SCC-style chunked correlation alignment with consensus confidence."""
    if ref.size == 0 or cand.size == 0:
        return 0.0, 0.0, True, "Empty audio for alignment.", None
    chunk_seconds = 5.0
    hop_seconds = 2.5
    chunk_len = int(chunk_seconds * sr)
    hop_len = int(hop_seconds * sr)
    if chunk_len <= 0 or hop_len <= 0:
        return 0.0, 0.0, True, "Invalid SCC chunk sizing.", None
    offsets = []
    confidences = []
    chunk_times = []
    max_start = max(0, min(ref.size, cand.size) - chunk_len)
    for start in range(0, max_start + 1, hop_len):
        ref_chunk = ref[start : start + chunk_len]
        cand_chunk = cand[start : start + chunk_len]
        if ref_chunk.size == 0 or cand_chunk.size == 0:
            continue
        corr = scipy.signal.fftconvolve(cand_chunk, ref_chunk[::-1], mode="full")
        peak_idx = int(np.argmax(corr))
        peak_val = float(corr[peak_idx])
        confidence = peak_val / (np.mean(np.abs(corr)) + 1e-12)
        offset_samples = peak_idx - (len(ref_chunk) - 1)
        offsets.append(offset_samples / sr)
        confidences.append(confidence)
        chunk_times.append(start / sr)
    if not offsets:
        return 0.0, 0.0, True, "SCC alignment failed to find chunks.", None
    sync_step = _detect_sync_step(
        np.asarray(offsets),
        np.asarray(confidences),
        np.asarray(chunk_times),
        min_confidence,
        sync_step_warn_ms,
    )
    median_confidence = float(np.median(confidences))
    if median_confidence < min_confidence:
        return 0.0, median_confidence, True, "SCC confidence below threshold.", sync_step
    offset_s = float(np.median(offsets))
    return offset_s, median_confidence, False, None, sync_step
def _stft_frame_count(n_samples: int, n_fft: int, hop_length: int) -> int:
    return 1 + (n_samples + 2 * (n_fft // 2) - n_fft) // hop_length


def _zero_padded_slice(y: np.ndarray, start: int, end: int, pad: int) -> np.ndarray:
    """Slice [start, end) of the signal as if zero-padded by `pad` on each side.

    Matches librosa.stft's default centered framing (pad_mode="constant").
    """
    out = np.zeros(end - start, dtype=np.float32)
    lo = max(start - pad, 0)
    hi = min(end - pad, int(y.size))
    if hi > lo:
        out[lo - (start - pad) : hi - (start - pad)] = y[lo:hi]
    return out


def _iter_stft_mag_chunks(
    y_mono: np.ndarray, settings: AnalysisSettings, frames_per_chunk: int = 4096
):
    """Yield (start_frame, end_frame, |STFT|) chunks matching librosa's centered STFT."""
    n_fft = settings.fft_size
    hop = settings.hop_length
    pad = n_fft // 2
    n = int(y_mono.size)
    total = _stft_frame_count(n, n_fft, hop)
    for k0 in range(0, total, frames_per_chunk):
        k1 = min(k0 + frames_per_chunk, total)
        s = k0 * hop
        e = (k1 - 1) * hop + n_fft
        if s - pad >= 0 and e - pad <= n:
            segment = np.asarray(y_mono[s - pad : e - pad], dtype=np.float32)
        else:
            segment = _zero_padded_slice(y_mono, s, e, pad)
        stft = librosa.stft(segment, n_fft=n_fft, hop_length=hop, center=False)
        yield k0, k1, np.abs(stft)


def _stream_spectral_pass(
    y_mono: np.ndarray,
    sr: int,
    settings: AnalysisSettings,
    mel_basis: np.ndarray | None = None,
) -> tuple[np.ndarray, float, np.ndarray | None, int]:
    """One streaming STFT pass: per-bin mean magnitude, global max power, optional mel."""
    total = _stft_frame_count(int(y_mono.size), settings.fft_size, settings.hop_length)
    mag_sum = np.zeros(settings.fft_size // 2 + 1, dtype=np.float64)
    max_power = 0.0
    mel = (
        np.empty((mel_basis.shape[0], total), dtype=np.float32)
        if mel_basis is not None
        else None
    )
    for k0, k1, mag in _iter_stft_mag_chunks(y_mono, settings):
        mag_sum += mag.sum(axis=1, dtype=np.float64)
        power = mag
        power *= power
        if power.size:
            max_power = max(max_power, float(power.max()))
        if mel is not None:
            mel[:, k0:k1] = mel_basis @ power
    mean_mag = (mag_sum / max(1, total)).astype(np.float32)
    return mean_mag, max_power, mel, total


def _stream_logpower_stats(
    y_mono: np.ndarray,
    sr: int,
    settings: AnalysisSettings,
    max_power: float,
    n_buckets: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Time-mean and time-bucketed log-power spectrum (librosa power_to_db semantics).

    Replaces the full (bins x frames) log-power spectrogram for delta-EQ work:
    the band metrics only need the time mean, and the delta-EQ PNG only needs
    a time-decimated map, so the full array never has to exist.
    """
    amin = 1e-10
    top_db = 80.0
    ref_db = 10.0 * math.log10(max(amin, max_power))
    bins = settings.fft_size // 2 + 1
    total = _stft_frame_count(int(y_mono.size), settings.fft_size, settings.hop_length)
    buckets = max(1, min(n_buckets, total))
    db_sum = np.zeros(bins, dtype=np.float64)
    bucket_sum = np.zeros((bins, buckets), dtype=np.float64)
    bucket_frames = np.zeros(buckets, dtype=np.int64)
    for k0, k1, mag in _iter_stft_mag_chunks(y_mono, settings):
        power = mag
        power *= power
        np.maximum(power, amin, out=power)
        db = 10.0 * np.log10(power)
        db -= ref_db
        np.maximum(db, -top_db, out=db)
        db_sum += db.sum(axis=1, dtype=np.float64)
        frame_buckets = (np.arange(k0, k1, dtype=np.int64) * buckets) // total
        for b in np.unique(frame_buckets):
            mask = frame_buckets == b
            bucket_sum[:, b] += db[:, mask].sum(axis=1, dtype=np.float64)
            bucket_frames[b] += int(mask.sum())
    mean_db = (db_sum / max(1, total)).astype(np.float32)
    bucket_db = (bucket_sum / np.maximum(bucket_frames, 1)).astype(np.float32)
    centers = (np.arange(buckets) + 0.5) * (total / buckets)
    times = librosa.frames_to_time(centers, sr=sr, hop_length=settings.hop_length)
    return mean_db, bucket_db, times


def _robust_level_match_gain_db(
    ref: np.ndarray, cand: np.ndarray, sr: int, silence_db: float
) -> float:
    eps = 1e-12
    win = sr
    if ref.size < win or cand.size < win:
        ref_rms = float(np.sqrt(np.mean(ref * ref) + eps))
        cand_rms = float(np.sqrt(np.mean(cand * cand) + eps))
        return 20.0 * math.log10((ref_rms + eps) / (cand_rms + eps))

    def window_rms(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        n = (x.size // win) * win
        x = x[:n]
        frames = x.reshape(-1, win)
        rms = np.sqrt(np.mean(frames * frames, axis=1) + eps)
        db = 20.0 * np.log10(rms + eps)
        return rms, db

    ref_rms, ref_db = window_rms(ref)
    cand_rms, cand_db = window_rms(cand)
    mask = (ref_db > silence_db) & (cand_db > silence_db)
    if not np.any(mask):
        ref_rms = float(np.sqrt(np.mean(ref * ref) + eps))
        cand_rms = float(np.sqrt(np.mean(cand * cand) + eps))
        return 20.0 * math.log10((ref_rms + eps) / (cand_rms + eps))
    ratio = float(np.median((ref_rms[mask] + eps) / (cand_rms[mask] + eps)))
    return 20.0 * math.log10(ratio + eps)


def _apply_gain_db(x: np.ndarray, gain_db: float) -> np.ndarray:
    g = 10.0 ** (gain_db / 20.0)
    return np.clip(x * g, -1.0, 1.0)


def _mean_spectrum_db(
    y_mono: np.ndarray, sr: int, settings: AnalysisSettings
) -> tuple[np.ndarray, np.ndarray]:
    mean_mag, _, _, _ = _stream_spectral_pass(y_mono, sr, settings)
    mag_db = librosa.amplitude_to_db(mean_mag, ref=np.max)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=settings.fft_size)
    return freqs, mag_db


def _spectral_cutoff(
    freqs: np.ndarray, mag_db: np.ndarray, settings: AnalysisSettings
) -> tuple[float, bool, bool]:
    threshold_db = np.max(mag_db) - settings.cutoff_db_below_peak
    valid = np.where(mag_db >= threshold_db)[0]
    cutoff_hz = float(freqs[valid[-1]]) if valid.size else 0.0

    def shelf_drop(center_hz: float) -> float:
        low_band = (freqs >= center_hz - 1000) & (freqs <= center_hz)
        high_band = (freqs >= center_hz) & (freqs <= settings.shelf_high_hz)
        low_mean = float(np.mean(mag_db[low_band])) if np.any(low_band) else -120.0
        high_mean = float(np.mean(mag_db[high_band])) if np.any(high_band) else -120.0
        return low_mean - high_mean

    drop_16k = shelf_drop(settings.shelf_low_hz)
    drop_17k = shelf_drop(settings.shelf_low_hz + 1000)
    shelf_detected = max(drop_16k, drop_17k) >= settings.shelf_drop_db
    above_18k = freqs >= settings.shelf_high_hz
    high_energy = float(np.mean(mag_db[above_18k])) if np.any(above_18k) else -120.0
    reencode_detected = high_energy <= -80.0
    return cutoff_hz, shelf_detected, reencode_detected


def _dialog_balance_db(
    freqs: np.ndarray, mag_db: np.ndarray, settings: AnalysisSettings
) -> float:
    dialog_band = (freqs >= settings.dialog_band_low_hz) & (freqs <= settings.dialog_band_high_hz)
    presence_band = (freqs >= settings.presence_band_low_hz) & (
        freqs <= settings.presence_band_high_hz
    )
    dialog_mean = float(np.mean(mag_db[dialog_band])) if np.any(dialog_band) else -120.0
    presence_mean = float(np.mean(mag_db[presence_band])) if np.any(presence_band) else -120.0
    return dialog_mean - presence_mean


def _detect_nr_filtered(
    mag_db: np.ndarray,
    freqs: np.ndarray,
    settings: AnalysisSettings,
    reference_mag_db: np.ndarray | None = None,
) -> float:
    """
    Returns NR severity as a float from 0.0 (no NR) to 1.0 (heavy NR).
    Remuxer thresholds: 0-3 dB = mild, 4-7 dB = moderate, ≥8 dB = severe
    Takes into account reference if provided - only penalizes if worse than reference.
    """
    # Check critical frequency ranges where NR is most audible
    critical_band = (freqs >= 6000) & (freqs <= 12000)  # 6-12 kHz where NR is most noticeable
    low_nr_band = (freqs >= 2000) & (freqs <= 4000)  # Low-freq NR starting here is severe
    mid_band = (freqs >= settings.dialog_band_low_hz) & (freqs <= settings.dialog_band_high_hz)

    critical_mean = float(np.mean(mag_db[critical_band])) if np.any(critical_band) else -120.0
    low_nr_mean = float(np.mean(mag_db[low_nr_band])) if np.any(low_nr_band) else -120.0
    mid_mean = float(np.mean(mag_db[mid_band])) if np.any(mid_band) else -120.0

    # If we have a reference, compare relative to it
    if reference_mag_db is not None:
        ref_critical = float(np.mean(reference_mag_db[critical_band])) if np.any(critical_band) else -120.0
        ref_low_nr = float(np.mean(reference_mag_db[low_nr_band])) if np.any(low_nr_band) else -120.0

        # Calculate HF loss vs reference (6-12 kHz range)
        hf_drop_db = ref_critical - critical_mean
        # Check if NR starts low (2-4 kHz range)
        low_drop_db = ref_low_nr - low_nr_mean

        # Only consider it NR if worse than reference
        if hf_drop_db <= 0 and low_drop_db <= 0:
            return 0.0

        # Map to remuxer severity scale
        # 0-3 dB = mild (0.2), 4-7 dB = moderate (0.5), 8+ dB = severe (0.8-1.0)
        max_drop = max(hf_drop_db, low_drop_db)

        if max_drop < 3.0:
            severity = max_drop / 15.0  # 0-3 dB → 0.0-0.2
        elif max_drop < 7.0:
            severity = 0.2 + (max_drop - 3.0) / 8.0  # 4-7 dB → 0.2-0.7
        else:
            severity = 0.7 + min(0.3, (max_drop - 7.0) / 20.0)  # 8+ dB → 0.7-1.0

        # Extra penalty if NR starts low (≤3 kHz)
        if low_drop_db >= 4.0:
            severity = min(1.0, severity + 0.2)

        return min(1.0, severity)

    # No reference: use absolute threshold (mid-to-critical ratio)
    ratio_db = mid_mean - critical_mean
    if ratio_db < settings.nr_ratio_db:
        return 0.0

    # Scale severity based on ratio
    severity = min(1.0, (ratio_db - settings.nr_ratio_db) / 12.0)
    return severity


def _evaluate_eq_delta(
    freqs: np.ndarray,
    mean_delta: np.ndarray,
    settings: AnalysisSettings,
) -> tuple[float, float, list[str]]:
    warnings: list[str] = []
    muffle_band = (freqs >= settings.eq_muffle_low_hz) & (freqs <= settings.eq_muffle_high_hz)
    boom_band = (freqs >= settings.eq_boom_center_hz - settings.eq_boom_band_hz) & (
        freqs <= settings.eq_boom_center_hz + settings.eq_boom_band_hz
    )
    muffle_db = float(np.mean(mean_delta[muffle_band])) if np.any(muffle_band) else 0.0
    boom_db = float(np.mean(mean_delta[boom_band])) if np.any(boom_band) else 0.0
    if muffle_db <= -settings.eq_muffle_drop_db:
        warnings.append("NR/Muffleness (2-7 kHz drop)")
    if boom_db >= settings.eq_boom_boost_db:
        warnings.append("Boominess (~120 Hz boost)")
    return muffle_db, boom_db, warnings


def _estimate_f0(
    y_mono: np.ndarray, sr: int, settings: AnalysisSettings
) -> float | None:
    if y_mono.size == 0:
        return None
    segment_length = int(settings.f0_segment_seconds * sr)
    if segment_length <= 0:
        return None
    start = int(settings.f0_segment_offset_ratio * y_mono.size)
    start = max(0, min(start, max(0, y_mono.size - segment_length)))
    segment = y_mono[start : start + segment_length]
    if segment.size == 0:
        return None
    f0 = librosa.yin(
        segment,
        fmin=80.0,
        fmax=400.0,
        sr=sr,
        frame_length=2048,
        hop_length=512,
    )
    f0 = f0[np.isfinite(f0)]
    if f0.size == 0:
        return None
    return float(np.median(f0))


def _detect_pitch_speed_shift(
    f0_ref: float | None, f0_candidate: float | None, settings: AnalysisSettings
) -> tuple[float | None, bool, bool]:
    if not f0_ref or not f0_candidate or f0_ref <= 0:
        return None, False, False
    ratio = f0_candidate / f0_ref
    pal_ratio = settings.pal_speed_ratio
    semitone_ratio = 2 ** (settings.pitch_semitone_shift / 12.0)
    tol = settings.pitch_tolerance_ratio
    speed_shift = abs(ratio - pal_ratio) <= pal_ratio * tol
    pitch_shift = abs(ratio - semitone_ratio) <= semitone_ratio * tol
    return ratio, speed_shift, pitch_shift


def _detect_surround_swaps(
    ref_audio: np.ndarray | None, cand_audio: np.ndarray, settings: AnalysisSettings
) -> bool:
    if ref_audio is None:
        return False
    if ref_audio.shape[0] < 5 or cand_audio.shape[0] < 5:
        return False
    n = min(ref_audio.shape[1], cand_audio.shape[1])
    if n == 0:
        return False
    rows = [ref_audio[i] for i in range(5)] + [cand_audio[i] for i in range(5)]
    s1, s2 = _stream_gram(rows, n)
    corr = _corr_from_gram(s1, s2, n)
    corr_block = corr[:5, 5:]
    max_indices = np.argmax(np.abs(corr_block), axis=1)
    swaps = sum(idx != i for i, idx in enumerate(max_indices))
    return swaps > 0 and np.max(np.abs(corr_block)) >= settings.channel_swap_corr_threshold


def _detect_lfe_rolloff(
    y: np.ndarray, sr: int, settings: AnalysisSettings, lfe_idx: int | None
) -> bool:
    if lfe_idx is None or y.shape[0] <= lfe_idx:
        return False
    lfe = y[lfe_idx]
    freqs, mag_db = _mean_spectrum_db(lfe, sr, settings)
    low_band = freqs <= settings.lfe_rolloff_hz
    high_band = freqs >= settings.lfe_rolloff_hz
    low_mean = float(np.mean(mag_db[low_band])) if np.any(low_band) else -120.0
    high_mean = float(np.mean(mag_db[high_band])) if np.any(high_band) else -120.0
    ratio = high_mean - low_mean
    return ratio >= settings.lfe_high_ratio_db


def _detect_limiting_segments(
    y: np.ndarray, sr: int, settings: AnalysisSettings
) -> tuple[list[float], list[tuple[float, float]]]:
    if y.size == 0:
        return [], []
    window_samples = int(sr * settings.limiting_window_ms / 1000.0)
    if window_samples <= 0:
        return [], []
    rows = y if y.ndim > 1 else y[np.newaxis, :]
    n = rows.shape[1]
    total_windows = max(1, n // window_samples)
    ratios: list[float] = []
    segments: list[tuple[float, float]] = []
    group = max(1, _CHUNK_SAMPLES // window_samples)
    for w0 in range(0, total_windows, group):
        w1 = min(total_windows, w0 + group)
        s = w0 * window_samples
        e = min(n, w1 * window_samples)
        flat = np.max(np.abs(np.asarray(rows[:, s:e], dtype=np.float32)), axis=0)
        for idx in range(w1 - w0):
            window = flat[idx * window_samples : (idx + 1) * window_samples]
            if window.size == 0:
                ratios.append(0.0)
                continue
            ratio = float(np.mean(window >= settings.clip_threshold))
            ratios.append(ratio)
            if ratio >= settings.limiting_ratio:
                start = (w0 + idx) * window_samples
                segments.append((start / sr, (start + window_samples) / sr))
    return ratios, segments


def _save_delta_eq_map(
    freqs: np.ndarray,
    times: np.ndarray,
    delta_db: np.ndarray,
    out_path: str,
) -> None:
    fig, ax = plt.subplots(figsize=(7, 3), dpi=200)
    extent = [times[0], times[-1], freqs[0], freqs[-1]]
    ax.imshow(delta_db, aspect="auto", origin="lower", extent=extent, cmap="coolwarm", vmin=-6, vmax=6)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Hz")
    ax.set_title("Delta EQ Map (Candidate - Reference)")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def _save_limiting_heatmap(
    ratios: list[float], window_seconds: float, out_path: str
) -> None:
    fig, ax = plt.subplots(figsize=(7, 2.2), dpi=200)
    times = np.arange(len(ratios)) * window_seconds
    ax.bar(times, ratios, width=window_seconds, color="#b22222", alpha=0.7)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Limited ratio")
    ax.set_title("Limiting Heatmap")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def _save_waveform_zoom(
    y: np.ndarray, sr: int, segment: tuple[float, float], out_path: str
) -> None:
    start, end = segment
    start_idx = int(start * sr)
    end_idx = int(end * sr)
    segment_audio = y[:, start_idx:end_idx] if y.ndim > 1 else y[start_idx:end_idx]
    if segment_audio.size == 0:
        return
    fig, ax = plt.subplots(figsize=(7, 2.2), dpi=200)
    if segment_audio.ndim > 1:
        mono = np.mean(segment_audio, axis=0)
    else:
        mono = segment_audio
    times = np.linspace(start, end, mono.size)
    ax.plot(times, mono, color="#1f77b4", linewidth=0.8)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Amplitude")
    ax.set_title(f"Waveform Zoom {start:.2f}s-{end:.2f}s")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def _scan_glitches(y: np.ndarray, sr: int, settings: AnalysisSettings) -> list[float]:
    if y.size == 0:
        return []
    rows = y if y.ndim > 1 else y[np.newaxis, :]
    n = rows.shape[1]
    if n < 2:
        return []

    def iter_diff_chunks():
        for s, e in _iter_ranges(1, n, _CHUNK_SAMPLES):
            seg = np.asarray(rows[:, s - 1 : e], dtype=np.float32)
            diff = np.abs(np.diff(seg, axis=1))
            yield s - 1, np.max(diff, axis=0)

    count = 0
    total = 0.0
    total_sq = 0.0
    for _, dmax in iter_diff_chunks():
        count += dmax.size
        d64 = dmax.astype(np.float64)
        total += float(d64.sum())
        total_sq += float(np.dot(d64, d64))
    if count == 0:
        return []
    mean = total / count
    std = math.sqrt(max(0.0, total_sq / count - mean * mean))
    threshold = max(settings.glitch_diff_threshold, mean + 6.0 * std)
    timestamps: list[float] = []
    for offset, dmax in iter_diff_chunks():
        for idx in np.where(dmax >= threshold)[0]:
            timestamps.append(float((offset + int(idx)) / sr))
            if len(timestamps) >= settings.glitch_max_count:
                return timestamps
    return timestamps


def _measure_stereo_width(y: np.ndarray) -> float:
    """
    Measure stereo width/soundstage using mid-side analysis.
    Returns a value from 0.0 (mono/narrow) to 1.0 (wide stereo).
    Based on the ratio of side (difference) to mid (sum) energy.
    """
    if y.shape[0] < 2:
        return 0.0  # Mono or no stereo
    n = y.shape[1]
    if n == 0:
        return 0.0

    # Mid-Side energy, accumulated chunk by chunk
    mid_sq = 0.0
    side_sq = 0.0
    for s, e in _iter_ranges(0, n, _CHUNK_SAMPLES):
        left = np.asarray(y[0, s:e], dtype=np.float64)
        right = np.asarray(y[1, s:e], dtype=np.float64)
        mid = (left + right) / 2.0
        side = (left - right) / 2.0
        mid_sq += float(np.dot(mid, mid))
        side_sq += float(np.dot(side, side))

    mid_rms = math.sqrt(mid_sq / n)
    side_rms = math.sqrt(side_sq / n)

    # Avoid division by zero
    if mid_rms < 1e-12:
        return 0.0

    # Width ratio: side/mid, normalized to 0-1 (typical music has 0.3-0.8;
    # above 0.8 = very wide, below 0.3 = narrow)
    width_ratio = side_rms / mid_rms
    return min(1.0, width_ratio / 0.8)


def _detect_lr_imbalance(y: np.ndarray, sr: int) -> tuple[float, float, bool]:
    """
    Detect L/R channel imbalance issues (remuxer issue #7).
    Returns: (level_imbalance_db, noise_floor_diff_db, has_imbalance)

    Detection signals:
    - RMS level mismatch between L/R
    - Noise floor mismatch (one channel noisier)
    - Spectral asymmetry
    """
    if y.shape[0] < 2:
        return 0.0, 0.0, False
    n = y.shape[1]
    if n == 0:
        return 0.0, 0.0, False

    def channel_stats(row: np.ndarray) -> tuple[float, float]:
        # RMS level, accumulated chunk by chunk
        sq = 0.0
        for s, e in _iter_ranges(0, n, _CHUNK_SAMPLES):
            seg = np.asarray(row[s:e], dtype=np.float64)
            sq += float(np.dot(seg, seg))
        rms = math.sqrt(sq / n)
        # Noise floor: mean of the bottom 10% of |samples| (in-place partition
        # keeps this to a single float32 copy of the channel)
        k = int(n * 0.1)
        noise = 0.0
        if k > 0:
            magnitudes = np.abs(np.asarray(row, dtype=np.float32))
            magnitudes.partition(k - 1)
            noise = float(np.mean(magnitudes[:k], dtype=np.float64))
        return rms, noise

    left_rms, left_noise = channel_stats(y[0])
    right_rms, right_noise = channel_stats(y[1])

    if left_rms < 1e-12 or right_rms < 1e-12:
        return 0.0, 0.0, False

    level_imbalance_db = 20 * math.log10(max(left_rms, right_rms) / min(left_rms, right_rms))

    if left_noise > 1e-12 and right_noise > 1e-12:
        noise_floor_diff_db = 20 * math.log10(max(left_noise, right_noise) / min(left_noise, right_noise))
    else:
        noise_floor_diff_db = 0.0

    # Flag as imbalanced if either metric exceeds threshold
    has_imbalance = level_imbalance_db > 1.5 or noise_floor_diff_db > 3.0

    return level_imbalance_db, noise_floor_diff_db, has_imbalance


def _detect_center_bass_loss(y: np.ndarray, sr: int, center_idx: int | None, reference_y: np.ndarray | None) -> float:
    """
    Detect bass stripping in center channel (remuxer issue #5).
    Returns bass attenuation in dB (0 = no loss, positive values = loss).

    Checks for 6-10 dB bass loss in 20-100 Hz range vs reference or vs L/R channels.
    """
    if center_idx is None or center_idx >= y.shape[0]:
        return 0.0
    n = y.shape[1]
    if n == 0:
        return 0.0

    # Bandpass 20-100 Hz
    nyq = sr / 2.0
    low = 20.0 / nyq
    high = min(100.0 / nyq, 0.99)

    if low >= high or low <= 0 or high >= 1.0:
        return 0.0

    try:
        sos = scipy.signal.butter(4, [low, high], btype='band', output='sos')
        _, center_sq = _sosfilt_stream(sos, y[center_idx])
    except Exception:
        return 0.0

    center_bass_rms = math.sqrt(center_sq / n)

    # Compare to reference if available
    if reference_y is not None and center_idx < reference_y.shape[0]:
        try:
            _, ref_sq = _sosfilt_stream(sos, reference_y[center_idx])
            ref_bass_rms = math.sqrt(ref_sq / reference_y.shape[1])

            if ref_bass_rms > 1e-12 and center_bass_rms > 1e-12:
                bass_loss_db = 20 * math.log10(ref_bass_rms / center_bass_rms)
                return max(0.0, bass_loss_db)
        except Exception:
            pass

    # No reference: compare center bass to L/R bass average
    if y.shape[0] >= 2:
        try:
            _, left_sq = _sosfilt_stream(sos, y[0])
            _, right_sq = _sosfilt_stream(sos, y[1])

            lr_bass_rms = math.sqrt((left_sq + right_sq) / (2.0 * n))

            if lr_bass_rms > 1e-12 and center_bass_rms > 1e-12:
                bass_loss_db = 20 * math.log10(lr_bass_rms / center_bass_rms)
                # Only flag if significantly lower (center naturally has less bass)
                return max(0.0, bass_loss_db - 3.0)  # -3 dB tolerance
        except Exception:
            pass

    return 0.0


def _analyze_channel_structure(
    y: np.ndarray, sr: int, settings: AnalysisSettings
) -> dict | None:
    """Full-file streamed channel correlation/covariance and per-channel RMS.

    Digitally silent stretches (1 s blocks whose all-channel RMS is under the
    silence floor) are excluded from the accumulation: silence tells us
    nothing about channel structure and dilutes the correlations. Every
    non-silent sample in the file is counted — this is not a sampling scheme.
    """
    if y.ndim < 2 or y.shape[0] < 2 or y.shape[1] == 0 or sr <= 0:
        return None
    k, n = y.shape
    block = int(sr)
    s1 = np.zeros(k, dtype=np.float64)
    s2 = np.zeros((k, k), dtype=np.float64)
    count = 0
    for s, e in _iter_ranges(0, n, block):
        seg = np.asarray(y[:, s:e], dtype=np.float64)
        block_rms = math.sqrt(float(np.mean(seg * seg)))
        if 20.0 * math.log10(max(block_rms, 1e-12)) < settings.dr_silence_db:
            continue
        s1 += seg.sum(axis=1)
        s2 += seg @ seg.T
        count += seg.shape[1]
    if count < sr * 5:
        return None
    corr = _corr_from_gram(s1, s2, count)
    cov = s2 / count - np.outer(s1, s1) / (float(count) * count)
    rms = np.sqrt(np.clip(np.diag(s2) / count, 0.0, None))
    rms_db = 20.0 * np.log10(np.maximum(rms, 1e-12))
    return {"corr": corr, "cov": cov, "rms_db": rms_db, "seconds": count / sr}


def _evaluate_fake_multichannel(
    structure: dict | None,
    chmap: dict[str, int],
    settings: AnalysisSettings,
) -> tuple[bool, list[str], dict[str, float], bool, list[float]]:
    """Judge channel structure the way a manual reviewer would.

    Returns (fake, reasons, pair_correlations, lfe_dead, per_channel_rms_db).
    A matrix upmix shows near-1.0 correlation on specific pairs (mono fronts,
    duplicated center, mono/difference surrounds) and often a dead LFE —
    true discrete mixes measure well under the threshold on real content.
    """
    if structure is None:
        return False, [], {}, False, []
    corr = structure["corr"]
    cov = structure["cov"]
    rms_db = structure["rms_db"]
    thr = settings.pair_corr_fake_threshold
    pairs: dict[str, float] = {}
    reasons: list[str] = []

    def pair_corr(a: str, b: str) -> float | None:
        ia, ib = chmap.get(a), chmap.get(b)
        if ia is None or ib is None or ia >= corr.shape[0] or ib >= corr.shape[0]:
            return None
        value = float(corr[ia, ib])
        pairs[f"{a}-{b}"] = value
        return value

    def corr_with_front_diff(name: str) -> float | None:
        """Correlation of a channel against the FL-FR difference signal."""
        i, fl, fr = chmap.get(name), chmap.get("FL"), chmap.get("FR")
        if i is None or fl is None or fr is None:
            return None
        var_d = cov[fl, fl] + cov[fr, fr] - 2.0 * cov[fl, fr]
        denom = math.sqrt(max(var_d, 0.0) * max(cov[i, i], 0.0))
        if denom <= 1e-18:
            return None
        value = float((cov[i, fl] - cov[i, fr]) / denom)
        pairs[f"{name}-(FL-FR)"] = value
        return value

    fl_fr = pair_corr("FL", "FR")
    fc_fl = pair_corr("FC", "FL")
    fc_fr = pair_corr("FC", "FR")
    surround_pair = None
    for a, b in (("BL", "BR"), ("SL", "SR")):
        if chmap.get(a) is not None and chmap.get(b) is not None:
            surround_pair = pair_corr(a, b)
            break

    mono_fronts = fl_fr is not None and fl_fr >= thr
    center_dup = (
        fc_fl is not None and fc_fr is not None and fc_fl >= thr and fc_fr >= thr
    )
    mono_surrounds = surround_pair is not None and surround_pair >= thr

    matrix_surrounds = False
    for name in ("BL", "BR", "SL", "SR"):
        diff_corr = corr_with_front_diff(name)
        if diff_corr is not None and abs(diff_corr) >= thr:
            matrix_surrounds = True

    lfe_idx = chmap.get("LFE")
    lfe_dead = (
        lfe_idx is not None
        and lfe_idx < rms_db.shape[0]
        and float(rms_db[lfe_idx]) < settings.lfe_dead_rms_db
    )

    if mono_fronts:
        reasons.append(f"mono fronts (corr FL-FR {fl_fr:.3f})")
    if center_dup:
        reasons.append(f"duplicated center (corr FC-FL {fc_fl:.3f})")
    if mono_surrounds:
        reasons.append(f"mono/matrix surrounds (corr {surround_pair:.3f})")
    if matrix_surrounds:
        reasons.append("surrounds are an FL-FR difference signal (matrix upmix)")
    if lfe_dead:
        reasons.append(f"dead LFE ({float(rms_db[lfe_idx]):.0f} dB RMS)")

    has_center_or_surrounds = (
        chmap.get("FC") is not None or surround_pair is not None or lfe_idx is not None
    )
    fake = (
        has_center_or_surrounds
        and mono_fronts
        and (center_dup or mono_surrounds or matrix_surrounds or lfe_dead)
    )
    if not fake:
        # A dead LFE or matrix surrounds without mono fronts still deserve
        # their reasons in the report, but only the combinations above are
        # conclusive enough to call the track fake.
        reasons = [r for r in reasons if r.startswith("dead LFE") or "matrix" in r]
    return fake, reasons, pairs, lfe_dead, [float(v) for v in rms_db]


def _score_mastering_accuracy(mean_abs_diff_db: float, settings: AnalysisSettings) -> float:
    return max(0.0, 100.0 - mean_abs_diff_db * settings.mastering_diff_penalty_db)


def _score_dialogue_clarity(
    center_clipping_ratio: float,
    center_nr_filtered: float,
    settings: AnalysisSettings,
) -> float:
    score = 100.0
    if center_clipping_ratio > 0:
        score -= min(60.0, center_clipping_ratio * 100000)
    # Scale NR penalty by severity (0.0-1.0)
    if center_nr_filtered > 0:
        score -= center_nr_filtered * settings.dialogue_clarity_penalty
    return max(0.0, score)


def _save_difference_spectrum(
    freqs: np.ndarray,
    diff_db: np.ndarray,
    out_path: str,
) -> None:
    fig, ax = plt.subplots(figsize=(7, 3), dpi=200)
    ax.plot(freqs / 1000.0, diff_db, color="#1f77b4", linewidth=1.0)
    ax.axhline(0.0, color="#888888", linewidth=0.8, linestyle="--")
    ax.set_xlabel("Frequency (kHz)")
    ax.set_ylabel("Diff (dB)")
    ax.set_title("Difference Spectrum (Target - Reference)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def _save_clipping_heatmap(
    clipping_series: list[float],
    block_seconds: float,
    out_path: str,
) -> None:
    fig, ax = plt.subplots(figsize=(7, 2.2), dpi=200)
    times = np.arange(len(clipping_series)) * block_seconds
    ax.bar(times, clipping_series, width=block_seconds, color="#d62728", alpha=0.7)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Clip ratio")
    ax.set_title("Clipping Heatmap")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
def _detect_clipping(y: np.ndarray, settings: AnalysisSettings) -> tuple[float, bool]:
    if y.size == 0:
        return 0.0, False
    clipping_ratio = _clipped_fraction(y, settings.clip_threshold)
    return clipping_ratio, clipping_ratio >= settings.clip_ratio_warn


def _detect_phase_inversion(y: np.ndarray, settings: AnalysisSettings) -> bool:
    if y.shape[0] < 2:
        return False
    n = y.shape[1]
    if n == 0:
        return False
    s1, s2 = _stream_gram([y[0], y[1]], n)
    corr = _corr_from_gram(s1, s2, n)[0, 1]
    return bool(corr <= settings.phase_inversion_threshold)


def _is_lossless_codec(codec_name: str | None, profile: str | None) -> bool | None:
    if not codec_name:
        return None
    codec = codec_name.lower()
    profile_text = (profile or "").lower()
    lossless_codecs = {
        "flac",
        "alac",
        "wavpack",
        "ape",
        "tta",
        "truehd",
        "mlp",
        "dts_hd_ma",
    }
    if codec.startswith("pcm_"):
        return True
    if codec in lossless_codecs:
        return True
    if codec == "dts" and "hd ma" in profile_text:
        return True
    lossy_codecs = {
        "aac",
        "ac3",
        "eac3",
        "mp3",
        "opus",
        "vorbis",
        "dts",
    }
    if codec in lossy_codecs:
        return False
    return None


def _probe_audio_codec(path: str) -> tuple[str | None, str | None, bool | None]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,profile,codec_long_name",
        "-of",
        "json",
        path,
    ]
    try:
        output = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None, None, None
    try:
        data = json.loads(output)
        streams = data.get("streams", [])
        if not streams:
            return None, None, None
        stream = streams[0]
        codec_name = stream.get("codec_name")
        profile = stream.get("profile")
        codec_long_name = stream.get("codec_long_name")
        codec_display = codec_long_name or codec_name
        return codec_display, profile, _is_lossless_codec(codec_name, profile)
    except (ValueError, TypeError):
        return None, None, None


def _probe_audio_metadata(path: str) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=channels,channel_layout,sample_rate,bit_rate,codec_name,profile,duration",
        "-show_entries",
        "format=duration:format_tags=ENCODER",
        "-of",
        "json",
        path,
    ]
    try:
        output = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {}
    try:
        data = json.loads(output)
    except (ValueError, TypeError):
        return {}
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    tags = fmt.get("tags", {}) if isinstance(fmt, dict) else {}
    stream = streams[0] if streams else {}
    return {
        "channels": stream.get("channels"),
        "channel_layout": stream.get("channel_layout"),
        "sample_rate": stream.get("sample_rate"),
        "bit_rate": stream.get("bit_rate"),
        "codec_name": stream.get("codec_name"),
        "profile": stream.get("profile"),
        "duration": fmt.get("duration"),
        "stream_duration": stream.get("duration"),
        "encoder": tags.get("ENCODER") if isinstance(tags, dict) else None,
    }


_LAYOUT_ORDERS = {
    "mono": ["FC"],
    "stereo": ["FL", "FR"],
    "2.1": ["FL", "FR", "LFE"],
    "3.0": ["FL", "FR", "FC"],
    "3.1": ["FL", "FR", "FC", "LFE"],
    "4.0": ["FL", "FR", "FC", "BC"],
    "4.1": ["FL", "FR", "FC", "LFE", "BC"],
    "quad": ["FL", "FR", "BL", "BR"],
    "5.0": ["FL", "FR", "FC", "BL", "BR"],
    "5.0(side)": ["FL", "FR", "FC", "SL", "SR"],
    "5.1": ["FL", "FR", "FC", "LFE", "BL", "BR"],
    "5.1(side)": ["FL", "FR", "FC", "LFE", "SL", "SR"],
    "6.1": ["FL", "FR", "FC", "LFE", "BC", "SL", "SR"],
    "7.1": ["FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"],
}

# Fallback orders (ffmpeg default layouts) when ffprobe gives no layout string.
_DEFAULT_ORDERS = {
    1: ["FC"],
    2: ["FL", "FR"],
    3: ["FL", "FR", "FC"],
    4: ["FL", "FR", "FC", "BC"],
    5: ["FL", "FR", "FC", "BL", "BR"],
    6: ["FL", "FR", "FC", "LFE", "BL", "BR"],
    7: ["FL", "FR", "FC", "LFE", "BC", "SL", "SR"],
    8: ["FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"],
}


def _layout_channel_map(channel_layout: str | None, channels: int) -> dict[str, int]:
    """Map semantic channel names (FL, FR, FC, LFE, BL/BR, SL/SR) to indices."""
    order = None
    if channel_layout:
        order = _LAYOUT_ORDERS.get(channel_layout.lower())
    if order is None:
        order = _DEFAULT_ORDERS.get(channels)
    if not order:
        return {}
    return {name: idx for idx, name in enumerate(order) if idx < channels}

def _probe_audio_bitrate(path: str) -> float | None:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=bit_rate",
        "-of",
        "json",
        path,
    ]
    try:
        output = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    try:
        data = json.loads(output)
        streams = data.get("streams", [])
        if not streams:
            return None
        bit_rate = streams[0].get("bit_rate")
        return float(bit_rate) if bit_rate else None
    except (ValueError, TypeError):
        return None


def _estimate_bitrate(path: str, duration_s: float) -> float | None:
    if duration_s <= 0:
        return None
    bit_rate = _probe_audio_bitrate(path)
    if bit_rate is not None:
        return bit_rate / 1000.0
    try:
        size_bytes = os.path.getsize(path)
    except OSError:
        return None
    return float((size_bytes * 8) / duration_s / 1000.0)


def _detect_bitrate_bloat(
    path: str, bitrate_kbps: float | None, cutoff_hz: float, sr: int, settings: AnalysisSettings
) -> bool:
    if bitrate_kbps is None:
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext in {".flac", ".wav", ".aiff", ".alac"}:
        return False
    nyquist = sr / 2
    if nyquist <= 0:
        return False
    cutoff_ratio = cutoff_hz / nyquist
    expected = settings.bitrate_reference_kbps * max(0.2, min(cutoff_ratio, 1.0))
    return bitrate_kbps > expected * 1.3


def _score_metric(value: float, max_value: float) -> float:
    if max_value <= 0:
        return 0.0
    return max(0.0, min(100.0, (value / max_value) * 100.0))


def _score_dynamic_range(dr_db: float, settings: AnalysisSettings) -> float:
    """
    Score crest factor DR based on remuxer standards:
    - ≥16 dB = good (100)
    - 13-15 dB = compromised (70-90)
    - ≤12 dB = severe (0-60)
    Higher DR is always better, no penalty for very dynamic content.
    """
    if dr_db <= 0:
        return 0.0
    if dr_db < settings.brickwall_dr_db:  # <8 dB = brickwalled
        return 0.0
    if dr_db < settings.target_dr_min:  # 8-13 dB = poor
        span = settings.target_dr_min - settings.brickwall_dr_db
        return (dr_db - settings.brickwall_dr_db) / span * 60.0
    if dr_db < settings.target_dr_max:  # 13-16 dB = compromised
        span = settings.target_dr_max - settings.target_dr_min
        return 60.0 + (dr_db - settings.target_dr_min) / span * 40.0
    # ≥16 dB = excellent, no penalty for high DR
    return 100.0


def _score_loudness_range(lra_db: float, settings: AnalysisSettings) -> float:
    if lra_db <= 0:
        return 0.0
    if lra_db < settings.lra_target_min:
        span = max(1.0, settings.lra_target_min)
        return max(0.0, (lra_db / span) * 80.0)
    if lra_db <= settings.lra_target_max:
        return 100.0
    excess = lra_db - settings.lra_target_max
    return max(70.0, 100.0 - excess * settings.lra_high_penalty_db)


def _grade_score(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def _weighted_score(
    freq_score: float,
    dr_score: float,
    cleanliness_score: float,
    efficiency_score: float,
    format_score: float,
    dialogue_score: float,
    mastering_score: float,
    settings: AnalysisSettings,
) -> float:
    return (
        freq_score * settings.weight_frequency / 100.0
        + dr_score * settings.weight_dynamic_range / 100.0
        + cleanliness_score * settings.weight_cleanliness / 100.0
        + efficiency_score * settings.weight_efficiency / 100.0
        + format_score * settings.weight_format / 100.0
        + dialogue_score * settings.weight_dialogue / 100.0
        + mastering_score * settings.weight_mastering / 100.0
    )


def _penalty_points_from_count(count: int, soft: int, hard: int, max_penalty: float) -> float:
    if count <= soft:
        return 0.0
    if count >= hard:
        return max_penalty
    t = (count - soft) / max(1, (hard - soft))
    return max_penalty * (t * t)


def _weighted_score_with_penalties(
    base_score: float, result: AudioAnalysisResult, settings: AnalysisSettings
) -> tuple[float, dict]:
    penalties: dict[str, float] = {}
    total_penalty = 0.0

    # NR penalty: scaled by severity (0.0-1.0)
    # For lossless files, reduce penalty by 75% (likely source characteristic)
    if result.nr_filtered > 0:
        nr_multiplier = 0.25 if result.is_lossless else 1.0
        max_nr_penalty = 20.0
        nr_penalty = result.nr_filtered * max_nr_penalty * nr_multiplier
        if nr_penalty > 1.0:  # Only add if meaningful
            penalties["noise_reduction"] = nr_penalty
            total_penalty += nr_penalty

    muffle_thresh = settings.eq_muffle_drop_db
    if result.eq_muffle_db <= -muffle_thresh:
        extra = min(1.0, (-result.eq_muffle_db - muffle_thresh) / max(1e-6, muffle_thresh))
        p = 10.0 + 10.0 * extra
        penalties["muffled_eq"] = p
        total_penalty += p

    limiting_hotspots = len(result.limiting_segments) if result.limiting_segments else 0
    p_lim = _penalty_points_from_count(limiting_hotspots, soft=1, hard=30, max_penalty=25.0)
    if p_lim > 0:
        penalties["sustained_limiting"] = p_lim
        total_penalty += p_lim

    if result.phase_inversion:
        penalties["phase_inversion"] = 5.0
        total_penalty += 5.0

    if result.surround_swap_detected:
        penalties["channel_swap"] = 8.0
        total_penalty += 8.0

    tp_warn = settings.true_peak_dbfs
    if result.true_peak_db > tp_warn:
        t = min(1.0, (result.true_peak_db - tp_warn) / max(1e-6, (0.0 - tp_warn)))
        p = 1.0 + 4.0 * t
        penalties["true_peak"] = p
        total_penalty += p

    # L/R channel imbalance penalty (remuxer issue #7)
    if result.lr_imbalance_detected:
        # Minor: 1.5-3 dB = 2-5 pts, Major: 3+ dB = 5-10 pts
        imbalance_penalty = min(10.0, result.lr_level_imbalance_db * 2.0)
        if imbalance_penalty > 1.0:
            penalties["lr_imbalance"] = imbalance_penalty
            total_penalty += imbalance_penalty

    # Center bass loss penalty (remuxer issue #5)
    if result.center_bass_loss_db >= 6.0:
        # 6-10 dB = moderate, >10 dB = severe
        bass_penalty = min(15.0, (result.center_bass_loss_db - 5.0) * 1.5)
        penalties["center_bass_loss"] = bass_penalty
        total_penalty += bass_penalty

    final_score = max(0.0, base_score - total_penalty)
    explanation = {
        "base_score": base_score,
        "final_score": final_score,
        "penalties": penalties,
    }
    return final_score, explanation


def _build_summary(result: AudioAnalysisResult, settings: AnalysisSettings) -> str:
    parts = []
    if result.disqualified:
        parts.append("DISQUALIFIED: " + "; ".join(result.disqualify_reasons))
    if result.decode_damaged:
        if result.damaged_regions:
            region_text = ", ".join(
                f"~{s:.0f}-{e:.0f}s" for s, e in result.damaged_regions[:3]
            )
            parts.append(f"Damaged regions {region_text}")
    elif result.decode_errors:
        parts.append(f"Decoder errors: {result.decode_errors}")
    if result.lfe_dead and not result.disqualified:
        parts.append("Dead LFE channel")
    if result.sync_step_detected:
        parts.append(
            f"Sync step {result.sync_step_delta_ms:+.1f} ms at "
            f"~{(result.sync_step_time_s or 0.0):.0f}s (post-step comparison unreliable)"
        )
    elif result.alignment_warning and not result.alignment_failed:
        parts.append(result.alignment_warning)
    if result.reencode_detected:
        parts.append("RE-ENCODE DETECTED (no energy above 18 kHz)")
    elif result.shelf_detected:
        parts.append(f"Spectral shelf detected near {int(result.freq_cutoff_hz)} Hz")
    else:
        parts.append(f"Frequency response up to {int(result.freq_cutoff_hz)} Hz")
    if result.eq_warnings:
        parts.append(", ".join(result.eq_warnings))
    parts.append(f"Crest factor {result.dr_db:.1f} dB")
    if result.loudness_range_db > 0:
        parts.append(f"Loudness range {result.loudness_range_db:.1f} dB")
    if result.is_lossless is True:
        parts.append("Lossless source detected")
    elif result.is_lossless is False:
        parts.append("Lossy source detected")
    if result.clipping_detected or result.true_peak_db >= -0.1:
        parts.append("True-peak clipping risk")
    if abs(result.loudness_offset_db) >= settings.loudness_diff_warn_db:
        parts.append(f"Loudness offset {result.loudness_offset_db:+.1f} dB")
    if abs(result.dialog_balance_db) >= settings.dialog_balance_warn_db:
        if result.dialog_balance_db < 0:
            parts.append("Presence boost (dialog recessed)")
        else:
            parts.append("Dialog-heavy balance")
    if result.surround_swap_detected:
        parts.append("Surround channel swap detected")
    if result.lfe_rolloff_error:
        parts.append("LFE roll-off error")
    if result.center_nr_filtered > 0.3 or result.nr_filtered > 0.3:
        parts.append("Excessive NR/Filtered highs")
    if result.phase_inversion:
        parts.append("Phase inversion detected")
    if result.fake_multichannel:
        parts.append("Fake multichannel remix")
    if result.bitrate_bloat:
        parts.append("Possible bitrate bloat")
    if result.glitch_timestamps:
        parts.append(f"Transient spikes: {len(result.glitch_timestamps)}")
    if result.speed_shift_detected:
        parts.append("PAL speed shift")
    if result.pitch_shift_detected:
        parts.append("Pitch shift")
    if result.alignment_failed and result.alignment_warning:
        parts.append(f"Alignment warning: {result.alignment_warning}")
    if result.limiting_segments:
        parts.append(f"Limiting hot spots: {len(result.limiting_segments)}")
    if result.stereo_width > 0:
        if result.stereo_width < 0.3:
            parts.append("Narrow soundstage")
        elif result.stereo_width > 0.7:
            parts.append("Wide soundstage")
    if result.lr_imbalance_detected:
        parts.append(f"L/R imbalance ({result.lr_level_imbalance_db:.1f} dB)")
    if result.center_bass_loss_db >= 6.0:
        parts.append(f"Center bass loss ({result.center_bass_loss_db:.1f} dB)")
    return "; ".join(parts)


def _save_spectrogram(
    y_mono: np.ndarray,
    sr: int,
    out_path: str,
    settings: AnalysisSettings,
    mel_power: np.ndarray | None = None,
) -> None:
    if mel_power is None:
        mel_basis = librosa.filters.mel(
            sr=sr, n_fft=settings.fft_size, n_mels=settings.mel_bins
        )
        _, _, mel_power, _ = _stream_spectral_pass(y_mono, sr, settings, mel_basis=mel_basis)
    fig, ax = plt.subplots(figsize=(7, 3), dpi=200)
    mag_db = librosa.power_to_db(mel_power, ref=np.max)
    extent = [0, len(y_mono) / sr, 0, sr / 2]
    ax.imshow(mag_db, aspect="auto", origin="lower", extent=extent, cmap="magma")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Hz")
    ax.set_title("Mel Spectrogram")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def _true_peak_db(y: np.ndarray, oversample: int = 4) -> float:
    """Oversampled true peak, computed per channel in chunks.

    Chunking bounds the oversampled float64 temporaries to a few hundred MB;
    naively upsampling a full movie-length multichannel track would need tens
    of GB in one allocation. Chunks overlap by `guard` samples so the polyphase
    filter edges do not affect the interior samples that are kept.
    """
    if y.size == 0:
        return -120.0
    rows = y if y.ndim > 1 else y[np.newaxis, :]
    n = rows.shape[1]
    guard = 256
    peak = 0.0
    for ch in range(rows.shape[0]):
        for start, end in _iter_ranges(0, n, _CHUNK_SAMPLES):
            s = max(0, start - guard)
            e = min(n, end + guard)
            segment = np.asarray(rows[ch, s:e], dtype=np.float32)
            upsampled = scipy.signal.resample_poly(segment, oversample, 1)
            lead = (start - s) * oversample
            tail = lead + (end - start) * oversample
            peak = max(peak, float(np.max(np.abs(upsampled[lead:tail]))))
    if peak <= 0:
        return -120.0
    return 20 * math.log10(peak)


def _file_size_mb(path: str) -> float:
    try:
        return float(os.path.getsize(path)) / (1024 * 1024)
    except OSError:
        return 0.0


def analyze_files(
    file_paths: Iterable[str],
    settings: AnalysisSettings,
    output_dir: str,
    reference_path: str | None = None,
) -> list[AudioAnalysisResult]:
    results: list[AudioAnalysisResult] = []
    temp_root = output_dir or tempfile.mkdtemp(prefix="audio_analysis_")
    made_temp_root = not output_dir
    leftover_temp: list[str] = []
    mel_basis = None
    ref_freqs = None
    ref_mag_db = None
    ref_mean_db = None
    ref_bucket_db = None
    ref_bucket_times = None
    ref_total_frames = None
    ref_audio = None
    ref_mono = None
    ref_sr = settings.target_sample_rate
    ref_f0 = None
    ref_decode_errors: int | None = None
    if reference_path:
        ref_audio, ref_sr, ref_leftover, ref_decode_errors = _load_audio(
            reference_path, settings.target_sample_rate, temp_root
        )
        leftover_temp.extend(ref_leftover)
        ref_mono = _mono_mix(ref_audio)
        ref_freqs = librosa.fft_frequencies(sr=ref_sr, n_fft=settings.fft_size)
        ref_mean_mag, ref_max_power, _, ref_total_frames = _stream_spectral_pass(
            ref_mono, ref_sr, settings
        )
        ref_mag_db = librosa.amplitude_to_db(ref_mean_mag, ref=np.max)
        ref_mean_db, ref_bucket_db, ref_bucket_times = _stream_logpower_stats(
            ref_mono, ref_sr, settings, ref_max_power, _DELTA_EQ_TIME_BUCKETS
        )
        ref_f0 = _estimate_f0(ref_mono, ref_sr, settings)
    for path in file_paths:
        is_reference_file = ref_audio is not None and path == reference_path
        if is_reference_file:
            y, sr = ref_audio, ref_sr
            decode_errors = ref_decode_errors
        else:
            y, sr, cand_leftover, decode_errors = _load_audio(
                path, settings.target_sample_rate, temp_root
            )
            leftover_temp.extend(cand_leftover)
        duration_s = float(librosa.get_duration(y=y, sr=sr))
        metadata = _probe_audio_metadata(path)
        if decode_errors is None:
            decode_errors = _null_decode_error_scan(path)
        container_duration_s = _container_duration(metadata)
        duration_mismatch_s = (
            container_duration_s - duration_s if container_duration_s else 0.0
        )
        decode_damaged = (
            decode_errors >= settings.decode_error_limit
            or abs(duration_mismatch_s) > settings.decode_duration_tolerance_s
        )
        damaged_regions: list[tuple[float, float]] = []
        if decode_damaged and decode_errors > 0:
            damaged_regions = _locate_damage_regions(path, container_duration_s)
        y_mono = ref_mono if is_reference_file else _mono_mix(y)
        codec_name, codec_profile, is_lossless = _probe_audio_codec(path)
        peak, rms, dr_db, dr_blocks_used = _calculate_dynamic_range(y, sr, settings)
        loudness_db, loudness_range_db = _calculate_loudness_metrics(y_mono, sr, settings)
        true_peak_db = _true_peak_db(y)
        if mel_basis is None and output_dir:
            mel_basis = librosa.filters.mel(
                sr=sr, n_fft=settings.fft_size, n_mels=settings.mel_bins
            )
        freqs = librosa.fft_frequencies(sr=sr, n_fft=settings.fft_size)
        mean_mag, max_power, mel_power, total_frames = _stream_spectral_pass(
            y_mono, sr, settings, mel_basis=mel_basis
        )
        mag_db = librosa.amplitude_to_db(mean_mag, ref=np.max)
        cutoff_hz, shelf_detected, reencode_detected = _spectral_cutoff(freqs, mag_db, settings)
        dialog_balance_db = _dialog_balance_db(freqs, mag_db, settings)
        reference_mag = ref_mag_db if ref_mag_db is not None and ref_mag_db.shape == mag_db.shape else None
        nr_filtered = _detect_nr_filtered(mag_db, freqs, settings, reference_mag)
        glitch_timestamps = _scan_glitches(y, sr, settings)
        stereo_width = _measure_stereo_width(y)
        lr_level_imbalance, lr_noise_floor_diff, lr_imbalance = _detect_lr_imbalance(y, sr)
        chmap = _layout_channel_map(metadata.get("channel_layout"), y.shape[0])
        channel_structure = (
            _analyze_channel_structure(y, sr, settings) if y.shape[0] >= 3 else None
        )
        (
            fake_multichannel,
            fake_reasons,
            channel_corr_pairs,
            lfe_dead,
            channel_rms_db,
        ) = _evaluate_fake_multichannel(channel_structure, chmap, settings)
        center_idx = chmap.get("FC")
        lfe_idx = chmap.get("LFE")
        center_bass_loss = _detect_center_bass_loss(y, sr, center_idx, ref_audio)
        surround_swap_detected = _detect_surround_swaps(ref_audio, y, settings)
        lfe_rolloff_error = _detect_lfe_rolloff(y, sr, settings, lfe_idx)
        if center_idx is not None:
            center_channel = y[center_idx]
            center_clipping_ratio = _clipped_fraction(center_channel, settings.clip_threshold)
            center_freqs, center_mag_db = _mean_spectrum_db(center_channel, sr, settings)
        else:
            center_channel = y_mono
            center_clipping_ratio = _clipped_fraction(y_mono, settings.clip_threshold)
            center_freqs, center_mag_db = freqs, mag_db
        center_clipping_detected = center_clipping_ratio >= settings.clip_ratio_warn
        center_reference_mag = (
            ref_mag_db if ref_mag_db is not None and ref_mag_db.shape == center_mag_db.shape else None
        )
        center_nr_filtered = _detect_nr_filtered(
            center_mag_db, center_freqs, settings, center_reference_mag
        )
        clipping_ratio, clipping_detected = _detect_clipping(y, settings)
        phase_inversion = _detect_phase_inversion(y, settings)
        bitrate_kbps = _estimate_bitrate(path, duration_s)
        bitrate_bloat = _detect_bitrate_bloat(path, bitrate_kbps, cutoff_hz, sr, settings)
        size_mb = _file_size_mb(path)
        f0_candidate = _estimate_f0(y_mono, sr, settings)
        pitch_ratio, speed_shift_detected, pitch_shift_detected = _detect_pitch_speed_shift(
            ref_f0, f0_candidate, settings
        )
        alignment_offset_s = 0.0
        alignment_confidence = 0.0
        aligned_duration_s = 0.0
        alignment_method = "none"
        alignment_failed = False
        alignment_warning = None
        aligned_ref_mono = None
        aligned_cand_mono = None
        sync_step_detected = False
        sync_step_time_s: float | None = None
        sync_step_delta_ms = 0.0
        if ref_audio is not None and path != reference_path:
            ref_band = _bandpass_mono(ref_mono, sr, 300.0, 3000.0)
            cand_band = _bandpass_mono(y_mono, sr, 300.0, 3000.0)
            (
                alignment_offset_s,
                alignment_confidence,
                alignment_failed,
                alignment_warning,
                sync_step,
            ) = _scc_align_offset(
                ref_band,
                cand_band,
                sr,
                settings.scc_min_match_confidence,
                settings.sync_step_warn_ms,
            )
            del ref_band, cand_band
            if sync_step is not None:
                if sync_step["kind"] == "step":
                    sync_step_detected = True
                    sync_step_time_s = sync_step["time_s"]
                    sync_step_delta_ms = sync_step["delta_ms"]
                    step_text = (
                        f"Sync step {sync_step['delta_ms']:+.1f} ms at "
                        f"~{sync_step['time_s']:.0f}s; comparison past the step is unreliable"
                    )
                else:
                    step_text = (
                        "Alignment offsets inconsistent across the runtime; "
                        "single-offset comparison unreliable"
                    )
                alignment_warning = (
                    f"{alignment_warning}; {step_text}" if alignment_warning else step_text
                )
            if not alignment_failed:
                alignment_method = "scc"
                aligned_ref_mono, aligned_cand_mono = _apply_offset(
                    ref_mono, y_mono, alignment_offset_s, sr
                )
                aligned_duration_s = (
                    aligned_cand_mono.size / sr if aligned_cand_mono is not None else 0.0
                )
            else:
                alignment_method = "unmatched"

        level_matched_mono = None
        if aligned_ref_mono is not None and aligned_cand_mono is not None:
            gain_db = _robust_level_match_gain_db(
                aligned_ref_mono, aligned_cand_mono, sr, settings.dr_silence_db
            )
            level_matched_mono = _apply_gain_db(aligned_cand_mono, gain_db)

        freq_score = _score_metric(cutoff_hz, sr / 2)
        if shelf_detected:
            freq_score = max(0.0, freq_score - 50.0)
        # Use crest factor DR (not LRA) - matches remuxer DR meter standards
        dr_score = _score_dynamic_range(dr_db, settings)

        # Cleanliness score: focus on actual limiting/clipping, not just hot levels
        # Start with clipping penalty based on actual clipping ratio
        clipping_score = 100.0 - min(100.0, clipping_ratio * 100000)

        # True-peak penalty: only apply to lossy files (indicates encoding issues)
        # For lossless, hot levels are source characteristics, not problems
        if true_peak_db >= settings.true_peak_dbfs and is_lossless is False:
            clipping_score -= 30.0  # Reduced from 50

        # Phase inversion is a real problem regardless of codec
        if phase_inversion:
            clipping_score -= 30.0

        # Dialog balance warning - reduce penalty (may be intentional mastering)
        if abs(dialog_balance_db) >= settings.dialog_balance_warn_db:
            clipping_score -= 10.0  # Reduced from 20

        clipping_score = max(0.0, clipping_score)
        efficiency_score = 50.0
        if is_lossless is True:
            format_score = 100.0
        elif is_lossless is False:
            format_score = 60.0
        else:
            format_score = 50.0
        dialogue_score = _score_dialogue_clarity(
            center_clipping_ratio, center_nr_filtered, settings
        )
        mastering_score = 100.0
        diff_spectrum_path = None
        delta_eq_path = None
        eq_muffle_db = 0.0
        eq_boom_db = 0.0
        eq_warnings: list[str] = []
        limiting_heatmap_path = None
        limiting_waveform_paths: list[str] = []
        if reference_path and reference_mag is not None and ref_freqs is not None and path != reference_path:
            diff_source = level_matched_mono if level_matched_mono is not None else y_mono
            if diff_source is y_mono:
                diff_freqs, diff_mag_db = freqs, mag_db
            else:
                diff_freqs, diff_mag_db = _mean_spectrum_db(diff_source, sr, settings)
            if diff_mag_db.shape == reference_mag.shape:
                diff_db = diff_mag_db - reference_mag
            else:
                diff_db = mag_db - reference_mag
            band_mask = (diff_freqs >= 20.0) & (diff_freqs <= settings.shelf_high_hz)
            mean_abs_diff = float(np.mean(np.abs(diff_db[band_mask]))) if np.any(band_mask) else 0.0
            mastering_score = _score_mastering_accuracy(mean_abs_diff, settings)
            if output_dir:
                base = os.path.splitext(os.path.basename(path))[0]
                diff_spectrum_path = os.path.join(output_dir, f"{base}_diff_spectrum.png")
                _save_difference_spectrum(diff_freqs, diff_db, diff_spectrum_path)
        if reference_path and ref_mean_db is not None and ref_freqs is not None and path != reference_path:
            if ref_total_frames == total_frames:
                mean_delta_db = None
                bucket_delta_db = None
                delta_times = None
                if aligned_ref_mono is not None and aligned_cand_mono is not None:
                    matched_candidate = (
                        level_matched_mono if level_matched_mono is not None else aligned_cand_mono
                    )
                    _, cand_max_power, _, cand_frames = _stream_spectral_pass(
                        matched_candidate, sr, settings
                    )
                    _, aligned_ref_max_power, _, aligned_ref_frames = _stream_spectral_pass(
                        aligned_ref_mono, sr, settings
                    )
                    if cand_frames == aligned_ref_frames:
                        cand_mean_db, cand_bucket_db, delta_times = _stream_logpower_stats(
                            matched_candidate, sr, settings, cand_max_power, _DELTA_EQ_TIME_BUCKETS
                        )
                        aligned_ref_mean_db, aligned_ref_bucket_db, _ = _stream_logpower_stats(
                            aligned_ref_mono, sr, settings, aligned_ref_max_power, _DELTA_EQ_TIME_BUCKETS
                        )
                        mean_delta_db = cand_mean_db - aligned_ref_mean_db
                        bucket_delta_db = cand_bucket_db - aligned_ref_bucket_db
                if mean_delta_db is None:
                    cand_mean_db, cand_bucket_db, _ = _stream_logpower_stats(
                        y_mono, sr, settings, max_power, _DELTA_EQ_TIME_BUCKETS
                    )
                    mean_delta_db = cand_mean_db - ref_mean_db
                    bucket_delta_db = cand_bucket_db - ref_bucket_db
                    delta_times = ref_bucket_times
                eq_muffle_db, eq_boom_db, eq_warnings = _evaluate_eq_delta(
                    freqs, mean_delta_db, settings
                )
                if output_dir:
                    base = os.path.splitext(os.path.basename(path))[0]
                    delta_eq_path = os.path.join(output_dir, f"{base}_delta_eq.png")
                    _save_delta_eq_map(freqs, delta_times, bucket_delta_db, delta_eq_path)
        weighted_score = _weighted_score(
            freq_score,
            dr_score,
            clipping_score,
            efficiency_score,
            format_score,
            dialogue_score,
            mastering_score,
            settings,
        )

        spectrogram_path = None
        clipping_heatmap_path = None
        limiting_segments: list[tuple[float, float]] = []
        if output_dir:
            base = os.path.splitext(os.path.basename(path))[0]
            spectrogram_path = os.path.join(output_dir, f"{base}_spectrogram.png")
            _save_spectrogram(y_mono, sr, spectrogram_path, settings, mel_power=mel_power)
            block_size = int(settings.clip_heatmap_block_seconds * sr)
            if block_size > 0:
                block_count = max(1, y.shape[1] // block_size)
                clipping_series = []
                for idx in range(block_count):
                    start = idx * block_size
                    end = start + block_size
                    block = y[:, start:end]
                    if block.size == 0:
                        clipping_series.append(0.0)
                        continue
                    ratio = float(np.mean(np.abs(block) >= settings.clip_threshold))
                    clipping_series.append(ratio)
                clipping_heatmap_path = os.path.join(output_dir, f"{base}_clip_heatmap.png")
                _save_clipping_heatmap(
                    clipping_series, settings.clip_heatmap_block_seconds, clipping_heatmap_path
                )
            limit_ratios, limiting_segments = _detect_limiting_segments(y, sr, settings)
            if limit_ratios:
                window_seconds = settings.limiting_window_ms / 1000.0
                block_seconds = settings.limiting_heatmap_block_seconds
                if block_seconds > window_seconds:
                    block_len = max(1, int(block_seconds / window_seconds))
                    block_ratios = [
                        max(limit_ratios[i : i + block_len])
                        for i in range(0, len(limit_ratios), block_len)
                    ]
                    heatmap_ratios = block_ratios
                    heatmap_seconds = block_len * window_seconds
                else:
                    heatmap_ratios = limit_ratios
                    heatmap_seconds = window_seconds
                limiting_heatmap_path = os.path.join(output_dir, f"{base}_limiting_heatmap.png")
                _save_limiting_heatmap(
                    heatmap_ratios,
                    heatmap_seconds,
                    limiting_heatmap_path,
                )
                for idx, segment in enumerate(limiting_segments[: settings.limiting_waveform_segments]):
                    zoom_path = os.path.join(output_dir, f"{base}_limit_zoom_{idx + 1}.png")
                    _save_waveform_zoom(y, sr, segment, zoom_path)
                    limiting_waveform_paths.append(zoom_path)

        disqualify_reasons: list[str] = []
        if decode_damaged:
            detail_parts = []
            if decode_errors:
                detail_parts.append(f"{decode_errors} decoder errors")
            if abs(duration_mismatch_s) > settings.decode_duration_tolerance_s:
                detail_parts.append(f"{abs(duration_mismatch_s):.1f}s of audio missing")
            disqualify_reasons.append(
                "Corrupt stream (" + ", ".join(detail_parts) + ")"
                if detail_parts
                else "Corrupt stream"
            )
        if fake_multichannel:
            disqualify_reasons.append(
                "Fake multichannel (" + "; ".join(fake_reasons) + ")"
                if fake_reasons
                else "Fake multichannel"
            )
        disqualified = bool(disqualify_reasons)

        result = AudioAnalysisResult(
            path=path,
            duration_s=duration_s,
            sample_rate=sr,
            channels=y.shape[0],
            codec_name=codec_name,
            codec_profile=codec_profile,
            is_lossless=is_lossless,
            peak=peak,
            rms=rms,
            dr_db=dr_db,
            dr_blocks_used=dr_blocks_used,
            loudness_db=loudness_db,
            loudness_range_db=loudness_range_db,
            dialog_balance_db=dialog_balance_db,
            loudness_offset_db=0.0,
            true_peak_db=true_peak_db,
            freq_cutoff_hz=cutoff_hz,
            shelf_detected=shelf_detected,
            reencode_detected=reencode_detected and (bitrate_kbps or 0.0) >= 128.0,
            nr_filtered=nr_filtered,
            eq_muffle_db=eq_muffle_db,
            eq_boom_db=eq_boom_db,
            eq_warnings=eq_warnings,
            clipping_ratio=clipping_ratio,
            clipping_detected=clipping_detected,
            center_clipping_ratio=center_clipping_ratio,
            center_clipping_detected=center_clipping_detected,
            center_nr_filtered=center_nr_filtered,
            phase_inversion=phase_inversion,
            fake_multichannel=fake_multichannel,
            surround_swap_detected=surround_swap_detected,
            lfe_rolloff_error=lfe_rolloff_error,
            pitch_ratio=pitch_ratio,
            speed_shift_detected=speed_shift_detected,
            pitch_shift_detected=pitch_shift_detected,
            alignment_offset_s=alignment_offset_s,
            alignment_confidence=alignment_confidence,
            aligned_duration_s=aligned_duration_s,
            alignment_method=alignment_method,
            alignment_failed=alignment_failed,
            alignment_warning=alignment_warning,
            score_explain={},
            bitrate_kbps=bitrate_kbps,
            bitrate_bloat=bitrate_bloat,
            file_size_mb=size_mb,
            freq_score=freq_score,
            dr_score=dr_score,
            cleanliness_score=clipping_score,
            efficiency_score=efficiency_score,
            format_score=format_score,
            dialogue_score=dialogue_score,
            mastering_score=mastering_score,
            quality_grade="",
            score=weighted_score,
            summary="",
            spectrogram_path=spectrogram_path,
            diff_spectrum_path=diff_spectrum_path,
            clipping_heatmap_path=clipping_heatmap_path,
            delta_eq_path=delta_eq_path,
            limiting_heatmap_path=limiting_heatmap_path,
            limiting_waveform_paths=limiting_waveform_paths,
            glitch_timestamps=glitch_timestamps,
            limiting_segments=limiting_segments,
            reference_path=reference_path,
            stereo_width=stereo_width,
            lr_level_imbalance_db=lr_level_imbalance,
            lr_noise_floor_diff_db=lr_noise_floor_diff,
            lr_imbalance_detected=lr_imbalance,
            center_bass_loss_db=center_bass_loss,
            decode_errors=decode_errors,
            container_duration_s=container_duration_s or 0.0,
            duration_mismatch_s=duration_mismatch_s,
            decode_damaged=decode_damaged,
            damaged_regions=damaged_regions,
            channel_corr_pairs=channel_corr_pairs,
            channel_rms_db=channel_rms_db,
            lfe_dead=lfe_dead,
            fake_reasons=fake_reasons,
            sync_step_detected=sync_step_detected,
            sync_step_time_s=sync_step_time_s,
            sync_step_delta_ms=sync_step_delta_ms,
            disqualified=disqualified,
            disqualify_reasons=disqualify_reasons,
        )
        result.score, result.score_explain = _weighted_score_with_penalties(
            result.score, result, settings
        )
        result.quality_grade = "DQ" if result.disqualified else _grade_score(result.score)
        result.summary = _build_summary(result, settings)
        results.append(result)

        # Drop this file's audio (and any views into it) before loading the
        # next one so at most the reference plus one candidate are alive.
        y = None
        y_mono = None
        center_channel = None
        mel_power = None
        aligned_ref_mono = None
        aligned_cand_mono = None
        level_matched_mono = None
        diff_source = None
        matched_candidate = None
        gc.collect()

    if len(results) > 1:
        loudness_values = [r.loudness_db for r in results if r.loudness_db > -120.0]
        if loudness_values:
            reference_loudness = float(np.median(loudness_values))
            for result in results:
                result.loudness_offset_db = result.loudness_db - reference_loudness
                diff = abs(result.loudness_offset_db)
                if diff >= settings.loudness_diff_warn_db:
                    penalty = min(30.0, (diff - settings.loudness_diff_warn_db) * 5.0 + 10.0)
                    result.cleanliness_score = max(0.0, result.cleanliness_score - penalty)
                    result.score = _weighted_score(
                        result.freq_score,
                        result.dr_score,
                        result.cleanliness_score,
                        result.efficiency_score,
                        result.format_score,
                        result.dialogue_score,
                        result.mastering_score,
                        settings,
                    )
                    result.score, result.score_explain = _weighted_score_with_penalties(
                        result.score, result, settings
                    )
                    result.quality_grade = (
                        "DQ" if result.disqualified else _grade_score(result.score)
                    )

        sizes = [r.file_size_mb for r in results]
        min_size = min(sizes)
        max_size = max(sizes)
        if max_size > min_size:
            for result in results:
                efficiency_score = (max_size - result.file_size_mb) / (max_size - min_size) * 100.0
                result.efficiency_score = efficiency_score
                result.score = _weighted_score(
                    result.freq_score,
                    result.dr_score,
                    result.cleanliness_score,
                    result.efficiency_score,
                    result.format_score,
                    result.dialogue_score,
                    result.mastering_score,
                    settings,
                )
                result.score, result.score_explain = _weighted_score_with_penalties(
                    result.score, result, settings
                )
                result.quality_grade = (
                    "DQ" if result.disqualified else _grade_score(result.score)
                )
    # Disqualified tracks always sort below every clean track, regardless of score.
    results.sort(key=lambda r: (r.disqualified, -r.score))
    ref_audio = None
    ref_mono = None
    gc.collect()
    _remove_files(leftover_temp)
    if made_temp_root:
        try:
            os.rmdir(temp_root)
        except OSError:
            pass
    return results
