"""Layer 3: Field-Swap Validation — physical proof of telecine vs interlaced."""

from __future__ import annotations

import time
from typing import Callable, Optional

from .models import (
    FieldSwapResult, PixelResult, StreamInfo,
    FIXED_THRESHOLD, MIN_COMBED_FOR_FIELDSWAP,
)
from .pixel_analysis import _compute_combing_ratio


def _swap_top_field(base, donor):
    """Replace even rows (top field) of base with donor's even rows."""
    result = base.copy()
    result[0::2] = donor[0::2]
    return result


def _swap_bottom_field(base, donor):
    """Replace odd rows (bottom field) of base with donor's odd rows."""
    result = base.copy()
    result[1::2] = donor[1::2]
    return result


def run_layer3(
    filepath: str,
    stream_info: StreamInfo,
    pixel_result: PixelResult,
    on_progress: Optional[Callable[[str], None]] = None,
    check_cancelled: Optional[Callable[[], bool]] = None,
) -> FieldSwapResult:
    """
    Field-swap validation — physical proof of telecine vs interlaced.

    For each combed frame from Layer 2, swap its fields with neighbors.
    If combing drops → fixable → telecine (fields from different film frames).
    If combing persists → unfixable → genuinely interlaced.
    """
    import numpy as np
    import vapoursynth as vs
    core = vs.core

    fs = FieldSwapResult()
    t0 = time.time()

    if on_progress:
        on_progress("Field-swap validation...")

    combed_indices = pixel_result.combed_indices
    per_frame = pixel_result.per_frame

    # Filter out degenerate frames (black/static: intra_sad < 0.1)
    eligible = []
    degenerate_count = 0
    for idx in combed_indices:
        if idx < len(per_frame):
            intra = per_frame[idx].get("intra_sad", 1.0)
            if intra < 0.1:
                degenerate_count += 1
            else:
                eligible.append(idx)

    fs.total_combed = len(combed_indices)
    fs.degenerate_skipped = degenerate_count
    fs.eligible_combed = len(eligible)

    if len(eligible) < MIN_COMBED_FOR_FIELDSWAP:
        if on_progress:
            on_progress(
                f"Insufficient combed frames ({len(eligible)} < "
                f"{MIN_COMBED_FOR_FIELDSWAP}), skipping field-swap"
            )
        fs.insufficient_data = True
        fs.elapsed_sec = round(time.time() - t0, 2)
        return fs

    if on_progress:
        on_progress(f"Testing {len(eligible):,} combed frames...")

    # Load video
    try:
        clip = core.ffms2.Source(filepath, threads=0)
    except Exception as e:
        fs.error = str(e)
        fs.elapsed_sec = round(time.time() - t0, 2)
        return fs

    total_frames = len(clip)

    if clip.format.color_family == vs.YUV:
        clip_y = core.std.ShufflePlanes(clip, planes=0, colorfamily=vs.GRAY)
    elif clip.format.color_family == vs.RGB:
        clip_y = core.std.ShufflePlanes(
            core.resize.Bicubic(clip, format=vs.YUV420P8),
            planes=0, colorfamily=vs.GRAY,
        )
    else:
        clip_y = clip

    # Frame cache (avoid reloading neighbors)
    frame_cache = {}

    def get_frame(idx):
        if idx not in frame_cache:
            f = clip_y.get_frame(idx)
            frame_cache[idx] = np.asarray(f[0], dtype=np.uint8).copy()
            if len(frame_cache) > 20:
                oldest = min(frame_cache.keys())
                del frame_cache[oldest]
        return frame_cache[idx]

    # Test field swaps
    fixed_count = 0
    unfixable_count = 0
    tested = 0
    swap_results = []
    last_report = t0

    for si, frame_idx in enumerate(eligible):
        if check_cancelled and check_cancelled():
            fs.error = "cancelled"
            break

        # Skip edge frames (need ±2 neighbors)
        if frame_idx < 2 or frame_idx >= total_frames - 2:
            continue

        curr = get_frame(frame_idx)
        original_ratio = per_frame[frame_idx]["ratio"] if frame_idx < len(per_frame) \
            else _compute_combing_ratio(curr, np)[0]

        # Try 8 field swaps (4 neighbors × top/bottom)
        best_ratio = original_ratio
        best_type = "none"

        neighbors = {
            "prev1": frame_idx - 1,
            "next1": frame_idx + 1,
            "prev2": frame_idx - 2,
            "next2": frame_idx + 2,
        }

        for name, n_idx in neighbors.items():
            if n_idx < 0 or n_idx >= total_frames:
                continue
            donor = get_frame(n_idx)

            swapped_top = _swap_top_field(curr, donor)
            r_top = _compute_combing_ratio(swapped_top, np)[0]
            if r_top < best_ratio:
                best_ratio = r_top
                best_type = f"top<-{name}"

            swapped_bot = _swap_bottom_field(curr, donor)
            r_bot = _compute_combing_ratio(swapped_bot, np)[0]
            if r_bot < best_ratio:
                best_ratio = r_bot
                best_type = f"bot<-{name}"

        is_fixed = best_ratio < FIXED_THRESHOLD
        if is_fixed:
            fixed_count += 1
        else:
            unfixable_count += 1
        tested += 1

        swap_results.append({
            "frame": frame_idx,
            "original": round(original_ratio, 3),
            "best": round(best_ratio, 3),
            "swap": best_type,
            "fixed": is_fixed,
        })

        # Progress
        now = time.time()
        if on_progress and now - last_report > 5.0:
            on_progress(
                f"Layer 3: {si + 1}/{len(eligible)} tested, "
                f"fixed: {fixed_count}, unfixable: {unfixable_count}"
            )
            last_report = now

    fs.sampled = len(eligible)
    fs.tested = tested
    fs.fixed_count = fixed_count
    fs.unfixable_count = unfixable_count
    fs.fix_pct = round((fixed_count / tested * 100) if tested > 0 else -1.0, 2)
    fs.insufficient_data = False
    fs.swap_results = swap_results
    fs.elapsed_sec = round(time.time() - t0, 2)

    if on_progress:
        on_progress(
            f"Layer 3 complete: {tested} tested, "
            f"{fixed_count} fixed ({fs.fix_pct:.1f}%), "
            f"{unfixable_count} unfixable ({fs.elapsed_sec:.1f}s)"
        )

    return fs
