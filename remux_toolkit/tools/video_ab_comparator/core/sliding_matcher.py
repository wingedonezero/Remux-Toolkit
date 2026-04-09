# remux_toolkit/tools/video_ab_comparator/core/sliding_matcher.py
"""
Sliding-window frame matching via GPU pHash.

Single-file port of the video_verified/pHash sliding pipeline from
Video-Sync-GUI. Ports:

- PTS-aware clip opener that reads ``_AbsoluteTime`` from frame 0 of
  both clips so files with non-zero container PTS origins don't
  derail the search.
- GPU DCT-II pHash descriptor extractor (no weights, no model
  download). Produces a 1024-bit (default ``hash_size=32``) unit-norm
  descriptor per frame using bilinear resize + 2D DCT + median
  threshold. Roughly 3x faster than ISC on GPU with sharper peaks.
- Cosine-similarity sliding scorer with the same ``compute_gradient``
  sharpness metric the rest of the codebase reports.
- ``calculate_sliding_offset()`` — main entry point, drop-in replacement
  for the old ISC ``calculate_neural_verified_offset``. Consensus
  across N positions, HIGH/MED/LOW confidence, per-position results
  dict, PTS metadata.

Note on PTS semantics in video_ab_comparator: the VSG matcher returns a
wall-clock offset (raw frame match minus ``pts_delta_frames``). That's
correct for subtitle timing, which is wall-clock. But ab_comparator
needs a *content*-index offset for FrameMapper (VapourSynth/FFMS2
``get_frame(n)`` is content-indexed), so this port returns the raw
frame-index match without the wall-clock subtraction. ``pts_delta_frames``
is still applied to the search center so the behavior exactly matches
VSG when both files have PTS=0 (the common case), and still finds the
correct content match when they don't.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

import numpy as np


# ── Public entry point ───────────────────────────────────────────────────────


def calculate_sliding_offset(
    source_a_path: str,
    source_b_path: str,
    audio_offset_ms: float,
    fps_a: float,
    fps_b: float,
    duration_sec: float,
    num_positions: int = 3,
    window_seconds: int = 10,
    slide_range_seconds: int = 5,
    batch_size: int = 32,
    hash_size: int = 32,
    temp_dir: Optional[Path] = None,
    progress_callback=None,
) -> dict[str, Any]:
    """Find the content frame offset between two video sources via GPU pHash.

    Parameters mirror the old neural matcher so callers don't need to
    change. ``hash_size`` is new (default 32 → 1024-bit descriptors).

    Returns a dict with: ``success``, ``offset_ms``, ``offset_frames``
    (content-domain), ``confidence``, ``confidence_label``, ``method``,
    ``error``, plus per-position results, consensus metadata, and PTS
    correction fields.
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
        return _fallback_result(audio_offset_ms, fps_a, f"VapourSynth unavailable: {e}")

    try:
        import torch
    except ImportError as e:
        log(f"[SlidingMatch] PyTorch not available: {e}")
        return _fallback_result(audio_offset_ms, fps_a, f"PyTorch unavailable: {e}")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # ── Open clips with PTS metadata ──────────────────────────────
    try:
        src_yuv, src_rgb, src_start_pts_s = _open_clip(source_a_path, vs, temp_dir)
        tgt_yuv, tgt_rgb, tgt_start_pts_s = _open_clip(source_b_path, vs, temp_dir)
    except Exception as e:
        log(f"[SlidingMatch] Failed to open videos: {e}")
        return _fallback_result(audio_offset_ms, fps_a, f"Failed to open videos: {e}")

    src_fps = src_yuv.fps.numerator / src_yuv.fps.denominator
    tgt_fps = tgt_yuv.fps.numerator / tgt_yuv.fps.denominator
    src_frame_dur_ms = 1000.0 / src_fps

    log(
        f"[SlidingMatch] Source: {src_yuv.num_frames}f @ {src_fps:.3f}fps  "
        f"start_pts={src_start_pts_s:+.6f}s"
    )
    log(
        f"[SlidingMatch] Target: {tgt_yuv.num_frames}f @ {tgt_fps:.3f}fps  "
        f"start_pts={tgt_start_pts_s:+.6f}s"
    )

    # ── PTS correction ────────────────────────────────────────────
    # Port of the VSG fix: if either file has a non-zero container PTS
    # origin, shift the target window center by the PTS delta (in
    # frames) so the sliding search is centered on the same region
    # VSG would pick. For the common case (both start_pts = 0) this is
    # a no-op and execution is identical to a PTS-unaware matcher.
    pts_delta_s = src_start_pts_s - tgt_start_pts_s
    pts_delta_frames = int(round(pts_delta_s * src_fps))
    pts_correction_applied = pts_delta_frames != 0

    if pts_correction_applied:
        log("[SlidingMatch] ─────────────────────────────────────")
        log("[SlidingMatch] PTS delta detected — shifting search center")
        log(
            f"[SlidingMatch]   Delta: {pts_delta_s:+.6f}s "
            f"= {pts_delta_frames:+d} frames"
        )
        log("[SlidingMatch] ─────────────────────────────────────")

    # ── FPS compatibility check ──────────────────────────────────
    fps_ratio = max(src_fps, tgt_fps) / min(src_fps, tgt_fps)
    if fps_ratio > 1.01:
        log(
            f"[SlidingMatch] FPS mismatch ({src_fps:.3f} vs {tgt_fps:.3f}), "
            f"ratio={fps_ratio:.4f} — falling back to audio"
        )
        return _fallback_result(audio_offset_ms, fps_a, "FPS mismatch")

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

        # Target window is centered at the PTS-shifted source index.
        # In the PTS=0 case this collapses to ``tgt_center = src_start``
        # which is what the pre-refactor ISC matcher did.
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
        # raw_offset_frames is the content-domain frame offset —
        # what FrameMapper in ab_comparator needs. We deliberately do
        # NOT subtract pts_delta_frames (which VSG does for sub
        # wall-clock correction) because FrameMapper / the detectors
        # compare frames by content index via VapourSynth's
        # content-indexed get_frame(n), not by wall-clock.
        raw_offset_frames = (tgt_window_start + best_pos) - src_start
        offset_ms = raw_offset_frames * src_frame_dur_ms

        gradient = _compute_gradient(scores, best_pos)
        dt = time.time() - t_pos_start

        results.append({
            "position_pct": pct,
            "src_start": src_start,
            "offset_frames": raw_offset_frames,
            "offset_ms": offset_ms,
            "score": float(scores[best_pos]),
            "matches": int(match_counts[best_pos]),
            "total": len(src_frames),
            "gradient": gradient,
            "time_s": dt,
        })

        log(
            f"[SlidingMatch]   [{i+1}/{num_positions}] {pct:.0f}% @{src_start}f → "
            f"offset={raw_offset_frames:+d}f ({offset_ms:+.1f}ms) "
            f"score={scores[best_pos]:.4f} "
            f"match={int(match_counts[best_pos])}/{len(src_frames)} "
            f"grad={gradient:.4f}/f ({dt:.1f}s)"
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
        return _fallback_result(audio_offset_ms, fps_a, "No valid positions")

    # ── Consensus + confidence ───────────────────────────────────
    offsets_f = [r["offset_frames"] for r in results]
    scores_list = [r["score"] for r in results]
    consensus = Counter(offsets_f).most_common(1)[0]
    consensus_frames = consensus[0]
    consensus_count = consensus[1]
    consensus_ms = consensus_frames * src_frame_dur_ms

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
        f"{consensus_ms:+.1f}ms ({consensus_count}/{len(results)})"
    )
    log(
        f"[SlidingMatch] Mean score: {mean_score:.4f}, "
        f"Range: [{min_score:.4f}, {max(scores_list):.4f}]"
    )
    log(f"[SlidingMatch] Mean gradient: {mean_gradient:.4f}/frame")
    log(f"[SlidingMatch] Confidence: {confidence_label}")
    log(f"[SlidingMatch] Audio offset:    {audio_offset_ms:+.3f}ms")
    log(f"[SlidingMatch] Sliding offset:  {consensus_ms:+.3f}ms")

    diff_ms = consensus_ms - audio_offset_ms
    diff_frames = diff_ms / src_frame_dur_ms
    log(f"[SlidingMatch] Diff from audio: {diff_ms:+.1f}ms ({diff_frames:+.1f}f)")
    if abs(diff_ms) > src_frame_dur_ms / 2:
        log("[SlidingMatch] SLIDING OFFSET DIFFERS FROM AUDIO CORRELATION")

    log(f"[SlidingMatch] Total time: {dt_total:.1f}s")
    log("[SlidingMatch] ═══════════════════════════════════════")

    confidence_float = {"HIGH": 0.95, "MEDIUM": 0.75, "LOW": 0.4}[confidence_label]

    return {
        "success": True,
        "offset_ms": consensus_ms,
        "offset_frames": consensus_frames,
        "confidence": confidence_float,
        "confidence_label": confidence_label,
        "method": "sliding-phash",
        "error": None,
        "consensus_count": consensus_count,
        "num_positions": len(results),
        "mean_score": mean_score,
        "min_score": min_score,
        "mean_gradient": mean_gradient,
        "source_fps": src_fps,
        "target_fps": tgt_fps,
        "total_time_s": dt_total,
        "per_position_results": results,
        "hash_size": hash_size,
        "descriptor_bits": hash_size * hash_size,
        # PTS metadata — useful for logs / debugging non-zero PTS files
        "pts_correction_applied": pts_correction_applied,
        "src_start_pts_s": src_start_pts_s,
        "tgt_start_pts_s": tgt_start_pts_s,
        "pts_delta_s": pts_delta_s,
        "pts_delta_frames": pts_delta_frames,
    }


