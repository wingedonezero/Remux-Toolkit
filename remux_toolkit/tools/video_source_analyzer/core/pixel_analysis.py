"""Layer 2: TIVTC-style Laplacian combing detection + cadence analysis.

Uses the proven 5-tap vertical Laplacian kernel [1, -3, 4, -3, 1] from
TIVTC/HandBrake to detect combing artifacts, with a temporal motion gate
to avoid false positives on static content.

Frame-level combing is determined via block-based counting (COMBPEL threshold
per 16x16 block), matching the approach used by HandBrake/TIVTC.

Classification signals:
- combed_pct: percentage of frames flagged as combed
- cadence5_pct: how strongly combed frames follow period-5 (3:2 telecine)
- These two metrics cleanly separate content types:
    ~30-40% combed + high cadence-5  → hard telecine
    >40% combed + low cadence-5      → interlaced
    <15% combed                      → progressive
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Callable, Optional

from .models import PixelResult, StreamInfo


# ── TIVTC-equivalent thresholds (8-bit baseline, scaled for higher depths) ──
CTHRESH_8BIT = 9          # adjacent-line diff threshold
MTHRESH_8BIT = 10         # motion threshold (temporal gate)
BLOCK_SIZE = 16            # block size for combed pixel counting
COMBPEL = 40               # combed pixels per block to flag frame as combed

# Classification thresholds (applied to combed_pct and cadence5_pct)
TELECINE_COMBED_MIN = 20.0     # minimum combed% to consider telecine
TELECINE_COMBED_MAX = 45.0     # above this, likely interlaced not telecine
TELECINE_CADENCE5_MIN = 15.0   # minimum cadence-5% for telecine
INTERLACED_COMBED_MIN = 35.0   # minimum combed% for interlaced
PROGRESSIVE_COMBED_MAX = 15.0  # below this, progressive


def run_layer2(
    filepath: str,
    stream_info: StreamInfo,
    include_per_frame: bool = True,
    on_progress: Optional[Callable[[str], None]] = None,
    check_cancelled: Optional[Callable[[], bool]] = None,
) -> PixelResult:
    """
    TIVTC-style combing detection with cadence analysis.

    Decodes full video via VapourSynth. Per-frame:
    1. 5-tap vertical Laplacian for combing detection
    2. Temporal motion gate to suppress false positives
    3. Block-based frame decision (COMBPEL per 16x16 block)
    4. Field SAD for duplicate field tracking

    Post-processing:
    - Cadence-5 analysis on combed frame indices
    - Gap distribution for pattern identification
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

    # Scale thresholds for bit depth
    bits = clip_y.format.bits_per_sample
    scale = 1 << (bits - 8)  # 1 for 8-bit, 4 for 10-bit, 16 for 12-bit
    cthresh = CTHRESH_8BIT * scale
    mthresh = MTHRESH_8BIT * scale
    lap_thresh = cthresh * 6  # Laplacian threshold = 6x cthresh (TIVTC convention)

    # Per-frame analysis
    per_frame = []
    combed_indices = []
    prev_frame = None
    prev_top_field = None
    prev_bot_field = None
    top_field_sads = []
    bot_field_sads = []
    combed_px_pcts = []
    last_report = t0

    # Use VapourSynth's built-in frame prefetching for parallel decode
    # prefetch=8 means up to 8 frames decoded in parallel by ffms2
    frame_iter = clip_y.frames(prefetch=8, backlog=16, close=True)

    for i, frame in enumerate(frame_iter):
        if check_cancelled and check_cancelled():
            px.error = "cancelled"
            px.elapsed_sec = time.time() - t0
            return px

        # int16 is ~2x faster than float32 and sufficient for our math
        # (no overflow at 8-12 bit luma values with our kernel coefficients)
        arr = np.asarray(frame[0]).astype(np.int16, copy=False)
        h, w = arr.shape

        # ── 1. Laplacian combing detection ────────────────────────────
        # 5-tap kernel [1, -3, 4, -3, 1] applied vertically
        if h >= 5:
            lap = np.abs(
                arr[0:h-4] + 4*arr[2:h-2] + arr[4:h]
                - 3*(arr[1:h-3] + arr[3:h-1])
            )
            adj_diff = np.abs(arr[2:h-2] - arr[3:h-1])
            combed_mask = (adj_diff > cthresh) & (lap > lap_thresh)
        else:
            combed_mask = np.zeros((max(h-4, 1), w), dtype=bool)

        # ── 2. Motion gate (temporal) ─────────────────────────────────
        if prev_frame is not None and h >= 5:
            motion = np.abs(arr - prev_frame)
            motion_region = motion[2:h-2]
            combed_mask = combed_mask & (motion_region > mthresh)

        # ── 3. Block-based frame decision (vectorized) ────────────────
        # Reshape combed_mask into 16x16 blocks and sum each block
        # in a single numpy operation instead of Python loops
        gh, gw = combed_mask.shape
        total_combed_px = int(combed_mask.sum())
        combed_px_pct = total_combed_px / max(combed_mask.size, 1) * 100

        # Pad to multiple of BLOCK_SIZE so reshape works
        pad_h = (BLOCK_SIZE - gh % BLOCK_SIZE) % BLOCK_SIZE
        pad_w = (BLOCK_SIZE - gw % BLOCK_SIZE) % BLOCK_SIZE
        if pad_h or pad_w:
            mask_padded = np.pad(combed_mask, ((0, pad_h), (0, pad_w)))
        else:
            mask_padded = combed_mask
        gh2, gw2 = mask_padded.shape
        # Reshape to (rows, BLOCK, cols, BLOCK), sum over the BLOCK axes
        blocks = mask_padded.reshape(
            gh2 // BLOCK_SIZE, BLOCK_SIZE, gw2 // BLOCK_SIZE, BLOCK_SIZE
        ).sum(axis=(1, 3), dtype=np.int32)
        max_block_count = int(blocks.max()) if blocks.size > 0 else 0
        is_combed = max_block_count > COMBPEL

        combed_px_pcts.append(combed_px_pct)

        # ── 4. Field SAD (duplicate field tracking) ───────────────────
        top_f = arr[0::2]
        bot_f = arr[1::2]
        top_fsad = None
        bot_fsad = None

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
        prev_frame = arr

        if is_combed:
            combed_indices.append(i)

        # Store per-frame data
        if include_per_frame:
            per_frame.append({
                "idx": i,
                "combed": is_combed,
                "combed_px_pct": round(combed_px_pct, 3),
                "max_block": max_block_count,
                "top_fsad": round(top_fsad, 4) if top_fsad is not None else None,
                "bot_fsad": round(bot_fsad, 4) if bot_fsad is not None else None,
            })
        else:
            per_frame.append({
                "idx": i,
                "combed": is_combed,
            })

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

    # ── Combing statistics ────────────────────────────────────────────
    px.combed_frames = len(combed_indices)
    px.combed_pct = round(px.combed_frames / total * 100, 2) if total > 0 else 0
    px.combed_indices = combed_indices

    # Combed pixel percentage stats
    cp_arr = np.array(combed_px_pcts) if combed_px_pcts else np.array([0.0])
    px.combed_px_mean = round(float(np.mean(cp_arr)), 4)
    px.combed_px_median = round(float(np.median(cp_arr)), 4)
    px.combed_px_p95 = round(float(np.percentile(cp_arr, 95)), 4)
    px.frames_with_any_combing = int(np.sum(cp_arr > 0))
    px.frames_with_any_combing_pct = round(
        px.frames_with_any_combing / total * 100, 2) if total > 0 else 0

    # ── Telecine cadence analysis ─────────────────────────────────────
    # Real 3:2 pulldown produces combed frames at strict positions like
    #   2,3, 7,8, 12,13, 17,18, 22,23 ...
    # which yields a gap sequence of 1,4,1,4,1,4 ...
    # Consecutive gap pairs are STRICTLY (1,4) or (4,1).
    #
    # The previous "any pair summing to 5" metric was triggered by (2,3)
    # patterns from interlaced content with motion clusters, causing false
    # telecine detection. We now require the actual structural fingerprint:
    # the (1,4)+(4,1) pattern.
    px.cadence5_pct = 0.0
    px.telecine_gap_pct = 0.0
    px.gap_distribution = {}

    if len(combed_indices) > 5:
        gaps = [combed_indices[j+1] - combed_indices[j]
                for j in range(len(combed_indices) - 1)]
        gap_counts = Counter(gaps)
        px.gap_distribution = {str(k): v for k, v in gap_counts.most_common(10)}

        # Telecine cadence: ONLY (1,4) and (4,1) consecutive gap pairs
        # This is the exact structural fingerprint of NTSC 3:2 pulldown
        telecine_pairs = sum(
            1 for j in range(len(gaps) - 1)
            if (gaps[j] == 1 and gaps[j+1] == 4)
            or (gaps[j] == 4 and gaps[j+1] == 1)
        )
        total_pairs = len(gaps) - 1
        if total_pairs > 0:
            px.cadence5_pct = round(telecine_pairs / total_pairs * 100, 2)

        # Telecine gap balance: gap=1 and gap=4 should both be present
        # in real telecine. In interlaced content gap=4 is rare.
        gap1 = gap_counts.get(1, 0)
        gap4 = gap_counts.get(4, 0)
        total_gaps = len(gaps)
        px.telecine_gap_pct = round((gap1 + gap4) / total_gaps * 100, 2) if total_gaps > 0 else 0

    # ── Field SAD statistics ──────────────────────────────────────────
    if top_field_sads:
        top_arr = np.array(top_field_sads)
        bot_arr = np.array(bot_field_sads)
        px.top_field_sad_median = round(float(np.median(top_arr)), 4)
        px.bot_field_sad_median = round(float(np.median(bot_arr)), 4)
        px.top_field_sad_p5 = round(float(np.percentile(top_arr, 5)), 4)
        px.bot_field_sad_p5 = round(float(np.percentile(bot_arr, 5)), 4)

    # ── Finalize ──────────────────────────────────────────────────────
    px.bit_depth = bits
    px.bit_depth_scale = scale
    px.per_frame = per_frame
    px.elapsed_sec = round(elapsed, 2)
    px.frames_per_sec = round(rate_fps, 1)

    if on_progress:
        on_progress(
            f"Layer 2 complete: {total:,} frames in {elapsed:.1f}s "
            f"@ {rate_fps:.0f} f/s | "
            f"combed={px.combed_pct:.1f}%, cadence5={px.cadence5_pct:.1f}%"
        )

    return px
