# remux_toolkit/tools/video_ab_comparator/core/neural_matcher.py
"""
Neural feature sequence sliding for video alignment.

Uses ISC (Image Similarity Challenge) features to find the correct
frame offset between two video sources by sliding a sequence of
feature vectors from one source across the other and finding the
position with highest cumulative cosine similarity.

This is fundamentally different from classic per-frame hash matching:
- Classic: compare individual frames at fixed checkpoints using perceptual hashes
- Neural: compare SEQUENCES of frames, slide across a window using 256-dim embeddings

The sequence approach works because even though individual frames
in static scenes are nearly identical at any offset, the *sequence
of transitions* between frames is unique and provides a strong signal.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

import numpy as np


def calculate_neural_verified_offset(
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
    temp_dir: Optional[Path] = None,
    progress_callback=None,
) -> dict[str, Any]:
    """
    Calculate video-verified offset using ISC neural feature sequence sliding.

    Args:
        source_a_path: Path to source A video
        source_b_path: Path to source B video
        audio_offset_ms: Audio correlation offset in milliseconds
        fps_a: Source A frame rate
        fps_b: Source B frame rate
        duration_sec: Video duration in seconds
        num_positions: Number of test positions (default: 3 at 20%, 50%, 80%)
        window_seconds: Duration of frame window per position
        slide_range_seconds: ±N seconds sliding range around audio offset
        batch_size: GPU batch size for feature extraction
        temp_dir: Temp directory for FFMS2 index caching
        progress_callback: Optional callback(message, progress_pct)

    Returns:
        Dict with: success, offset_ms, offset_frames, confidence, method, error, details
    """
    def log(msg: str):
        print(msg)

    log("[NeuralMatch] === Neural Feature Matching ===")
    log(f"[NeuralMatch] Source A: {Path(source_a_path).name}")
    log(f"[NeuralMatch] Source B: {Path(source_b_path).name}")
    log(f"[NeuralMatch] Audio offset: {audio_offset_ms:+.3f}ms")

    if progress_callback:
        progress_callback("Loading neural matcher...", 92)

    # Check dependencies
    try:
        import vapoursynth as vs
    except ImportError as e:
        log(f"[NeuralMatch] VapourSynth not available: {e}")
        return _fallback_result(audio_offset_ms, fps_a, f"VapourSynth unavailable: {e}")

    try:
        import torch
    except ImportError as e:
        log(f"[NeuralMatch] PyTorch not available: {e}")
        return _fallback_result(audio_offset_ms, fps_a, f"PyTorch unavailable: {e}")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Open clips
    try:
        src_yuv, src_rgb = _open_clip(source_a_path, vs, temp_dir)
        tgt_yuv, tgt_rgb = _open_clip(source_b_path, vs, temp_dir)
    except Exception as e:
        log(f"[NeuralMatch] Failed to open videos: {e}")
        return _fallback_result(audio_offset_ms, fps_a, f"Failed to open videos: {e}")

    src_fps = src_yuv.fps.numerator / src_yuv.fps.denominator
    tgt_fps = tgt_yuv.fps.numerator / tgt_yuv.fps.denominator
    src_frame_dur_ms = 1000.0 / src_fps

    log(f"[NeuralMatch] Source: {src_yuv.num_frames}f @ {src_fps:.3f}fps")
    log(f"[NeuralMatch] Target: {tgt_yuv.num_frames}f @ {tgt_fps:.3f}fps")

    # Check FPS compatibility
    fps_ratio = max(src_fps, tgt_fps) / min(src_fps, tgt_fps)
    if fps_ratio > 1.01:
        log(f"[NeuralMatch] FPS mismatch ({src_fps:.3f} vs {tgt_fps:.3f}), falling back")
        return _fallback_result(audio_offset_ms, fps_a, "FPS mismatch")

    # Load ISC model
    t_model_start = time.time()
    try:
        from .isc_model import create_isc_model

        model, preprocessor = create_isc_model(device=str(device), log=log)
    except Exception as e:
        log(f"[NeuralMatch] Failed to load ISC model: {e}")
        return _fallback_result(audio_offset_ms, fps_a, f"ISC model failed: {e}")

    t_model = time.time() - t_model_start

    # Calculate frame counts for window and slide
    src_n_frames = int(window_seconds * src_fps)
    slide_pad = int(slide_range_seconds * tgt_fps)

    log(f"[NeuralMatch] Window: {src_n_frames} frames ({window_seconds}s)")
    log(f"[NeuralMatch] Slide: ±{slide_pad} frames (±{slide_range_seconds}s)")
    log(f"[NeuralMatch] Positions: {num_positions}, Batch: {batch_size}")
    log(f"[NeuralMatch] Model load: {t_model:.1f}s")

    # Select test positions (evenly across 10%-90%)
    positions_pct = [10 + 80 * (i + 0.5) / num_positions for i in range(num_positions)]

    log(f"[NeuralMatch] ─────────────────────────────────────")

    # Run sliding at each position
    results = []
    t_total_start = time.time()

    for i, pct in enumerate(positions_pct):
        if progress_callback:
            progress = 93 + int(5 * (i / num_positions))
            progress_callback(f"Neural matching position {i+1}/{num_positions}...", progress)

        t_pos_start = time.time()

        # Source frame range
        src_start = int(src_rgb.num_frames * pct / 100.0)
        src_end = min(src_start + src_n_frames, src_rgb.num_frames)
        src_frames = list(range(src_start, src_end))

        # Target frame range (padded for sliding)
        tgt_center = src_start
        tgt_window_start = max(0, tgt_center - slide_pad)
        tgt_window_end = min(tgt_rgb.num_frames, tgt_center + src_n_frames + slide_pad)
        tgt_frames = list(range(tgt_window_start, tgt_window_end))

        if len(tgt_frames) <= len(src_frames):
            log(f"[NeuralMatch]   [{i+1}/{num_positions}] {pct:.0f}% — SKIPPED (edge)")
            continue

        # Extract features
        src_feats = _extract_features_batch(
            src_rgb, src_frames, model, device, batch_size, torch
        )
        tgt_feats = _extract_features_batch(
            tgt_rgb, tgt_frames, model, device, batch_size, torch
        )

        # Slide and score
        scores, match_counts = _slide_and_score(src_feats, tgt_feats)

        if len(scores) == 0:
            log(f"[NeuralMatch]   [{i+1}/{num_positions}] {pct:.0f}% — SKIPPED (no slides)")
            continue

        best_pos = int(np.argmax(scores))
        offset_frames = (tgt_window_start + best_pos) - src_start
        offset_ms = offset_frames * src_frame_dur_ms

        gradient = _compute_gradient(scores, best_pos)
        dt = time.time() - t_pos_start

        result = {
            "position_pct": pct,
            "src_start": src_start,
            "offset_frames": offset_frames,
            "offset_ms": offset_ms,
            "score": float(scores[best_pos]),
            "matches": int(match_counts[best_pos]),
            "total": len(src_frames),
            "gradient": gradient,
            "time_s": dt,
        }
        results.append(result)

        log(
            f"[NeuralMatch]   [{i+1}/{num_positions}] {pct:.0f}% @{src_start}f → "
            f"offset={offset_frames:+d}f ({offset_ms:+.1f}ms) "
            f"score={scores[best_pos]:.4f} "
            f"match={int(match_counts[best_pos])}/{len(src_frames)} "
            f"grad={gradient:.4f}/f ({dt:.1f}s)"
        )

    dt_total = time.time() - t_total_start

    # Cleanup GPU
    from .gpu_backend import cleanup_gpu
    cleanup_gpu()

    if not results:
        log("[NeuralMatch] No valid positions — falling back to audio correlation")
        return _fallback_result(audio_offset_ms, fps_a, "No valid positions")

    # Consensus
    offsets_f = [r["offset_frames"] for r in results]
    scores_list = [r["score"] for r in results]
    consensus = Counter(offsets_f).most_common(1)[0]
    consensus_frames = consensus[0]
    consensus_count = consensus[1]
    consensus_ms = consensus_frames * src_frame_dur_ms

    # Confidence assessment
    consensus_ratio = consensus_count / len(results)
    mean_score = float(np.mean(scores_list))

    if consensus_ratio >= 0.9 and mean_score >= 0.98:
        confidence = "HIGH"
    elif consensus_ratio >= 0.7 and mean_score >= 0.95:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    # Results summary
    log(f"[NeuralMatch] ═══════════════════════════════════════")
    log(f"[NeuralMatch] RESULTS SUMMARY")
    log(f"[NeuralMatch] ═══════════════════════════════════════")
    log(
        f"[NeuralMatch] Consensus: {consensus_frames:+d}f = {consensus_ms:+.1f}ms "
        f"({consensus_count}/{len(results)} positions)"
    )
    log(f"[NeuralMatch] Mean score: {mean_score:.4f}")
    log(f"[NeuralMatch] Confidence: {confidence}")
    log(f"[NeuralMatch] Audio correlation: {audio_offset_ms:+.3f}ms")

    diff_ms = consensus_ms - audio_offset_ms
    diff_frames = diff_ms / src_frame_dur_ms
    log(f"[NeuralMatch] Diff from audio: {diff_ms:+.1f}ms ({diff_frames:+.1f} frames)")

    if abs(diff_ms) > src_frame_dur_ms / 2:
        log(f"[NeuralMatch] VIDEO OFFSET DIFFERS FROM AUDIO CORRELATION")

    log(f"[NeuralMatch] Total time: {dt_total:.1f}s")
    log(f"[NeuralMatch] ═══════════════════════════════════════")

    confidence_float = {"HIGH": 0.95, "MEDIUM": 0.75, "LOW": 0.4}.get(confidence, 0.4)

    return {
        "success": True,
        "offset_ms": consensus_ms,
        "offset_frames": consensus_frames,
        "confidence": confidence_float,
        "confidence_label": confidence,
        "method": "neural-isc",
        "error": None,
        "consensus_count": consensus_count,
        "num_positions": len(results),
        "mean_score": mean_score,
        "total_time_s": dt_total,
        "per_position_results": results,
    }


# ─── Internal helpers ─────────────────────────────────────────────────


def _fallback_result(audio_offset_ms: float, fps: float, error: str) -> dict:
    """Return a fallback result using audio correlation."""
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


def _open_clip(video_path: str, vs, temp_dir: Path | None = None):
    """Open a video with VapourSynth/FFMS2 and return (yuv_clip, rgb_clip)."""
    core = vs.core

    cache_path = str(_get_ffms2_cache_path(video_path, temp_dir))

    try:
        clip = core.ffms2.Source(source=video_path, cachefile=cache_path)
    except Exception:
        # Delete stale index and retry
        stale = Path(cache_path)
        if stale.exists():
            stale.unlink(missing_ok=True)
        clip = core.ffms2.Source(source=video_path, cachefile=cache_path)

    rgb_clip = core.resize.Bicubic(clip, format=vs.RGB24, matrix_in_s="170m")
    return clip, rgb_clip


def _get_ffms2_cache_path(video_path: str, temp_dir: Path | None = None) -> Path:
    """Generate cache path for FFMS2 index (same logic as VideoReader)."""
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


def _frame_to_tensor(frame, device, F):
    """Convert a VapourSynth RGB24 frame to a normalized GPU tensor for ISC."""
    import torch

    r = np.asarray(frame[0])
    g = np.asarray(frame[1])
    b = np.asarray(frame[2])
    rgb_np = np.stack([r, g, b], axis=0).astype(np.float32) / 255.0
    tensor = torch.from_numpy(rgb_np).unsqueeze(0).to(device)
    resized = F.interpolate(
        tensor, size=(512, 512), mode="bilinear", align_corners=False
    )
    mean = torch.tensor([0.485, 0.456, 0.406], device=device).view(1, 3, 1, 1)
    std = torch.tensor([0.229, 0.224, 0.225], device=device).view(1, 3, 1, 1)
    normalized = (resized - mean) / std
    return normalized.squeeze(0)


def _extract_features_batch(rgb_clip, frame_nums, model, device, batch_size, torch):
    """Extract ISC features for a list of frame numbers using GPU batch processing.

    Uses clip.frames() for contiguous frame ranges (faster I/O).
    """
    import torch.nn.functional as F

    all_feats = []
    batch_tensors = []

    first_frame = frame_nums[0]
    last_frame = frame_nums[-1]
    is_contiguous = frame_nums == list(range(first_frame, last_frame + 1))

    with torch.no_grad():
        if is_contiguous:
            # Fast path: use clip.frames() for contiguous ranges
            trimmed = rgb_clip[first_frame : last_frame + 1]
            for i, frame in enumerate(trimmed.frames()):
                tensor = _frame_to_tensor(frame, device, F)
                batch_tensors.append(tensor)

                if len(batch_tensors) == batch_size or i == len(frame_nums) - 1:
                    batch = torch.stack(batch_tensors).to(device)
                    feats = model(batch)
                    all_feats.append(feats.cpu())
                    batch_tensors = []
        else:
            # Slow path: random access
            for fn in frame_nums:
                fn_clamped = max(0, min(fn, rgb_clip.num_frames - 1))
                frame = rgb_clip.get_frame(fn_clamped)
                tensor = _frame_to_tensor(frame, device, F)
                batch_tensors.append(tensor)

                if len(batch_tensors) == batch_size or fn == frame_nums[-1]:
                    batch = torch.stack(batch_tensors).to(device)
                    feats = model(batch)
                    all_feats.append(feats.cpu())
                    batch_tensors = []

    return torch.cat(all_feats, dim=0).numpy()


def _slide_and_score(src_feats: np.ndarray, tgt_feats: np.ndarray):
    """Slide source features across target and compute cosine similarity.

    Returns:
        scores: mean cosine similarity at each slide position
        match_counts: number of frame pairs with similarity > 0.5
    """
    S = len(src_feats)
    T = len(tgt_feats)
    max_slides = T - S + 1
    if max_slides <= 0:
        return np.array([]), np.array([])

    # L2 normalize
    src_norm = src_feats / (np.linalg.norm(src_feats, axis=1, keepdims=True) + 1e-8)
    tgt_norm = tgt_feats / (np.linalg.norm(tgt_feats, axis=1, keepdims=True) + 1e-8)

    scores = np.zeros(max_slides)
    match_counts = np.zeros(max_slides, dtype=int)
    for p in range(max_slides):
        pair_sims = np.sum(src_norm * tgt_norm[p : p + S], axis=1)
        scores[p] = pair_sims.mean()
        match_counts[p] = np.sum(pair_sims > 0.5)

    return scores, match_counts


def _compute_gradient(scores: np.ndarray, best_pos: int) -> float:
    """Compute average score drop-off per frame from peak.

    Higher gradient = sharper peak = more confident result.
    """
    if len(scores) < 3:
        return 0.0

    peak_score = scores[best_pos]
    gradients = []

    for delta in range(1, 6):
        for sign in [-1, 1]:
            pos = best_pos + sign * delta
            if 0 <= pos < len(scores):
                drop = peak_score - scores[pos]
                gradients.append(drop / delta)

    return float(np.mean(gradients)) if gradients else 0.0
