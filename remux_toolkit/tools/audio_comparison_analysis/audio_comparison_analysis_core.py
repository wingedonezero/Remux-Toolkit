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

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def check_dependencies() -> tuple[bool, str]:
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
    mel_bins: int
    weight_frequency: float
    weight_dynamic_range: float
    weight_cleanliness: float
    weight_efficiency: float
    weight_format: float


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
    clipping_ratio: float
    clipping_detected: bool
    phase_inversion: bool
    bitrate_kbps: float | None
    bitrate_bloat: bool
    file_size_mb: float
    freq_score: float
    dr_score: float
    cleanliness_score: float
    efficiency_score: float
    format_score: float
    quality_grade: str
    score: float
    summary: str
    spectrogram_path: str | None

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
        audio = np.stack(
            [
                librosa.resample(channel, orig_sr=sr, target_sr=target_sr)
                for channel in audio
            ],
            axis=0,
        )
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
    settings: AnalysisSettings,
) -> float:
    return (
        freq_score * settings.weight_frequency / 100.0
        + dr_score * settings.weight_dynamic_range / 100.0
        + cleanliness_score * settings.weight_cleanliness / 100.0
        + efficiency_score * settings.weight_efficiency / 100.0
        + format_score * settings.weight_format / 100.0
    )


def _build_summary(result: AudioAnalysisResult, settings: AnalysisSettings) -> str:
    parts = []
    if result.reencode_detected:
        parts.append("RE-ENCODE DETECTED (no energy above 18 kHz)")
    elif result.shelf_detected:
        parts.append(f"Spectral shelf detected near {int(result.freq_cutoff_hz)} Hz")
    else:
        parts.append(f"Frequency response up to {int(result.freq_cutoff_hz)} Hz")
    parts.append(f"True DR {result.dr_db:.1f} dB")
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
    if result.phase_inversion:
        parts.append("Phase inversion detected")
    if result.bitrate_bloat:
        parts.append("Possible bitrate bloat")
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
) -> list[AudioAnalysisResult]:
    results: list[AudioAnalysisResult] = []
    for path in file_paths:
        y, sr = _load_audio(path, settings.target_sample_rate)
        duration_s = float(librosa.get_duration(y=y, sr=sr))
        y_mono = np.mean(y, axis=0) if y.shape[0] > 1 else y[0]
        codec_name, codec_profile, is_lossless = _probe_audio_codec(path)
        peak, rms, dr_db, dr_blocks_used = _calculate_dynamic_range(y, sr, settings)
        loudness_db, loudness_range_db = _calculate_loudness_metrics(y, sr, settings)
        true_peak_db = _true_peak_db(y)
        cutoff_hz, shelf_detected, reencode_detected = _spectral_cutoff(y_mono, sr, settings)
        dialog_balance_db = _dialog_balance_db(y_mono, sr, settings)
        clipping_ratio, clipping_detected = _detect_clipping(y, settings)
        phase_inversion = _detect_phase_inversion(y, settings)
        bitrate_kbps = _estimate_bitrate(path, duration_s)
        bitrate_bloat = _detect_bitrate_bloat(path, bitrate_kbps, cutoff_hz, sr, settings)
        size_mb = _file_size_mb(path)

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
        weighted_score = _weighted_score(
            freq_score, dr_score, clipping_score, efficiency_score, format_score, settings
        )

        spectrogram_path = None
        if output_dir:
            base = os.path.splitext(os.path.basename(path))[0]
            spectrogram_path = os.path.join(output_dir, f"{base}_spectrogram.png")
            _save_spectrogram(y_mono, sr, spectrogram_path, settings)

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
            clipping_ratio=clipping_ratio,
            clipping_detected=clipping_detected,
            phase_inversion=phase_inversion,
            bitrate_kbps=bitrate_kbps,
            bitrate_bloat=bitrate_bloat,
            file_size_mb=size_mb,
            freq_score=freq_score,
            dr_score=dr_score,
            cleanliness_score=clipping_score,
            efficiency_score=efficiency_score,
            format_score=format_score,
            quality_grade="",
            score=weighted_score,
            summary="",
            spectrogram_path=spectrogram_path,
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
                    settings,
                )
                result.quality_grade = _grade_score(result.score)
    results.sort(key=lambda r: r.score, reverse=True)
    return results