# ── Fallback ──────────────────────────────────────────────────────────────────


def _fallback_result(audio_offset_ms: float, fps: float, error: str) -> dict:
    frame_dur_ms = 1000.0 / fps if fps > 0 else 41.708
    return {
        "success": False,
        "offset_ms": audio_offset_ms,
        "offset_frames": round(audio_offset_ms / frame_dur_ms),
        "confidence": 0.3,
        "confidence_label": "LOW",
        "method": "audio-fallback",
        "error": error,
    }


# ── Clip I/O with PTS metadata ───────────────────────────────────────────────


def _open_clip(video_path: str, vs, temp_dir: Optional[Path] = None):
    """Open a video and return ``(yuv_clip, rgb_clip, start_pts_s)``.

    ``start_pts_s`` is the wall-clock time of frame 0 from ffms2's
    ``_AbsoluteTime`` property. 0.0 for well-formed files, non-zero for
    DVDs / re-encodes that preserve a wall-clock offset in the
    container. The sliding matcher uses this to center its search on
    the same region VSG would pick.
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
        cache_dir = temp_dir / "ffindex"
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
    ``s = sqrt(hash_size**2)`` so every row has unit L2 norm (the
    cosine-slide harness expects unit descriptors — it skips
    re-normalization when rows are already unit length).
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
