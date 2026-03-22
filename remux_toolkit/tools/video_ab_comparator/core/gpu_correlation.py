# remux_toolkit/tools/video_ab_comparator/core/gpu_correlation.py
"""
GPU-accelerated correlation utilities for SCC (Standard Cross-Correlation).

Provides peak extraction with sub-sample parabolic fitting and
confidence scoring on torch tensors.
"""

from __future__ import annotations

import torch


def extract_peak(
    corr: torch.Tensor,
    n_fft: int,
    sr: int,
    peak_fit: bool = False,
) -> tuple[float, int]:
    """
    Extract delay and peak index from a waveform-domain correlation.

    Searches the full lag range for the strongest peak.

    Args:
        corr: Correlation result from irfft (length n_fft).
        n_fft: FFT size used.
        sr: Sample rate in Hz.
        peak_fit: If True, apply parabolic sub-sample interpolation.

    Returns:
        (delay_ms, peak_index)
    """
    n = n_fft
    abs_corr = torch.abs(corr)
    k = torch.argmax(abs_corr).item()

    # Convert circular index to signed lag
    lag_samples = float(k if k <= n // 2 else k - n)

    # Parabolic (sub-sample) peak fitting
    if peak_fit and 0 < k < len(abs_corr) - 1:
        y1 = abs_corr[k - 1].item()
        y2 = abs_corr[k].item()
        y3 = abs_corr[k + 1].item()
        denom = y1 - 2.0 * y2 + y3
        if abs(denom) > 1e-12:
            delta = 0.5 * (y1 - y3) / denom
            if -1.0 < delta < 1.0:
                lag_samples += delta

    delay_ms = lag_samples / float(sr) * 1000.0

    return delay_ms, k


def scc_confidence(
    corr: torch.Tensor,
    peak_idx: int,
    ref_norm: torch.Tensor,
    tgt_norm: torch.Tensor,
) -> float:
    """
    SCC-specific confidence: peak / sqrt(energy_ref * energy_tgt) * 100.

    Returns match percentage (0-100).
    """
    peak_val = torch.abs(corr[peak_idx]).item()
    energy_ref = torch.sum(ref_norm ** 2).item()
    energy_tgt = torch.sum(tgt_norm ** 2).item()
    match_pct = peak_val / (((energy_ref * energy_tgt) ** 0.5) + 1e-9) * 100.0
    return min(100.0, max(0.0, match_pct))
