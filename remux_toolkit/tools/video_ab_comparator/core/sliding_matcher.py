# remux_toolkit/tools/video_ab_comparator/core/sliding_matcher.py
"""
Sliding-window frame matching via GPU pHash.

Standalone port of the video-verified sliding pipeline (Video-Sync-GUI
current design). This is the comparator's own implementation — no code
is shared with Video-Sync-GUI.

What it does:

- Opens both videos via VapourSynth + FFMS2 (frame-exact index access,
  per-file .ffindex cache keyed on size+mtime).
- Probes timeline integrity for both files: does frame index n actually
  sit at wall-clock slot n? Detects dropped/extra frame slots (pts gaps).
- Applies PTS-origin correction: if the containers have different start
  PTS, the search window center is shifted so the slide is centered on
  wall-clock equality.
- Extracts a GPU DCT-II perceptual hash per frame (no model weights,
  1024-bit at the default hash_size=32) and slides a source window
  across a padded target window scoring mean cosine similarity.
- Votes across N positions for a consensus answer with HIGH/MEDIUM/LOW
  confidence.

Two offset domains are reported, because the comparator needs both:

- ``offset_frames`` — CONTENT-INDEX domain: the raw ffms2 frame-index
  delta of the matched content (``tgt_index - src_index``). This is what
  FrameMapper needs for direct frame-to-frame mapping, and it includes
  any PTS-label shift naturally (it's where the content actually is).
- ``offset_ms`` — WALL-CLOCK domain: the real container-timestamp
  difference of the matched frame pair (``_AbsoluteTime`` props). This
  is what every ``ts_b = ts_a + offset`` consumer needs. When container
  timestamps are unavailable it falls back to index math minus the PTS
  label delta (correct under the gapless-CFR assumption, which the
  timeline probe verifies).

For the common case (both files start at PTS 0, no pts gaps) the two
domains agree exactly and behavior is identical to a PTS-unaware matcher.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np


# ── Public entry point ───────────────────────────────────────────────────────


def calculate_sliding_offset(
    source_a_path: str,
    source_b_path: str,
    audio_offset_ms: float,
    fps_a: float,
    fps_b: float,
    duration_sec: float,
    num_positions: int = 9,
    window_seconds: int = 10,
    slide_range_seconds: int = 5,
    batch_size: int = 32,
    hash_size: int = 32,
    temp_dir: Optional[Path] = None,
    debug_output_dir: Optional[Path] = None,
    progress_callback=None,
) -> dict[str, Any]:
    """Find the frame offset between two video sources via GPU pHash.

    Returns a dict with ``success``, ``offset_ms`` (wall-clock),
    ``offset_frames`` (content-index, for FrameMapper), ``confidence``,
    ``confidence_label``, ``method``, per-position results, consensus,
    PTS-correction and timeline-integrity metadata.
    """
    def log(msg: str):
        print(msg)

    log("[SlidingMatch] === Sliding-Window pHash Matching ===")
    log(f"[SlidingMatch] Source A: {Path(source_a_path).name}")
    log(f"[SlidingMatch] Source B: {Path(source_b_path).name}")
    log(f"[SlidingMatch] Audio offset: {audio_offset_ms:+.3f}ms")
    log(f"[SlidingMatch] Hash size: {hash_size} ({hash_size*hash_size}-bit)")

    if progress_callback:
        progress_callback("Loading sliding matcher...", 92)

    # ── Dependency checks ─────────────────────────────────────────
    try:
        import vapoursynth as vs
    except ImportError as e:
        log(f"[SlidingMatch] VapourSynth not available: {e}")
        return _fallback_result(audio_offset_ms, fps_a, "fallback-no-vapoursynth",
                                f"VapourSynth unavailable: {e}")

    # Pin ROCm to the discrete GPU before torch initializes HIP: on a
    # dual-GPU box the iGPU can SIGSEGV on first kernel launch.
    # setdefault: respected only when the environment hasn't already
    # chosen a device (run.sh exports this too).
    os.environ.setdefault("HIP_VISIBLE_DEVICES", "0")

    try:
        import torch
    except ImportError as e:
        log(f"[SlidingMatch] PyTorch not available: {e}")
        return _fallback_result(audio_offset_ms, fps_a, "fallback-no-torch",
                                f"PyTorch unavailable: {e}")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # ── Open clips with PTS metadata ──────────────────────────────
    try:
        src_yuv, src_rgb, src_start_pts_s = _open_clip(source_a_path, vs, temp_dir)
        tgt_yuv, tgt_rgb, tgt_start_pts_s = _open_clip(source_b_path, vs, temp_dir)
    except Exception as e:
        log(f"[SlidingMatch] Failed to open videos: {e}")
        return _fallback_result(audio_offset_ms, fps_a, "fallback-video-open-failed",
                                f"Failed to open videos: {e}")

    src_fps = src_yuv.fps.numerator / src_yuv.fps.denominator
    tgt_fps = tgt_yuv.fps.numerator / tgt_yuv.fps.denominator
    if src_fps <= 0:
        src_fps = fps_a if fps_a > 0 else 23.976
    if tgt_fps <= 0:
        tgt_fps = fps_b if fps_b > 0 else src_fps
    src_frame_dur_ms = 1000.0 / src_fps

    log(
        f"[SlidingMatch] Source: {src_yuv.num_frames}f @ {src_fps:.3f}fps  "
        f"start_pts={src_start_pts_s:+.6f}s"
    )
    log(
        f"[SlidingMatch] Target: {tgt_yuv.num_frames}f @ {tgt_fps:.3f}fps  "
        f"start_pts={tgt_start_pts_s:+.6f}s"
    )

    # ── Timeline integrity (frame-index ↔ wall-clock) ─────────────
    # A file with dropped frame slots keeps correct wall-clock stamps
    # but its frame indices no longer map to wall-clock via
    # index * frame_duration. Per-position offsets below are converted
    # to wall-clock with the REAL timestamps of the matched pair; this
    # probe tells the log (and the results dict) which regime the
    # files are in.
    src_probe = _probe_timeline_integrity(
        src_yuv.num_frames, src_fps, src_start_pts_s,
        lambda n: _frame_abs_time_s(src_yuv, n),
    )
    tgt_probe = _probe_timeline_integrity(
        tgt_yuv.num_frames, tgt_fps, tgt_start_pts_s,
        lambda n: _frame_abs_time_s(tgt_yuv, n),
    )
    for label, probe in (("Source A", src_probe), ("Source B", tgt_probe)):
        if probe.ok is True:
            log(f"[SlidingMatch] Timeline integrity: {label} OK (no pts gaps)")
        elif probe.ok is None:
            log(
                f"[SlidingMatch] ⚠ Timeline integrity: {label} timestamps "
                f"unavailable — cannot verify frame-index ↔ wall-clock mapping"
            )
        else:
            where = (
                f"first gap at ~{probe.first_divergence_time_s:.3f}s "
                f"(frame index {probe.first_divergence_index})"
                if probe.first_divergence_time_s is not None
                else "location unknown"
            )
            log(
                f"[SlidingMatch] ⚠ Timeline integrity: {label} has "
                f"{abs(probe.missing_slots)} "
                f"{'missing' if probe.missing_slots > 0 else 'extra'} frame "
                f"slot(s) — {where}"
            )

    # ── PTS correction ────────────────────────────────────────────
    # If the containers have different PTS origins, their frame-index
    # spaces are shifted by a constant. Shift the target window center
    # so the sliding search is centered on wall-clock equality. For the
    # common case (both start_pts = 0) this is a no-op.
    pts_delta_s = src_start_pts_s - tgt_start_pts_s
    pts_delta_frames = int(round(pts_delta_s * src_fps))
    pts_correction_applied = pts_delta_frames != 0

    if pts_correction_applied:
        log("[SlidingMatch] ─────────────────────────────────────")
        log("[SlidingMatch] ⚠ PTS DELTA DETECTED — shifting search center")
        log(f"[SlidingMatch]   Source A start_pts: {src_start_pts_s:+.6f}s")
        log(f"[SlidingMatch]   Source B start_pts: {tgt_start_pts_s:+.6f}s")
        log(
            f"[SlidingMatch]   Delta: {pts_delta_s:+.6f}s "
            f"= {pts_delta_frames:+d} frames"
        )
        log("[SlidingMatch]   offset_ms will be wall-clock; offset_frames content-index")
        log("[SlidingMatch] ─────────────────────────────────────")

    # ── FPS compatibility check ──────────────────────────────────
    fps_ratio = max(src_fps, tgt_fps) / min(src_fps, tgt_fps)
    if fps_ratio > 1.01:
        log(
            f"[SlidingMatch] FPS mismatch ({src_fps:.3f} vs {tgt_fps:.3f}), "
            f"ratio={fps_ratio:.4f} — falling back to audio"
        )
        return _fallback_result(audio_offset_ms, fps_a, "fallback-cross-fps",
                                "FPS mismatch")

    # ── Sliding geometry ─────────────────────────────────────────
    src_n_frames = int(window_seconds * src_fps)
    slide_pad = int(slide_range_seconds * tgt_fps)

    log(f"[SlidingMatch] Window: {src_n_frames} frames ({window_seconds}s)")
    log(f"[SlidingMatch] Slide: ±{slide_pad} frames (±{slide_range_seconds}s)")
    log(f"[SlidingMatch] Positions: {num_positions}, Batch: {batch_size}")

    # ── Build DCT basis / luma weights on device ─────────────────
    dct_size = max(32, hash_size * 4)
    dct_matrix = _dct_ii_matrix(dct_size, device, torch)
    luma = torch.tensor([0.299, 0.587, 0.114], device=device).view(1, 3, 1, 1)

    # ── Positions evenly across 10%–90% ──────────────────────────
    positions_pct = [10 + 80 * (i + 0.5) / num_positions for i in range(num_positions)]

    log("[SlidingMatch] ─────────────────────────────────────")
    results: list[dict[str, Any]] = []
    landscapes: list[dict[str, Any]] = []
    t_total_start = time.time()

    for i, pct in enumerate(positions_pct):
        if progress_callback:
            progress = 93 + int(5 * (i / num_positions))
            progress_callback(
                f"Sliding position {i+1}/{num_positions}...", progress
            )

        t_pos_start = time.time()

        src_start = int(src_rgb.num_frames * pct / 100.0)
        src_end = min(src_start + src_n_frames, src_rgb.num_frames)
        src_frames = list(range(src_start, src_end))

        # Target window centered at the PTS-shifted source index so
        # slide_pos == slide_pad corresponds to wall-clock equality.
        tgt_center = src_start + pts_delta_frames
        tgt_window_start = max(0, tgt_center - slide_pad)
        tgt_window_end = min(
            tgt_rgb.num_frames, tgt_center + src_n_frames + slide_pad
        )
        tgt_frames = list(range(tgt_window_start, tgt_window_end))

        if len(tgt_frames) <= len(src_frames):
            log(
                f"[SlidingMatch]   [{i+1}/{num_positions}] {pct:.0f}% — "
                f"SKIPPED (edge)"
            )
            continue

        try:
            src_feats = _extract_phash_descriptors(
                src_rgb, src_frames, device, batch_size, hash_size, dct_size,
                dct_matrix, luma, torch,
            )
            tgt_feats = _extract_phash_descriptors(
                tgt_rgb, tgt_frames, device, batch_size, hash_size, dct_size,
                dct_matrix, luma, torch,
            )
        except Exception as e:
            log(
                f"[SlidingMatch]   [{i+1}/{num_positions}] {pct:.0f}% — "
                f"EXTRACT ERROR: {e}"
            )
            continue

        scores, match_counts = _cosine_slide(src_feats, tgt_feats)

        if len(scores) == 0:
            log(
                f"[SlidingMatch]   [{i+1}/{num_positions}] {pct:.0f}% — "
                f"SKIPPED (no slides)"
            )
            continue

        best_pos = int(np.argmax(scores))

        # CONTENT-INDEX offset: where the matching content actually is,
        # in ffms2 frame indices. This is what FrameMapper consumes
        # (frame_b = frame_a + offset_frames).
        content_offset_frames = (tgt_window_start + best_pos) - src_start
        # Index offset with the PTS label delta removed — the wall-clock
        # offset under the gapless-CFR assumption.
        index_offset_frames = content_offset_frames - pts_delta_frames

        # WALL-CLOCK offset: real container timestamps of the matched
        # pair are authoritative for any ts_b = ts_a + offset consumer.
        src_abs = _frame_abs_time_s(src_yuv, src_start)
        tgt_abs = _frame_abs_time_s(tgt_yuv, tgt_window_start + best_pos)
        if src_abs is not None and tgt_abs is not None:
            wallclock_ms = (tgt_abs - src_abs) * 1000.0
            wallclock_frames = int(round(wallclock_ms / src_frame_dur_ms))
            pts_based = True
        else:
            wallclock_frames = index_offset_frames
            pts_based = False
        wallclock_offset_ms = wallclock_frames * src_frame_dur_ms
        divergence_frames = wallclock_frames - index_offset_frames

        gradient = _compute_gradient(scores, best_pos)
        dt = time.time() - t_pos_start

        results.append({
            "position_pct": pct,
            "src_start": src_start,
            "content_offset_frames": content_offset_frames,
            "index_offset_frames": index_offset_frames,
            "wallclock_frames": wallclock_frames,
            "wallclock_offset_ms": wallclock_offset_ms,
            "divergence_frames": divergence_frames,
            "pts_based": pts_based,
            "score": float(scores[best_pos]),
            "matches": int(match_counts[best_pos]),
            "total": len(src_frames),
            "gradient": gradient,
            "time_s": dt,
        })

        landscapes.append({
            "position_pct": pct,
            "scores": scores.tolist(),
            "best_pos": best_pos,
            "tgt_window_start": tgt_window_start,
            "src_start": src_start,
            "divergence_frames": divergence_frames,
        })

        divergence_note = (
            f" [timeline-gap corrected from index {index_offset_frames:+d}f]"
            if divergence_frames != 0
            else ("" if pts_based else " [index-math: timestamps unavailable]")
        )
        log(
            f"[SlidingMatch]   [{i+1}/{num_positions}] {pct:.0f}% @{src_start}f → "
            f"offset={wallclock_frames:+d}f ({wallclock_offset_ms:+.1f}ms) "
            f"score={scores[best_pos]:.4f} "
            f"match={int(match_counts[best_pos])}/{len(src_frames)} "
            f"grad={gradient:.4f}/f ({dt:.1f}s)"
            f"{divergence_note}"
        )

    dt_total = time.time() - t_total_start

    # Release GPU tensors + cuda cache before returning
    del dct_matrix, luma
    try:
        from .gpu_backend import cleanup_gpu
        cleanup_gpu()
    except Exception:
        pass

    if not results:
        log("[SlidingMatch] No valid positions — falling back to audio correlation")
        return _fallback_result(audio_offset_ms, fps_a, "fallback-no-valid-positions",
                                "No valid positions")

    # ── Consensus (wall-clock domain drives confidence) ──────────
    wallclock_list = [r["wallclock_frames"] for r in results]
    scores_list = [r["score"] for r in results]
    consensus_frames, consensus_count = Counter(wallclock_list).most_common(1)[0]
    consensus_ms = consensus_frames * src_frame_dur_ms

    # Content-index consensus for FrameMapper.
    content_list = [r["content_offset_frames"] for r in results]
    content_consensus_frames = Counter(content_list).most_common(1)[0][0]

    # Timeline self-consistency bookkeeping.
    index_list = [r["index_offset_frames"] for r in results]
    index_consensus_frames = Counter(index_list).most_common(1)[0][0]
    divergences = [r["divergence_frames"] for r in results if r["pts_based"]]
    positions_pts_based = sum(1 for r in results if r["pts_based"])
    timeline_correction_frames = consensus_frames - index_consensus_frames
    timeline_correction_applied = timeline_correction_frames != 0
    divergence_inconsistent = len(set(divergences)) > 1

    consensus_ratio = consensus_count / len(results)
    mean_score = float(np.mean(scores_list))
    min_score = float(min(scores_list))
    mean_gradient = float(np.mean([r["gradient"] for r in results]))

    if consensus_ratio >= 0.9 and mean_score >= 0.98:
        confidence_label = "HIGH"
    elif consensus_ratio >= 0.7 and mean_score >= 0.95:
        confidence_label = "MEDIUM"
    else:
        confidence_label = "LOW"

    log("[SlidingMatch] ═══════════════════════════════════════")
    log("[SlidingMatch] RESULTS SUMMARY")
    log("[SlidingMatch] ═══════════════════════════════════════")
    log(
        f"[SlidingMatch] Consensus: {consensus_frames:+d}f = "
        f"{consensus_ms:+.1f}ms ({consensus_count}/{len(results)} positions)"
    )
    if timeline_correction_applied:
        log(
            f"[SlidingMatch] ⚠ TIMELINE GAP CORRECTION: frame-index match was "
            f"{index_consensus_frames:+d}f but real timestamps give "
            f"{consensus_frames:+d}f — wall-clock is authoritative"
        )
    elif positions_pts_based == len(results):
        log(
            "[SlidingMatch] Timeline check: OK — frame-index and wall-clock "
            "offsets agree at all positions"
        )
    if positions_pts_based < len(results):
        log(
            f"[SlidingMatch] ⚠ Timeline check: "
            f"{len(results) - positions_pts_based}/{len(results)} position(s) "
            f"had no timestamps and used frame-index math"
        )
    if divergence_inconsistent:
        log(
            f"[SlidingMatch] ⚠ Timeline divergence INCONSISTENT across positions "
            f"({sorted(set(divergences))}) — possible mid-file timestamp anomaly"
        )
    log(
        f"[SlidingMatch] Mean score: {mean_score:.4f}, "
        f"Range: [{min_score:.4f}, {max(scores_list):.4f}]"
    )
    log(f"[SlidingMatch] Mean gradient: {mean_gradient:.4f}/frame")
    log(f"[SlidingMatch] Confidence: {confidence_label}")
    log(f"[SlidingMatch] Audio offset:    {audio_offset_ms:+.3f}ms")
    log(f"[SlidingMatch] Sliding offset:  {consensus_ms:+.3f}ms (wall-clock)")

    diff_ms = consensus_ms - audio_offset_ms
    diff_frames = diff_ms / src_frame_dur_ms
    log(f"[SlidingMatch] Diff from audio: {diff_ms:+.1f}ms ({diff_frames:+.1f}f)")
    if abs(diff_ms) > src_frame_dur_ms / 2:
        log("[SlidingMatch] SLIDING OFFSET DIFFERS FROM AUDIO CORRELATION")

    log(f"[SlidingMatch] Total time: {dt_total:.1f}s")

    # Score landscape summary for top positions
    for land in landscapes[:3]:
        sc = np.array(land["scores"])
        bp = land["best_pos"]
        lsrc_start = land["src_start"]
        ltgt_ws = land["tgt_window_start"]
        ldiv = land.get("divergence_frames", 0)
        best_off_f = (ltgt_ws + bp) - lsrc_start - pts_delta_frames + ldiv
        log(
            f"[SlidingMatch]   Landscape {land['position_pct']:.0f}%: "
            f"peak {best_off_f:+d}f score={sc[bp]:.4f}"
        )
        for delta in range(-5, 6):
            pos = bp + delta
            if 0 <= pos < len(sc):
                off_f = (ltgt_ws + pos) - lsrc_start - pts_delta_frames + ldiv
                marker = " ★" if delta == 0 else ""
                log(
                    f"[SlidingMatch]     {off_f:+4d}f: {sc[pos]:.4f}{marker}"
                )
    log("[SlidingMatch] ═══════════════════════════════════════")

    confidence_float = {"HIGH": 0.95, "MEDIUM": 0.75, "LOW": 0.4}[confidence_label]

    result = {
        "success": True,
        "reason": "sliding-matched",
        # Wall-clock offset — for ts_b = ts_a + offset consumers.
        "offset_ms": consensus_ms,
        # Content-index offset — for FrameMapper frame_b = frame_a + offset.
        "offset_frames": content_consensus_frames,
        "wallclock_offset_frames": consensus_frames,
        "index_offset_frames": index_consensus_frames,
        "confidence": confidence_float,
        "confidence_label": confidence_label,
        "method": "sliding-phash",
        "error": None,
        "consensus_count": consensus_count,
        "num_positions": len(results),
        "consensus_ratio": consensus_ratio,
        "mean_score": mean_score,
        "min_score": min_score,
        "mean_gradient": mean_gradient,
        "source_fps": src_fps,
        "target_fps": tgt_fps,
        "total_time_s": dt_total,
        "per_position_results": results,
        "hash_size": hash_size,
        "descriptor_bits": hash_size * hash_size,
        # PTS metadata
        "pts_correction_applied": pts_correction_applied,
        "src_start_pts_s": src_start_pts_s,
        "tgt_start_pts_s": tgt_start_pts_s,
        "pts_delta_s": pts_delta_s,
        "pts_delta_frames": pts_delta_frames,
        # Timeline integrity metadata
        "timeline_src_ok": src_probe.ok,
        "timeline_src_missing_slots": src_probe.missing_slots,
        "timeline_src_first_gap_s": src_probe.first_divergence_time_s,
        "timeline_tgt_ok": tgt_probe.ok,
        "timeline_tgt_missing_slots": tgt_probe.missing_slots,
        "timeline_tgt_first_gap_s": tgt_probe.first_divergence_time_s,
        "timeline_correction_applied": timeline_correction_applied,
        "timeline_correction_frames": timeline_correction_frames,
        "timeline_divergence_inconsistent": divergence_inconsistent,
        "positions_pts_based": positions_pts_based,
    }

    if debug_output_dir:
        _write_debug_report(
            debug_output_dir=Path(debug_output_dir),
            source_a=source_a_path,
            source_b=source_b_path,
            audio_offset_ms=audio_offset_ms,
            result=result,
            landscapes=landscapes,
            src_fps=src_fps,
            tgt_fps=tgt_fps,
            src_frame_dur_ms=src_frame_dur_ms,
            pts_delta_frames=pts_delta_frames,
            src_probe=src_probe,
            tgt_probe=tgt_probe,
            log=log,
        )

    return result


# ── Fallback ──────────────────────────────────────────────────────────────────


def _fallback_result(audio_offset_ms: float, fps: float, reason: str, error: str) -> dict:
    frame_dur_ms = 1000.0 / fps if fps > 0 else 41.708
    return {
        "success": False,
        "reason": reason,
        "offset_ms": audio_offset_ms,
        "offset_frames": round(audio_offset_ms / frame_dur_ms),
        "confidence": 0.3,
        "confidence_label": "LOW",
        "method": "audio-fallback",
        "error": error,
    }


# ── Timeline integrity ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class TimelineProbe:
    """Result of checking a clip's frame-index ↔ wall-clock mapping.

    ``ok=None`` means timestamps were unavailable and nothing could be
    verified (distinct from ``ok=True``, a positive confirmation).
    """

    ok: Optional[bool]
    num_frames: int
    missing_slots: int  # >0: dropped frame slots (pts gaps); <0: extra frames
    first_divergence_index: Optional[int]
    first_divergence_time_s: Optional[float]


def _probe_timeline_integrity(
    num_frames: int,
    fps: float,
    start_pts_s: float,
    pts_lookup: Callable[[int], Optional[float]],
) -> TimelineProbe:
    """Check whether frame index ``n`` sits at wall-clock slot ``n``.

    A clean CFR file satisfies ``round((pts(n) - pts(0)) * fps) == n``
    for every frame. A dropped frame slot breaks this from the gap
    onward. Cost: 1 pts read for the last frame, plus O(log n) reads
    to locate the first divergence when one exists.
    """
    if num_frames < 2 or fps <= 0:
        return TimelineProbe(True, num_frames, 0, None, None)

    def slot_of(n: int) -> Optional[int]:
        t = pts_lookup(n)
        if t is None:
            return None
        return round((t - start_pts_s) * fps)

    last_slot = slot_of(num_frames - 1)
    if last_slot is None:
        return TimelineProbe(None, num_frames, 0, None, None)

    missing = last_slot - (num_frames - 1)
    if missing == 0:
        return TimelineProbe(True, num_frames, 0, None, None)

    lo, hi = 0, num_frames - 1  # slot(0) == 0 by construction
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        mid_slot = slot_of(mid)
        if mid_slot is None:
            break
        if mid_slot == mid:
            lo = mid
        else:
            hi = mid
    first_idx = hi
    first_time = pts_lookup(first_idx)
    return TimelineProbe(False, num_frames, missing, first_idx, first_time)


# ── Clip I/O with PTS metadata ───────────────────────────────────────────────


def _open_clip(video_path: str, vs, temp_dir: Optional[Path] = None):
    """Open a video and return ``(yuv_clip, rgb_clip, start_pts_s)``.

    ``start_pts_s`` is the wall-clock time of frame 0 from ffms2's
    ``_AbsoluteTime`` property. 0.0 for well-formed files, non-zero for
    DVDs / re-encodes that preserve a wall-clock offset in the container.
    """
    core = vs.core
    cache_path = str(_get_ffms2_cache_path(video_path, temp_dir))

    try:
        clip = core.ffms2.Source(source=video_path, cachefile=cache_path)
    except Exception:
        stale = Path(cache_path)
        if stale.exists():
            stale.unlink(missing_ok=True)
        clip = core.ffms2.Source(source=video_path, cachefile=cache_path)

    rgb_clip = core.resize.Bicubic(clip, format=vs.RGB24, matrix_in_s="170m")

    try:
        start_pts_s = float(clip.get_frame(0).props.get("_AbsoluteTime", 0.0))
    except Exception:
        start_pts_s = 0.0

    return clip, rgb_clip, start_pts_s


def _frame_abs_time_s(clip, n: int) -> Optional[float]:
    """Real container timestamp of frame ``n`` in seconds, or ``None``.

    Returns ``None`` when the prop is missing or the frame can't be
    fetched — callers must fall back to frame-index math, never guess.
    """
    try:
        t = clip.get_frame(n).props.get("_AbsoluteTime")
        return float(t) if t is not None else None
    except Exception:
        return None


def _get_ffms2_cache_path(video_path: str, temp_dir: Optional[Path] = None) -> Path:
    video_path_obj = Path(video_path)
    stat = os.stat(video_path)
    file_size = stat.st_size
    mtime = int(stat.st_mtime)

    parent_dir = video_path_obj.parent.name
    if not parent_dir or parent_dir == '.':
        path_hash = hashlib.md5(str(video_path_obj.resolve()).encode()).hexdigest()[:8]
        cache_key = f"{video_path_obj.stem}_{path_hash}_{file_size}_{mtime}"
    else:
        cache_key = f"{parent_dir}_{video_path_obj.stem}_{file_size}_{mtime}"

    if temp_dir:
        cache_dir = Path(temp_dir) / "ffindex"
    else:
        import tempfile
        cache_dir = Path(tempfile.gettempdir()) / "remux_toolkit_ffindex"

    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{cache_key}.ffindex"


# ── GPU pHash descriptor extraction ──────────────────────────────────────────


def _dct_ii_matrix(N: int, device, torch):
    """Type-II orthonormal DCT basis of shape [N, N]."""
    n = torch.arange(N, device=device).float()
    k = torch.arange(N, device=device).float().unsqueeze(1)
    M = torch.cos((torch.pi / N) * (n + 0.5) * k)
    M[0] *= 1.0 / (N ** 0.5)
    M[1:] *= (2.0 / N) ** 0.5
    return M


def _extract_phash_descriptors(
    rgb_clip,
    frame_nums: list[int],
    device,
    batch_size: int,
    hash_size: int,
    dct_size: int,
    dct_matrix,
    luma,
    torch,
) -> np.ndarray:
    """Batched GPU pHash descriptor extractor.

    Returns ``[N, hash_size**2]`` with values in ``{-1/s, +1/s}`` where
    ``s = sqrt(hash_size**2)`` so every row has unit L2 norm.
    """
    import torch.nn.functional as F

    descriptor_dim = hash_size * hash_size
    scale = 1.0 / (descriptor_dim ** 0.5)

    all_desc: list = []
    batch_tensors: list = []

    first_frame = frame_nums[0]
    last_frame = frame_nums[-1]
    is_contiguous = frame_nums == list(range(first_frame, last_frame + 1))

    def push_tensor_from_frame(frame):
        r = np.asarray(frame[0])
        g = np.asarray(frame[1])
        b = np.asarray(frame[2])
        rgb_np = np.stack([r, g, b], axis=0).astype(np.float32) / 255.0
        tensor = torch.from_numpy(rgb_np).unsqueeze(0).to(device)
        resized = F.interpolate(
            tensor, size=(dct_size, dct_size), mode="bilinear", align_corners=False
        )
        batch_tensors.append(resized.squeeze(0))

    def flush_batch():
        if not batch_tensors:
            return
        batch = torch.stack(batch_tensors).to(device)  # [B, 3, D, D]
        gray = (batch * luma).sum(dim=1)  # [B, D, D]
        dct = dct_matrix @ gray @ dct_matrix.T
        low = dct[:, :hash_size, :hash_size]
        flat = low.reshape(low.shape[0], -1)
        med = flat[:, 1:].median(dim=1, keepdim=True).values
        bits = (flat > med).float()
        desc = (bits * 2.0 - 1.0) * scale
        all_desc.append(desc.cpu())
        batch_tensors.clear()

    with torch.no_grad():
        if is_contiguous:
            trimmed = rgb_clip[first_frame: last_frame + 1]
            for i, frame in enumerate(trimmed.frames()):
                push_tensor_from_frame(frame)
                if len(batch_tensors) == batch_size or i == len(frame_nums) - 1:
                    flush_batch()
        else:
            for idx, fn in enumerate(frame_nums):
                fn_clamped = max(0, min(fn, rgb_clip.num_frames - 1))
                frame = rgb_clip.get_frame(fn_clamped)
                push_tensor_from_frame(frame)
                if len(batch_tensors) == batch_size or idx == len(frame_nums) - 1:
                    flush_batch()

    return torch.cat(all_desc, dim=0).numpy()


# ── Cosine sliding + sharpness metric ────────────────────────────────────────


def _cosine_slide(
    src_feats: np.ndarray,
    tgt_feats: np.ndarray,
    match_threshold: float = 0.5,
) -> tuple[np.ndarray, np.ndarray]:
    """Slide src across tgt and return per-position mean cosine similarity."""
    S = len(src_feats)
    T = len(tgt_feats)
    max_slides = T - S + 1
    if max_slides <= 0:
        return np.array([], dtype=np.float64), np.array([], dtype=np.int64)

    src_norm = src_feats / (np.linalg.norm(src_feats, axis=1, keepdims=True) + 1e-8)
    tgt_norm = tgt_feats / (np.linalg.norm(tgt_feats, axis=1, keepdims=True) + 1e-8)

    scores = np.zeros(max_slides, dtype=np.float64)
    match_counts = np.zeros(max_slides, dtype=np.int64)
    for p in range(max_slides):
        pair_sims = np.sum(src_norm * tgt_norm[p: p + S], axis=1)
        scores[p] = pair_sims.mean()
        match_counts[p] = int(np.sum(pair_sims > match_threshold))

    return scores, match_counts


def _compute_gradient(scores: np.ndarray, best_pos: int) -> float:
    """Mean score drop-off per frame within ±5 frames of the peak."""
    if len(scores) < 3:
        return 0.0

    peak_score = scores[best_pos]
    gradients: list[float] = []
    for delta in range(1, 6):
        for sign in (-1, 1):
            pos = best_pos + sign * delta
            if 0 <= pos < len(scores):
                drop = peak_score - scores[pos]
                gradients.append(drop / delta)

    return float(np.mean(gradients)) if gradients else 0.0


# ── Debug report writer ──────────────────────────────────────────────────────


def _write_debug_report(
    debug_output_dir: Path,
    source_a: str,
    source_b: str,
    audio_offset_ms: float,
    result: dict,
    landscapes: list[dict],
    src_fps: float,
    tgt_fps: float,
    src_frame_dur_ms: float,
    pts_delta_frames: int,
    src_probe: TimelineProbe,
    tgt_probe: TimelineProbe,
    log,
) -> None:
    """Write a full score-landscape report next to the run's temp data."""
    try:
        debug_output_dir.mkdir(parents=True, exist_ok=True)
        report_path = debug_output_dir / "sliding_match_report.txt"

        lines: list[str] = []
        lines.append("=" * 80)
        lines.append("SLIDING-WINDOW pHASH MATCHING DEBUG REPORT")
        lines.append("=" * 80)
        lines.append(f"Source A: {source_a}")
        lines.append(f"Source B: {source_b}")
        lines.append(f"Source A FPS: {src_fps:.3f}   Source B FPS: {tgt_fps:.3f}")
        lines.append(f"Frame duration: {src_frame_dur_ms:.2f}ms")
        lines.append(f"Audio correlation: {audio_offset_ms:+.3f}ms")
        if pts_delta_frames != 0:
            lines.append(f"PTS delta correction applied: {pts_delta_frames:+d} frames")
        lines.append("")

        lines.append("-" * 80)
        lines.append("TIMELINE INTEGRITY (frame-index <-> wall-clock)")
        lines.append("-" * 80)
        for label, probe in (("Source A", src_probe), ("Source B", tgt_probe)):
            if probe.ok is None:
                lines.append(f"  {label}: UNVERIFIED (container timestamps unavailable)")
            elif probe.ok:
                lines.append(f"  {label}: OK (no pts gaps)")
            else:
                where = (
                    f" — first gap at ~{probe.first_divergence_time_s:.3f}s "
                    f"(frame index {probe.first_divergence_index})"
                    if probe.first_divergence_time_s is not None
                    else ""
                )
                lines.append(
                    f"  {label}: {abs(probe.missing_slots)} "
                    f"{'missing' if probe.missing_slots > 0 else 'extra'} "
                    f"frame slot(s){where}"
                )
        if result.get("timeline_correction_applied"):
            lines.append(
                f"  GAP CORRECTION APPLIED: frame-index consensus was "
                f"{result['index_offset_frames']:+d}f; real timestamps give "
                f"{result['wallclock_offset_frames']:+d}f"
            )
        else:
            lines.append(
                "  No correction needed: frame-index and wall-clock offsets agree."
            )
        lines.append("")
        lines.append(
            f"RESULT: wall-clock {result['wallclock_offset_frames']:+d}f = "
            f"{result['offset_ms']:+.1f}ms; content-index "
            f"{result['offset_frames']:+d}f "
            f"({result['consensus_count']}/{result['num_positions']} consensus)"
        )
        lines.append(f"Confidence: {result['confidence_label']}")
        lines.append(f"Mean score: {result['mean_score']:.4f}")
        lines.append(f"Total time: {result['total_time_s']:.1f}s")
        lines.append("")

        lines.append("-" * 80)
        lines.append("PER-POSITION RESULTS")
        lines.append("-" * 80)
        for r in result["per_position_results"]:
            div = r.get("divergence_frames", 0)
            div_note = (
                f" [gap-corrected from index {r['index_offset_frames']:+d}f]"
                if div != 0
                else ("" if r.get("pts_based", True) else " [index-math]")
            )
            lines.append(
                f"  {r['position_pct']:5.1f}% @{r['src_start']:6d}f: "
                f"offset={r['wallclock_frames']:+4d}f "
                f"({r['wallclock_offset_ms']:+8.1f}ms) "
                f"score={r['score']:.4f} match={r['matches']}/{r['total']} "
                f"grad={r['gradient']:.4f}/f ({r['time_s']:.1f}s){div_note}"
            )
        lines.append("")

        lines.append("-" * 80)
        lines.append("SCORE LANDSCAPES (all positions)")
        lines.append("-" * 80)
        for land in landscapes:
            sc = np.array(land["scores"])
            bp = land["best_pos"]
            src_start = land["src_start"]
            tgt_ws = land["tgt_window_start"]
            ldiv = land.get("divergence_frames", 0)
            best_off_f = (tgt_ws + bp) - src_start - pts_delta_frames + ldiv
            best_off_ms = best_off_f * src_frame_dur_ms

            lines.append("")
            lines.append(
                f"  Position {land['position_pct']:.0f}% (src={src_start}) — "
                f"peak: {best_off_f:+d}f ({best_off_ms:+.1f}ms) "
                f"score={sc[bp]:.4f}"
            )

            for delta in range(-15, 16):
                pos = bp + delta
                if 0 <= pos < len(sc):
                    off_f = (tgt_ws + pos) - src_start - pts_delta_frames + ldiv
                    off_ms = off_f * src_frame_dur_ms
                    marker = " ★" if delta == 0 else ""
                    bar_val = max(0, (sc[pos] - 0.3) * 60)
                    bar = "█" * int(bar_val)
                    lines.append(
                        f"    {off_f:+4d}f ({off_ms:+7.1f}ms): "
                        f"{sc[pos]:.4f} {bar}{marker}"
                    )

        report_path.write_text("\n".join(lines), encoding="utf-8")
        log(f"[SlidingMatch] Debug report saved: {report_path}")
    except Exception as e:
        log(f"[SlidingMatch] WARNING: Failed to write debug report: {e}")
