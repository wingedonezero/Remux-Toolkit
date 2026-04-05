"""Layer 2: Naranjo chi-square + dual autocorrelation + duplicate field detection."""

from __future__ import annotations

import time
from typing import Callable, Optional

from .models import (
    PixelResult, StreamInfo,
    COMBING_RATIO_THRESHOLD, CHI_SQUARE_ALPHA, MAD_SCALE,
    DUP_FIELD_SAD_THRESHOLD, DUP_FIELD_PERIOD5_RATIO,
)


def _compute_field_difference(arr, np) -> float:
    """Field difference x[n] = mean|even_rows - odd_rows|."""
    h, w = arr.shape
    if h < 6:
        return 0.0
    f = arr.astype(np.float32)
    even_rows = f[0::2]
    odd_rows = f[1::2]
    min_h = min(len(even_rows), len(odd_rows))
    return float(np.mean(np.abs(even_rows[:min_h] - odd_rows[:min_h])))


def _compute_combing_ratio(arr, np) -> tuple[float, float, float]:
    """
    Inter-field/intra-field SAD ratio — self-normalizing combing metric.

    Returns (ratio, inter_sad, intra_sad).
    """
    h, w = arr.shape
    if h < 6:
        return 0.0, 0.0, 0.0

    f = arr.astype(np.float32)

    # Inter-field: adjacent rows (different fields)
    even_rows = f[0::2]
    odd_rows = f[1::2]
    min_h = min(len(even_rows), len(odd_rows))
    inter_sad = float(np.mean(np.abs(even_rows[:min_h] - odd_rows[:min_h])))

    # Intra-field: same-parity rows (within each field)
    intra_top = (
        float(np.mean(np.abs(even_rows[:-1] - even_rows[1:])))
        if len(even_rows) > 1 else 0.0
    )
    intra_bot = (
        float(np.mean(np.abs(odd_rows[:-1] - odd_rows[1:])))
        if len(odd_rows) > 1 else 0.0
    )
    intra_sad = (intra_top + intra_bot) / 2.0

    if intra_sad < 0.1:
        return (0.0 if inter_sad < 0.1 else 10.0), inter_sad, intra_sad

    return inter_sad / intra_sad, inter_sad, intra_sad


def _chi_square_detection(x, np, alpha=CHI_SQUARE_ALPHA) -> dict:
    """
    Naranjo chi-square energy test for telecine detection.

    Under H0 (not telecined): normalized field differences are ~Gaussian.
    Under H1 (telecined): periodic pulses add energy above noise floor.
    """
    from scipy import stats as sp_stats

    N = len(x)
    if N < 20:
        return {"detected": False, "energy_ratio": 0, "std_ratio": 1.0,
                "reason": "too few frames"}

    # Robust noise estimation using MAD
    x_median = float(np.median(x))
    mad = float(np.median(np.abs(x - x_median)))
    sigma_robust = mad * MAD_SCALE

    x_std = float(np.std(x, ddof=1))

    if sigma_robust < 0.01:
        if x_std < 0.01:
            return {"detected": False, "energy_ratio": 0, "std_ratio": 1.0,
                    "reason": "no variance in field differences"}
        sigma_robust = x_std  # fallback

    x_norm = (x - x_median) / sigma_robust
    energy = float(np.sum(x_norm ** 2))
    threshold = float(sp_stats.chi2.ppf(1 - alpha, df=N))

    detected = energy > threshold
    energy_ratio = energy / threshold
    std_ratio = x_std / sigma_robust if sigma_robust > 0.01 else 1.0

    return {
        "detected": detected,
        "energy": round(energy, 1),
        "threshold": round(threshold, 1),
        "energy_ratio": round(energy_ratio, 4),
        "std_ratio": round(std_ratio, 4),
        "sigma_robust": round(sigma_robust, 4),
        "reason": (f"energy {'>' if detected else '<='} threshold "
                   f"({energy:.1f} vs {threshold:.1f}, ratio={energy_ratio:.3f})"),
    }


def _autocorr(signal, np):
    """Compute normalized autocorrelation at lags 1-10."""
    mean_s = float(np.mean(signal))
    centered = signal - mean_s
    var_s = float(np.var(signal))
    ac = {}
    if var_s > 0.001:
        for lag in range(1, 11):
            if lag < len(signal):
                ac[lag] = float(np.mean(centered[:-lag] * centered[lag:]) / var_s)
    return ac, var_s > 0.001


