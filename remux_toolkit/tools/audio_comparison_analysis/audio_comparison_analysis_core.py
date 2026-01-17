# remux_toolkit/tools/audio_comparison_analysis/audio_comparison_analysis_core.py

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import os
import subprocess
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
    fake_multichannel_corr_threshold: float
    fake_multichannel_energy_variance_db: float
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
    nr_filtered: bool
    eq_muffle_db: float
    eq_boom_db: float
    eq_warnings: list[str]
    clipping_ratio: float
    clipping_detected: bool
    center_clipping_ratio: float
    center_clipping_detected: bool
    center_nr_filtered: bool
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

    def to_dict(self) -> dict:
        data = asdict(self)
        data["path"] = self.path
        return data


def _load_audio(path: str, target_sr: int) -> tuple[np.ndarray, int]:
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

    return audio, sr


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
    y: np.ndarray, sr: int, settings: AnalysisSettings
) -> tuple[float, float]:
    if y.size == 0:
        return -120.0, 0.0
    y_mono = np.mean(y, axis=0) if y.shape[0] > 1 else y[0]
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
    return scipy.signal.sosfilt(sos, y)


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

def _log_power_spectrogram(
    y_mono: np.ndarray, sr: int, settings: AnalysisSettings
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Log-power spectrogram for delta EQ mapping."""
    stft = librosa.stft(y_mono, n_fft=settings.fft_size, hop_length=settings.hop_length)
    power = np.abs(stft) ** 2
    power_db = librosa.power_to_db(power, ref=np.max)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=settings.fft_size)
    times = librosa.frames_to_time(
        np.arange(power_db.shape[1]), sr=sr, hop_length=settings.hop_length
    )
    return freqs, times, power_db


def _mean_spectrum_db(
    y_mono: np.ndarray, sr: int, settings: AnalysisSettings
) -> tuple[np.ndarray, np.ndarray]:
    stft = librosa.stft(y_mono, n_fft=settings.fft_size, hop_length=settings.hop_length)
    mag = np.abs(stft)
    mean_mag = np.mean(mag, axis=1)
    mag_db = librosa.amplitude_to_db(mean_mag, ref=np.max)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=settings.fft_size)
    return freqs, mag_db


def _spectral_cutoff(
    y_mono: np.ndarray, sr: int, settings: AnalysisSettings
) -> tuple[float, bool, bool]:
    stft = librosa.stft(y_mono, n_fft=settings.fft_size, hop_length=settings.hop_length)
    mag = np.abs(stft)
    mean_mag = np.mean(mag, axis=1)
    mag_db = librosa.amplitude_to_db(mean_mag, ref=np.max)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=settings.fft_size)
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
    y_mono: np.ndarray, sr: int, settings: AnalysisSettings
) -> float:
    stft = librosa.stft(y_mono, n_fft=settings.fft_size, hop_length=settings.hop_length)
    mag = np.abs(stft)
    mag_db = librosa.amplitude_to_db(np.mean(mag, axis=1), ref=np.max)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=settings.fft_size)
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
) -> bool:
    high_band = freqs >= settings.nr_cutoff_hz
    mid_band = (freqs >= settings.dialog_band_low_hz) & (freqs <= settings.dialog_band_high_hz)
    high_mean = float(np.mean(mag_db[high_band])) if np.any(high_band) else -120.0
    mid_mean = float(np.mean(mag_db[mid_band])) if np.any(mid_band) else -120.0
    ratio_db = mid_mean - high_mean
    if reference_mag_db is not None:
        ref_high = float(np.mean(reference_mag_db[high_band])) if np.any(high_band) else -120.0
        if ref_high - high_mean >= settings.nr_drop_db:
            return True
    return ratio_db >= settings.nr_ratio_db


def _evaluate_eq_delta(
    freqs: np.ndarray,
    delta_db: np.ndarray,
    settings: AnalysisSettings,
) -> tuple[float, float, list[str]]:
    warnings: list[str] = []
    mean_delta = np.mean(delta_db, axis=1)
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
    ref_channels = ref_audio[:5, :]
    cand_channels = cand_audio[:5, :]
    corr = np.nan_to_num(np.corrcoef(np.vstack([ref_channels, cand_channels])))
    ref_count = ref_channels.shape[0]
    corr_block = corr[:ref_count, ref_count:]
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
    flat = np.max(np.abs(y), axis=0) if y.ndim > 1 else np.abs(y)
    total_windows = max(1, flat.size // window_samples)
    ratios = []
    segments: list[tuple[float, float]] = []
    for idx in range(total_windows):
        start = idx * window_samples
        end = start + window_samples
        window = flat[start:end]
        if window.size == 0:
            ratios.append(0.0)
            continue
        ratio = float(np.mean(window >= settings.clip_threshold))
        ratios.append(ratio)
        if ratio >= settings.limiting_ratio:
            segments.append((start / sr, end / sr))
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
    diffs = []
    for channel in y:
        channel_diff = np.abs(np.diff(channel))
        if channel_diff.size:
            diffs.append(channel_diff)
    if not diffs:
        return []
    diff = np.max(np.vstack(diffs), axis=0)
    if diff.size == 0:
        return []
    mean = float(np.mean(diff))
    std = float(np.std(diff))
    threshold = max(settings.glitch_diff_threshold, mean + 6.0 * std)
    spikes = np.where(diff >= threshold)[0]
    if spikes.size == 0:
        return []
    timestamps = (spikes / sr).tolist()
    return [float(ts) for ts in timestamps[: settings.glitch_max_count]]


def _detect_fake_multichannel(y: np.ndarray, settings: AnalysisSettings) -> bool:
    if y.shape[0] < 6:
        return False
    channels = y[: y.shape[0], :]
    if channels.shape[1] == 0:
        return False
    corr = np.nan_to_num(np.corrcoef(channels))
    if corr.shape[0] < 2:
        return False
    upper = corr[np.triu_indices_from(corr, k=1)]
    median_corr = float(np.median(np.abs(upper))) if upper.size else 0.0
    rms = np.sqrt(np.mean(channels**2, axis=1))
    rms_db = 20 * np.log10(np.maximum(rms, 1e-12))
    energy_spread = float(np.max(rms_db) - np.min(rms_db))
    return (
        median_corr >= settings.fake_multichannel_corr_threshold
        and energy_spread <= settings.fake_multichannel_energy_variance_db
    )


def _score_mastering_accuracy(mean_abs_diff_db: float, settings: AnalysisSettings) -> float:
    return max(0.0, 100.0 - mean_abs_diff_db * settings.mastering_diff_penalty_db)


def _score_dialogue_clarity(
    center_clipping_ratio: float,
    center_nr_filtered: bool,
    settings: AnalysisSettings,
) -> float:
    score = 100.0
    if center_clipping_ratio > 0:
        score -= min(60.0, center_clipping_ratio * 100000)
    if center_nr_filtered:
        score -= settings.dialogue_clarity_penalty
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
    flat = np.abs(y.reshape(-1))
    if flat.size == 0:
        return 0.0, False
    clipping_ratio = float(np.mean(flat >= settings.clip_threshold))
    return clipping_ratio, clipping_ratio >= settings.clip_ratio_warn


def _detect_phase_inversion(y: np.ndarray, settings: AnalysisSettings) -> bool:
    if y.shape[0] < 2:
        return False
    left = y[0]
    right = y[1]
    if left.size == 0 or right.size == 0:
        return False
    corr = np.corrcoef(left, right)[0, 1]
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
        "stream=channels,channel_layout,sample_rate,bit_rate,codec_name,profile",
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
        "encoder": tags.get("ENCODER") if isinstance(tags, dict) else None,
    }


def _channel_index(channel_layout: str | None, target: str, channels: int) -> int | None:
    if not channel_layout:
        if target == "FC" and channels >= 3:
            return 2
        if target == "LFE" and channels >= 4:
            return 3
        return None
    layout = channel_layout.lower()
    layout_map = {
        "mono": ["FC"],
        "stereo": ["FL", "FR"],
        "2.1": ["FL", "FR", "LFE"],
        "3.0": ["FL", "FR", "FC"],
        "3.1": ["FL", "FR", "FC", "LFE"],
        "4.0": ["FL", "FR", "FC", "BC"],
        "4.1": ["FL", "FR", "FC", "LFE", "BC"],
        "5.0": ["FL", "FR", "FC", "BL", "BR"],
        "5.1": ["FL", "FR", "FC", "LFE", "BL", "BR"],
        "5.1(side)": ["FL", "FR", "FC", "LFE", "SL", "SR"],
        "6.1": ["FL", "FR", "FC", "LFE", "BL", "BR", "BC"],
        "7.1": ["FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"],
    }
    order = layout_map.get(layout)
    if not order:
        return None
    try:
        return order.index(target)
    except ValueError:
        return None

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
    if dr_db <= 0:
        return 0.0
    if dr_db < settings.brickwall_dr_db:
        return 0.0
    if dr_db < settings.target_dr_min:
        span = settings.target_dr_min - settings.brickwall_dr_db
        return 50.0 + (dr_db - settings.brickwall_dr_db) / span * 40.0
    if dr_db <= settings.target_dr_max:
        return 100.0
    return max(70.0, 100.0 - (dr_db - settings.target_dr_max) * 2.0)


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


def _build_summary(result: AudioAnalysisResult, settings: AnalysisSettings) -> str:
    parts = []
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
    if result.center_nr_filtered or result.nr_filtered:
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
    if result.limiting_segments:
        parts.append(f"Limiting hot spots: {len(result.limiting_segments)}")
    return "; ".join(parts)


def _save_spectrogram(y_mono: np.ndarray, sr: int, out_path: str, settings: AnalysisSettings) -> None:
    fig, ax = plt.subplots(figsize=(7, 3), dpi=200)
    mel = librosa.feature.melspectrogram(
        y=y_mono,
        sr=sr,
        n_fft=settings.fft_size,
        hop_length=settings.hop_length,
        n_mels=settings.mel_bins,
        power=2.0,
    )
    mag_db = librosa.power_to_db(mel, ref=np.max)
    extent = [0, len(y_mono) / sr, 0, sr / 2]
    ax.imshow(mag_db, aspect="auto", origin="lower", extent=extent, cmap="magma")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Hz")
    ax.set_title("Mel Spectrogram")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def _true_peak_db(y: np.ndarray, oversample: int = 4) -> float:
    if y.size == 0:
        return -120.0
    flat = y.reshape(-1)
    upsampled = scipy.signal.resample_poly(flat, oversample, 1)
    peak = float(np.max(np.abs(upsampled)))
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
    ref_freqs = None
    ref_mag_db = None
    ref_power_db = None
    ref_times = None
    ref_audio = None
    ref_f0 = None
    if reference_path:
        ref_audio, ref_sr = _load_audio(reference_path, settings.target_sample_rate)
        ref_mono = np.mean(ref_audio, axis=0) if ref_audio.shape[0] > 1 else ref_audio[0]
        ref_freqs, ref_mag_db = _mean_spectrum_db(ref_mono, ref_sr, settings)
        ref_freqs, ref_times, ref_power_db = _log_power_spectrogram(ref_mono, ref_sr, settings)
        ref_f0 = _estimate_f0(ref_mono, ref_sr, settings)
    for path in file_paths:
        y, sr = _load_audio(path, settings.target_sample_rate)
        duration_s = float(librosa.get_duration(y=y, sr=sr))
        metadata = _probe_audio_metadata(path)
        y_mono = np.mean(y, axis=0) if y.shape[0] > 1 else y[0]
        codec_name, codec_profile, is_lossless = _probe_audio_codec(path)
        peak, rms, dr_db, dr_blocks_used = _calculate_dynamic_range(y, sr, settings)
        loudness_db, loudness_range_db = _calculate_loudness_metrics(y, sr, settings)
        true_peak_db = _true_peak_db(y)
        cutoff_hz, shelf_detected, reencode_detected = _spectral_cutoff(y_mono, sr, settings)
        freqs, mag_db = _mean_spectrum_db(y_mono, sr, settings)
        spec_freqs, spec_times, power_db = _log_power_spectrogram(y_mono, sr, settings)
        dialog_balance_db = _dialog_balance_db(y_mono, sr, settings)
        reference_mag = ref_mag_db if ref_mag_db is not None and ref_mag_db.shape == mag_db.shape else None
        nr_filtered = _detect_nr_filtered(mag_db, freqs, settings, reference_mag)
        glitch_timestamps = _scan_glitches(y, sr, settings)
        fake_multichannel = _detect_fake_multichannel(y, settings)
        center_idx = _channel_index(metadata.get("channel_layout"), "FC", y.shape[0])
        lfe_idx = _channel_index(metadata.get("channel_layout"), "LFE", y.shape[0])
        surround_swap_detected = _detect_surround_swaps(ref_audio, y, settings)
        lfe_rolloff_error = _detect_lfe_rolloff(y, sr, settings, lfe_idx)
        center_channel = y[center_idx] if center_idx is not None else y_mono
        center_clipping_ratio = float(np.mean(np.abs(center_channel) >= settings.clip_threshold))
        center_clipping_detected = center_clipping_ratio >= settings.clip_ratio_warn
        center_freqs, center_mag_db = _mean_spectrum_db(center_channel, sr, settings)
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
        aligned_ref_mono = None
        aligned_cand_mono = None
        if ref_audio is not None and path != reference_path:
            ref_band = _bandpass_mono(ref_mono, sr, 300.0, 3000.0)
            cand_band = _bandpass_mono(y_mono, sr, 300.0, 3000.0)
            alignment_offset_s, alignment_confidence = _align_offset(ref_band, cand_band, sr)
            aligned_ref_mono, aligned_cand_mono = _apply_offset(
                ref_mono, y_mono, alignment_offset_s, sr
            )
            aligned_duration_s = aligned_cand_mono.size / sr if aligned_cand_mono is not None else 0.0

        freq_score = _score_metric(cutoff_hz, sr / 2)
        if shelf_detected:
            freq_score = max(0.0, freq_score - 50.0)
        dr_score = _score_loudness_range(loudness_range_db, settings)
        clipping_score = 100.0 - min(100.0, clipping_ratio * 100000)
        if true_peak_db >= settings.true_peak_dbfs:
            clipping_score -= 50.0
        if phase_inversion:
            clipping_score -= 30.0
        if abs(dialog_balance_db) >= settings.dialog_balance_warn_db:
            clipping_score -= 20.0
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
            diff_db = mag_db - reference_mag
            band_mask = (freqs >= 20.0) & (freqs <= settings.shelf_high_hz)
            mean_abs_diff = float(np.mean(np.abs(diff_db[band_mask]))) if np.any(band_mask) else 0.0
            mastering_score = _score_mastering_accuracy(mean_abs_diff, settings)
            if output_dir:
                base = os.path.splitext(os.path.basename(path))[0]
                diff_spectrum_path = os.path.join(output_dir, f"{base}_diff_spectrum.png")
                _save_difference_spectrum(freqs, diff_db, diff_spectrum_path)
        if reference_path and ref_power_db is not None and ref_freqs is not None and path != reference_path:
            if ref_power_db.shape == power_db.shape:
                if aligned_ref_mono is not None and aligned_cand_mono is not None:
                    aligned_freqs, aligned_times, aligned_power = _log_power_spectrogram(
                        aligned_cand_mono, sr, settings
                    )
                    _, _, aligned_ref_power = _log_power_spectrogram(
                        aligned_ref_mono, sr, settings
                    )
                    if aligned_power.shape == aligned_ref_power.shape:
                        delta_db = aligned_power - aligned_ref_power
                        delta_freqs = aligned_freqs
                        delta_times = aligned_times
                    else:
                        delta_db = power_db - ref_power_db
                        delta_freqs = spec_freqs
                        delta_times = spec_times
                else:
                    delta_db = power_db - ref_power_db
                    delta_freqs = spec_freqs
                    delta_times = spec_times
                eq_muffle_db, eq_boom_db, eq_warnings = _evaluate_eq_delta(
                    delta_freqs, delta_db, settings
                )
                if output_dir:
                    base = os.path.splitext(os.path.basename(path))[0]
                    delta_eq_path = os.path.join(output_dir, f"{base}_delta_eq.png")
                    _save_delta_eq_map(delta_freqs, delta_times, delta_db, delta_eq_path)
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
            _save_spectrogram(y_mono, sr, spectrogram_path, settings)
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
        )
        result.quality_grade = _grade_score(result.score)
        result.summary = _build_summary(result, settings)
        results.append(result)

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
                    result.quality_grade = _grade_score(result.score)

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
                result.quality_grade = _grade_score(result.score)
    results.sort(key=lambda r: r.score, reverse=True)
    return results