def run_layer2(
    filepath: str,
    stream_info: StreamInfo,
    include_per_frame: bool = True,
    on_progress: Optional[Callable[[str], None]] = None,
    check_cancelled: Optional[Callable[[], bool]] = None,
) -> PixelResult:
    """
    Naranjo chi-square energy test + dual autocorrelation + dup field detection.

    Decodes the full video via VapourSynth. Computes three signals per frame:
    1. Field difference x[n] for chi-square energy test
    2. Combing ratio for autocorrelation (key metric: lag5/lag1)
    3. Duplicate field SAD for telecine field-copy detection
    """
    import numpy as np
    import vapoursynth as vs
    core = vs.core

    px = PixelResult()
    t0 = time.time()

    if on_progress:
        on_progress("Loading video for pixel analysis...")

    try:
        clip = core.ffms2.Source(filepath, threads=0)
    except Exception as e:
        px.error = str(e)
        px.elapsed_sec = time.time() - t0
        return px

    total = len(clip)
    fps = clip.fps.numerator / clip.fps.denominator if clip.fps.denominator else 29.97
    px.total_frames = total

    if on_progress:
        on_progress(f"Analyzing {total:,} frames @ {fps:.3f} fps...")

    # Extract luma plane
    if clip.format.color_family == vs.YUV:
        clip_y = core.std.ShufflePlanes(clip, planes=0, colorfamily=vs.GRAY)
    elif clip.format.color_family == vs.RGB:
        clip_y = core.std.ShufflePlanes(
            core.resize.Bicubic(clip, format=vs.YUV420P8),
            planes=0, colorfamily=vs.GRAY,
        )
    else:
        clip_y = clip

    # Per-frame analysis
    per_frame = []
    field_diffs = []
    ratios = []
    combed_indices = []
    prev_top_field = None
    prev_bot_field = None
    top_field_sads = []
    bot_field_sads = []
    last_report = t0

    for i, frame in enumerate(clip_y.frames()):
        # Check cancellation
        if check_cancelled and check_cancelled():
            px.error = "cancelled"
            px.elapsed_sec = time.time() - t0
            return px

        arr = np.asarray(frame[0])

        # Signal 1: field difference x[n]
        xn = _compute_field_difference(arr, np)

        # Signal 2: combing ratio
        ratio, inter_sad, intra_sad = _compute_combing_ratio(arr, np)
        combed = ratio > COMBING_RATIO_THRESHOLD

        # Signal 3: duplicate field detection
        f32 = arr.astype(np.float32)
        top_f = f32[0::2]
        bot_f = f32[1::2]
        top_fsad = -1.0
        bot_fsad = -1.0
        if prev_top_field is not None:
            min_ht = min(len(top_f), len(prev_top_field))
            min_hb = min(len(bot_f), len(prev_bot_field))
            top_fsad = float(np.mean(np.abs(
                top_f[:min_ht] - prev_top_field[:min_ht])))
            bot_fsad = float(np.mean(np.abs(
                bot_f[:min_hb] - prev_bot_field[:min_hb])))
            top_field_sads.append(top_fsad)
            bot_field_sads.append(bot_fsad)
        prev_top_field = top_f.copy()
        prev_bot_field = bot_f.copy()

        if include_per_frame:
            per_frame.append({
                "idx": i,
                "xn": round(xn, 4),
                "ratio": round(ratio, 4),
                "inter_sad": round(inter_sad, 2),
                "intra_sad": round(intra_sad, 2),
                "combed": combed,
                "top_fsad": round(top_fsad, 4) if top_fsad >= 0 else None,
                "bot_fsad": round(bot_fsad, 4) if bot_fsad >= 0 else None,
            })
        else:
            # Still need ratio/combed for Layer 3 handoff
            per_frame.append({
                "idx": i,
                "ratio": round(ratio, 4),
                "intra_sad": round(intra_sad, 2),
                "combed": combed,
            })

        field_diffs.append(xn)
        ratios.append(ratio)
        if combed:
            combed_indices.append(i)

        # Progress reporting
        now = time.time()
        if on_progress and now - last_report > 5.0:
            elapsed = now - t0
            pct = (i + 1) / total * 100
            rate_fps = (i + 1) / elapsed
            eta = (total - i - 1) / rate_fps if rate_fps > 0 else 0
            on_progress(
                f"Layer 2: {pct:.0f}% ({i + 1:,}/{total:,}) "
                f"@ {rate_fps:.0f} f/s, ETA {eta:.0f}s"
            )
            last_report = now

    elapsed = time.time() - t0
    rate_fps = total / elapsed if elapsed > 0 else 0

    xn_arr = np.array(field_diffs)
    ratios_arr = np.array(ratios)

    # ── Chi-square energy test ─────────────────────────────────────────
    if on_progress:
        on_progress("Computing chi-square energy test...")

    chi_result = _chi_square_detection(xn_arr, np)
    px.chi_square_detected = chi_result.get("detected", False)
    px.energy_ratio = round(chi_result.get("energy_ratio", 0), 4)
    px.std_ratio = round(chi_result.get("std_ratio", 1.0), 4)
    px.chi_square_detail = chi_result

    # ── Combing statistics ─────────────────────────────────────────────
    px.combed_frames = len(combed_indices)
    px.combed_pct = round(px.combed_frames / total * 100, 2) if total > 0 else 0
    px.median_ratio = round(float(np.median(ratios_arr)), 4)
    px.combed_indices = combed_indices

    # ── Field difference stats ─────────────────────────────────────────
    px.xn_mean = round(float(np.mean(xn_arr)), 4)
    px.xn_std = round(float(np.std(xn_arr)), 4)
    px.xn_median = round(float(np.median(xn_arr)), 4)

    # ── Autocorrelation of BOTH signals ────────────────────────────────
    xn_autocorr, xn_has_var = _autocorr(xn_arr, np)
    comb_autocorr, comb_has_var = _autocorr(ratios_arr, np)

    px.xn_autocorrelation = {str(k): round(v, 4) for k, v in xn_autocorr.items()}
    px.xn_lag1 = round(xn_autocorr.get(1, 0.0), 4)
    px.xn_lag5 = round(xn_autocorr.get(5, 0.0), 4)
    px.xn_lag5_lag1_ratio = round(
        px.xn_lag5 / px.xn_lag1 if abs(px.xn_lag1) > 0.001 else 0.0, 4
    )

    px.comb_autocorrelation = {str(k): round(v, 4) for k, v in comb_autocorr.items()}
    px.comb_lag1 = round(comb_autocorr.get(1, 0.0), 4)
    px.comb_lag5 = round(comb_autocorr.get(5, 0.0), 4)
    px.comb_lag5_lag1_ratio = round(
        px.comb_lag5 / px.comb_lag1 if abs(px.comb_lag1) > 0.001 else 0.0, 4
    )
    px.has_variance = comb_has_var or xn_has_var

    # ── Duplicate field detection ──────────────────────────────────────
    top_sads_arr = np.array(top_field_sads)
    bot_sads_arr = np.array(bot_field_sads)
    n_pairs = len(top_sads_arr)

    if n_pairs > 10:
        either_dup = (
            (top_sads_arr < DUP_FIELD_SAD_THRESHOLD)
            | (bot_sads_arr < DUP_FIELD_SAD_THRESHOLD)
        )
        px.dup_field_pct = round(float(np.sum(either_dup) / n_pairs * 100), 2)
        px.dup_field_pct_02 = round(float(np.sum(
            (top_sads_arr < 0.2) | (bot_sads_arr < 0.2)) / n_pairs * 100), 2)
        px.dup_field_pct_10 = round(float(np.sum(
            (top_sads_arr < 1.0) | (bot_sads_arr < 1.0)) / n_pairs * 100), 2)

        # Autocorrelation of the duplicate mask
        dup_sig = either_dup.astype(np.float64)
        dup_sig_centered = dup_sig - dup_sig.mean()
        dup_sig_var = float(np.var(dup_sig))

        dup_autocorr = {}
        if dup_sig_var > 1e-6:
            for lag in range(1, 11):
                if lag < len(dup_sig):
                    dup_autocorr[lag] = float(
                        np.mean(dup_sig_centered[:-lag] * dup_sig_centered[lag:])
                        / dup_sig_var
                    )

        px.dup_field_autocorrelation = {
            str(k): round(v, 4) for k, v in dup_autocorr.items()
        }
        px.dup_field_lag1 = round(dup_autocorr.get(1, 0.0), 4)
        px.dup_field_lag5 = round(dup_autocorr.get(5, 0.0), 4)
        dup_l5l1 = (
            px.dup_field_lag5 / px.dup_field_lag1
            if abs(px.dup_field_lag1) > 0.001 else 0.0
        )
        px.dup_field_lag5_lag1_ratio = round(dup_l5l1, 4)
        px.dup_field_has_period5 = (
            dup_l5l1 > DUP_FIELD_PERIOD5_RATIO and px.dup_field_lag5 > 0.1
        )

        # Field SAD statistics
        px.dup_field_top_median_sad = round(float(np.median(top_sads_arr)), 4)
        px.dup_field_bot_median_sad = round(float(np.median(bot_sads_arr)), 4)
        px.dup_field_top_p5_sad = round(float(np.percentile(top_sads_arr, 5)), 4)
        px.dup_field_bot_p5_sad = round(float(np.percentile(bot_sads_arr, 5)), 4)

    # ── Finalize ───────────────────────────────────────────────────────
    px.per_frame = per_frame
    px.elapsed_sec = round(elapsed, 2)
    px.frames_per_sec = round(rate_fps, 1)

    if on_progress:
        on_progress(
            f"Layer 2 complete: {total:,} frames in {elapsed:.1f}s "
            f"@ {rate_fps:.0f} f/s"
        )

    return px
