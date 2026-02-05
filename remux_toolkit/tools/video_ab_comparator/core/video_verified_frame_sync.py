# remux_toolkit/tools/video_ab_comparator/core/video_verified_frame_sync.py
"""
Video-Verified Frame Sync for Video A/B Comparator.

Implements frame-perfect alignment using the Video-Sync-GUI methodology:

1. Use audio correlation to get rough offset estimate (ballpark)
2. Generate candidate frame offsets around the correlation value
3. At multiple checkpoints, verify candidates using strict position matching
4. Require N consecutive frames to match at exact positions (no window search)
5. Select candidate with most sequence-verified checkpoints

This approach prevents false positives by requiring consecutive frames to match
at their expected positions, rather than searching for similar content.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Callable
from pathlib import Path
import numpy as np
from PIL import Image

from .audio_correlation_frame_sync import (
    VideoReader,
    detect_video_fps,
    get_video_duration,
    time_to_frame_floor,
    frame_to_time_floor,
)


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class SequenceVerifyResult:
    """Result of verifying a sequence of consecutive frames."""
    matched_count: int
    total_count: int
    avg_distance: float
    distances: List[float]
    verified: bool  # True if >= threshold % matched


@dataclass
class CheckpointQuality:
    """Quality measurement at a single checkpoint."""
    checkpoint_idx: int
    checkpoint_time_sec: float
    source_frame_idx: int
    target_frame_idx: int
    sequence_verified: bool
    sequence_matched: int
    sequence_total: int
    avg_distance: float


@dataclass
class CandidateResult:
    """Result of testing a candidate frame offset."""
    frame_offset: int
    offset_ms: float
    checkpoints_verified: int
    checkpoints_total: int
    total_score: float
    avg_distance: float
    checkpoint_details: List[CheckpointQuality]


@dataclass
class VideoVerifiedResult:
    """Final result of video-verified frame sync."""
    success: bool
    offset_ms: float
    offset_frames: int
    confidence: float
    method: str  # 'video-verified' or 'audio-fallback'
    best_candidate: Optional[CandidateResult]
    all_candidates: List[CandidateResult]
    error: Optional[str] = None


# ============================================================================
# Comparison Methods
# ============================================================================

def compute_frame_hash(
    frame: Image.Image,
    hash_size: int = 8,
    algorithm: str = 'dhash'
) -> Any:
    """
    Compute perceptual hash of a frame.

    Args:
        frame: PIL Image (grayscale)
        hash_size: Hash size (8 = 64-bit, 16 = 256-bit)
        algorithm: 'dhash', 'phash', 'average_hash', 'whash'

    Returns:
        ImageHash object or None on failure
    """
    try:
        import imagehash

        if algorithm == 'dhash':
            return imagehash.dhash(frame, hash_size=hash_size)
        elif algorithm == 'phash':
            return imagehash.phash(frame, hash_size=hash_size)
        elif algorithm == 'average_hash':
            return imagehash.average_hash(frame, hash_size=hash_size)
        elif algorithm == 'whash':
            return imagehash.whash(frame, hash_size=hash_size)
        else:
            # Default to dhash
            return imagehash.dhash(frame, hash_size=hash_size)

    except ImportError:
        print("[VideoVerified] ERROR: imagehash not installed")
        return None
    except Exception as e:
        print(f"[VideoVerified] Hash computation failed: {e}")
        return None


def compute_ssim(frame1: Image.Image, frame2: Image.Image) -> float:
    """
    Compute Structural Similarity Index between two frames.

    Args:
        frame1: First PIL Image (grayscale)
        frame2: Second PIL Image (grayscale)

    Returns:
        SSIM value (0-1, higher = more similar)
    """
    try:
        from skimage.metrics import structural_similarity as ssim

        # Convert to numpy arrays
        arr1 = np.array(frame1)
        arr2 = np.array(frame2)

        # Resize if dimensions don't match
        if arr1.shape != arr2.shape:
            # Resize arr2 to match arr1
            frame2_resized = frame2.resize(frame1.size, Image.Resampling.LANCZOS)
            arr2 = np.array(frame2_resized)

        # Compute SSIM
        score = ssim(arr1, arr2)
        return float(score)

    except ImportError:
        print("[VideoVerified] ERROR: scikit-image not installed for SSIM")
        return 0.0
    except Exception as e:
        print(f"[VideoVerified] SSIM computation failed: {e}")
        return 0.0


def compare_frames(
    frame1: Image.Image,
    frame2: Image.Image,
    method: str = 'hash',
    hash_algorithm: str = 'dhash',
    hash_size: int = 8,
    hash_threshold: int = 5
) -> tuple[float, bool]:
    """
    Compare two frames using specified method.

    Args:
        frame1: First frame
        frame2: Second frame
        method: 'hash' or 'ssim'
        hash_algorithm: Hash algorithm if method='hash'
        hash_size: Hash size if method='hash'
        hash_threshold: Max hamming distance to consider match

    Returns:
        (distance, is_match) - distance and whether frames match
        For hash: distance is hamming distance (lower = more similar)
        For ssim: distance is 1-ssim (lower = more similar)
    """
    if method == 'ssim':
        ssim_score = compute_ssim(frame1, frame2)
        distance = 1.0 - ssim_score  # Convert to distance (lower = better)
        is_match = ssim_score >= 0.85  # 85% similarity threshold for SSIM
        return distance, is_match
    else:
        # Hash-based comparison
        hash1 = compute_frame_hash(frame1, hash_size, hash_algorithm)
        hash2 = compute_frame_hash(frame2, hash_size, hash_algorithm)

        if hash1 is None or hash2 is None:
            return float('inf'), False

        distance = float(hash1 - hash2)  # Hamming distance
        is_match = distance <= hash_threshold
        return distance, is_match


# ============================================================================
# Checkpoint Selection
# ============================================================================

def select_checkpoints_video_verified(
    duration_sec: float,
    num_checkpoints: int = 5
) -> List[float]:
    """
    Select checkpoint times at strategic positions.

    Uses 15%, 30%, 50%, 70%, 85% positions (matching Video-Sync-GUI).

    Args:
        duration_sec: Video duration in seconds
        num_checkpoints: Number of checkpoints (default: 5)

    Returns:
        List of checkpoint times in seconds
    """
    # Use percentage-based positions (matching Video-Sync-GUI)
    # Video-Sync-GUI uses: positions = [15, 30, 50, 70, 85][:num_checkpoints]
    # and calculates: time_ms = duration_ms * pos / 100
    if num_checkpoints == 5:
        positions = [15, 30, 50, 70, 85]
    elif num_checkpoints == 3:
        positions = [20, 50, 80]
    else:
        # Evenly distributed
        positions = [int(100 * (i + 1) / (num_checkpoints + 1)) for i in range(num_checkpoints)]

    # Calculate checkpoint times as percentage of total duration
    checkpoints = [duration_sec * pos / 100.0 for pos in positions]
    return checkpoints


# ============================================================================
# Candidate Generation
# ============================================================================

def generate_frame_candidates(
    correlation_frames: float,
    search_range: int = 3
) -> List[int]:
    """
    Generate candidate frame offsets around the audio correlation value.

    Always includes 0 as a fallback candidate.

    Args:
        correlation_frames: Frame offset from audio correlation
        search_range: Search ±N frames around correlation

    Returns:
        List of integer frame offsets to test
    """
    center = round(correlation_frames)

    # Generate range around center
    candidates = list(range(center - search_range, center + search_range + 1))

    # Always include 0 if not already present
    if 0 not in candidates:
        candidates.append(0)

    # Sort by distance from correlation (test most likely first)
    candidates.sort(key=lambda x: abs(x - correlation_frames))

    return candidates


# ============================================================================
# Sequence Verification (Core Algorithm)
# ============================================================================

def verify_frame_sequence(
    source_reader: VideoReader,
    target_reader: VideoReader,
    source_start_idx: int,
    target_start_idx: int,
    sequence_length: int = 10,
    comparison_method: str = 'hash',
    hash_algorithm: str = 'dhash',
    hash_size: int = 8,
    hash_threshold: int = 5,
    match_threshold_pct: float = 70.0
) -> SequenceVerifyResult:
    """
    Verify that N consecutive frames match at exact positions.

    This is the KEY algorithm: if the offset is correct, then
    source[N], source[N+1], ... should match target[N+offset], target[N+offset+1], ...
    at their EXACT positions (no window search).

    Args:
        source_reader: VideoReader for source video
        target_reader: VideoReader for target video
        source_start_idx: Starting frame index in source
        target_start_idx: Starting frame index in target (= source_start + offset)
        sequence_length: Number of consecutive frames to verify
        comparison_method: 'hash' or 'ssim'
        hash_algorithm: Hash algorithm if using hash method
        hash_size: Hash size
        hash_threshold: Max hamming distance per frame
        match_threshold_pct: Percentage of frames that must match

    Returns:
        SequenceVerifyResult with verification details
    """
    matched = 0
    distances = []

    for i in range(sequence_length):
        source_idx = source_start_idx + i
        target_idx = target_start_idx + i

        # Get frames at exact positions
        source_frame = source_reader.get_frame_at_index(source_idx)
        target_frame = target_reader.get_frame_at_index(target_idx)

        if source_frame is None or target_frame is None:
            distances.append(float('inf'))
            continue

        # Debug: show first frame comparison details
        if i == 0:
            import numpy as np
            src_arr = np.array(source_frame)
            tgt_arr = np.array(target_frame)
            # Show mean pixel values to verify frames are different
            print(f"[FrameDebug] Source frame {source_idx}: size={source_frame.size}, mean={src_arr.mean():.1f}")
            print(f"[FrameDebug] Target frame {target_idx}: size={target_frame.size}, mean={tgt_arr.mean():.1f}")

        # Compare frames
        distance, is_match = compare_frames(
            source_frame, target_frame,
            method=comparison_method,
            hash_algorithm=hash_algorithm,
            hash_size=hash_size,
            hash_threshold=hash_threshold
        )

        # Debug: show first comparison result
        if i == 0:
            print(f"[FrameDebug] Distance={distance}, is_match={is_match}")

        distances.append(distance)
        if is_match:
            matched += 1

    # Calculate if sequence is verified
    valid_count = len([d for d in distances if d != float('inf')])
    if valid_count == 0:
        return SequenceVerifyResult(
            matched_count=0,
            total_count=sequence_length,
            avg_distance=float('inf'),
            distances=distances,
            verified=False
        )

    min_matches = int(valid_count * match_threshold_pct / 100.0)
    verified = matched >= min_matches

    avg_dist = sum(d for d in distances if d != float('inf')) / valid_count

    return SequenceVerifyResult(
        matched_count=matched,
        total_count=valid_count,
        avg_distance=avg_dist,
        distances=distances,
        verified=verified
    )


# ============================================================================
# Candidate Quality Measurement
# ============================================================================

def measure_frame_offset_quality(
    frame_offset: int,
    checkpoint_times: List[float],
    source_reader: VideoReader,
    target_reader: VideoReader,
    fps: float,
    sequence_length: int = 10,
    comparison_method: str = 'hash',
    hash_algorithm: str = 'dhash',
    hash_size: int = 8,
    hash_threshold: int = 5,
    match_threshold_pct: float = 70.0,
    log: Optional[Callable] = None
) -> CandidateResult:
    """
    Measure quality of a candidate frame offset at all checkpoints.

    For each checkpoint:
    1. Calculate source frame index from checkpoint time
    2. Calculate target frame index = source + offset
    3. Verify sequence of consecutive frames at exact positions
    4. Track how many checkpoints have verified sequences

    Args:
        frame_offset: Candidate frame offset to test
        checkpoint_times: List of checkpoint times in seconds
        source_reader: VideoReader for source
        target_reader: VideoReader for target
        fps: Frame rate
        sequence_length: Consecutive frames to verify per checkpoint
        comparison_method: 'hash' or 'ssim'
        hash_algorithm: Hash algorithm
        hash_size: Hash size
        hash_threshold: Hash threshold
        match_threshold_pct: Percentage threshold for verification
        log: Optional logging function

    Returns:
        CandidateResult with quality metrics
    """
    frame_duration_ms = 1000.0 / fps
    checkpoint_details = []
    total_score = 0.0
    total_distance = 0.0
    verified_count = 0

    for idx, checkpoint_sec in enumerate(checkpoint_times):
        checkpoint_ms = checkpoint_sec * 1000.0
        source_frame_idx = time_to_frame_floor(checkpoint_ms, fps)
        # Convention: target_frame = source_frame + frame_offset
        # (matches Video-Sync-GUI: positive offset means target is ahead)
        target_frame_idx = source_frame_idx + frame_offset

        # Debug: show frame indices for first checkpoint of each candidate
        if idx == 0 and log:
            log(f"    Checkpoint 1: source_frame={source_frame_idx}, target_frame={target_frame_idx}")

        # Skip if target would be negative
        if target_frame_idx < 0:
            continue

        # Verify sequence at this checkpoint
        result = verify_frame_sequence(
            source_reader,
            target_reader,
            source_frame_idx,
            target_frame_idx,
            sequence_length,
            comparison_method,
            hash_algorithm,
            hash_size,
            hash_threshold,
            match_threshold_pct
        )

        checkpoint_quality = CheckpointQuality(
            checkpoint_idx=idx,
            checkpoint_time_sec=checkpoint_sec,
            source_frame_idx=source_frame_idx,
            target_frame_idx=target_frame_idx,
            sequence_verified=result.verified,
            sequence_matched=result.matched_count,
            sequence_total=result.total_count,
            avg_distance=result.avg_distance
        )
        checkpoint_details.append(checkpoint_quality)

        if result.verified:
            verified_count += 1
            # Score: 2.0 for verified, scaled by match ratio
            total_score += 2.0 * (result.matched_count / result.total_count)
        else:
            # Partial score for partial matches
            total_score += 0.3 * (result.matched_count / max(result.total_count, 1))

        if result.avg_distance != float('inf'):
            total_distance += result.avg_distance

    # Calculate average distance
    valid_checkpoints = len([c for c in checkpoint_details if c.avg_distance != float('inf')])
    avg_distance = total_distance / valid_checkpoints if valid_checkpoints > 0 else float('inf')

    offset_ms = frame_offset * frame_duration_ms

    if log:
        log(f"  Offset {frame_offset:+d} frames ({offset_ms:+.1f}ms): "
            f"{verified_count}/{len(checkpoint_details)} verified, "
            f"score={total_score:.2f}, avg_dist={avg_distance:.2f}")

    return CandidateResult(
        frame_offset=frame_offset,
        offset_ms=offset_ms,
        checkpoints_verified=verified_count,
        checkpoints_total=len(checkpoint_details),
        total_score=total_score,
        avg_distance=avg_distance,
        checkpoint_details=checkpoint_details
    )


# ============================================================================
# Main Entry Point
# ============================================================================

def video_verified_frame_sync(
    source_a_path: str,
    source_b_path: str,
    audio_offset_ms: float,
    fps_a: Optional[float] = None,
    fps_b: Optional[float] = None,
    num_checkpoints: int = 5,
    sequence_length: int = 10,
    candidate_range: int = 3,
    comparison_method: str = 'hash',
    hash_algorithm: str = 'dhash',
    hash_size: int = 8,
    hash_threshold: int = 5,
    match_threshold_pct: float = 70.0,
    temp_dir: Optional[Path] = None,
    progress_callback: Optional[Callable] = None
) -> VideoVerifiedResult:
    """
    Video-verified frame synchronization.

    Uses strict consecutive frame matching to find precise frame alignment.

    Algorithm:
    1. Convert audio correlation to frame offset estimate
    2. Generate candidate frame offsets (±N frames around estimate)
    3. For each candidate, verify at multiple checkpoints
    4. At each checkpoint, verify N consecutive frames match at exact positions
    5. Select candidate with most verified checkpoints
    6. Fall back to audio correlation if no candidate passes

    Args:
        source_a_path: Path to source A video
        source_b_path: Path to source B video
        audio_offset_ms: Audio correlation offset in milliseconds
        fps_a: Source A frame rate (auto-detected if None)
        fps_b: Source B frame rate (auto-detected if None)
        num_checkpoints: Number of verification checkpoints (default: 5)
        sequence_length: Consecutive frames to verify (default: 10)
        candidate_range: Search ±N frames around correlation (default: 3)
        comparison_method: 'hash' or 'ssim' (default: 'hash')
        hash_algorithm: 'dhash', 'phash', 'average_hash', 'whash' (default: 'dhash')
        hash_size: Hash size (default: 8)
        hash_threshold: Max hamming distance (default: 5)
        match_threshold_pct: Percentage of sequence that must match (default: 70.0)
        temp_dir: Optional temp directory for index caching
        progress_callback: Optional progress callback(message, progress_pct)

    Returns:
        VideoVerifiedResult with alignment details
    """
    def log(msg: str):
        print(f"[VideoVerified] {msg}")

    log("=" * 60)
    log("Video-Verified Frame Sync")
    log("=" * 60)
    log(f"Source A: {Path(source_a_path).name}")
    log(f"  Full path: {source_a_path}")
    log(f"Source B: {Path(source_b_path).name}")
    log(f"  Full path: {source_b_path}")
    log(f"Audio offset: {audio_offset_ms:.3f}ms")

    if progress_callback:
        progress_callback("Initializing video readers...", 5)

    # Detect FPS
    if fps_a is None:
        fps_a = detect_video_fps(source_a_path)
    if fps_b is None:
        fps_b = detect_video_fps(source_b_path)

    # Use source A fps for frame calculations (assuming same fps)
    fps = fps_a
    frame_duration_ms = 1000.0 / fps

    log(f"FPS: A={fps_a:.3f}, B={fps_b:.3f}")
    log(f"Frame duration: {frame_duration_ms:.3f}ms")

    # Get duration
    duration_a = get_video_duration(source_a_path)
    if duration_a == 0.0:
        return VideoVerifiedResult(
            success=False,
            offset_ms=audio_offset_ms,
            offset_frames=round(audio_offset_ms / frame_duration_ms),
            confidence=0.0,
            method='audio-fallback',
            best_candidate=None,
            all_candidates=[],
            error='Failed to detect video duration'
        )

    log(f"Duration: {duration_a:.1f}s ({duration_a/60:.1f} min)")

    # Convert audio offset to frames
    correlation_frames = audio_offset_ms / frame_duration_ms
    log(f"Audio correlation: {correlation_frames:.2f} frames")

    # Generate candidates
    candidates = generate_frame_candidates(correlation_frames, candidate_range)
    log(f"Testing {len(candidates)} candidates: {candidates}")

    # Select checkpoints
    checkpoint_times = select_checkpoints_video_verified(duration_a, num_checkpoints)
    log(f"Checkpoints: {[f'{t:.1f}s' for t in checkpoint_times]}")

    if progress_callback:
        progress_callback("Opening video files...", 10)

    # Open video readers
    try:
        source_a_reader = VideoReader(source_a_path, temp_dir)
        source_b_reader = VideoReader(source_b_path, temp_dir)
    except Exception as e:
        return VideoVerifiedResult(
            success=False,
            offset_ms=audio_offset_ms,
            offset_frames=round(audio_offset_ms / frame_duration_ms),
            confidence=0.0,
            method='audio-fallback',
            best_candidate=None,
            all_candidates=[],
            error=f'Failed to open videos: {e}'
        )

    # Test each candidate
    log("")
    log("Testing candidates...")
    candidate_results = []

    for i, frame_offset in enumerate(candidates):
        if progress_callback:
            progress = 15 + int(70 * (i / len(candidates)))
            progress_callback(f"Testing offset {frame_offset:+d} frames...", progress)

        result = measure_frame_offset_quality(
            frame_offset,
            checkpoint_times,
            source_a_reader,
            source_b_reader,
            fps,
            sequence_length,
            comparison_method,
            hash_algorithm,
            hash_size,
            hash_threshold,
            match_threshold_pct,
            log
        )
        candidate_results.append(result)

    # Clean up
    source_a_reader.close()
    source_b_reader.close()

    if progress_callback:
        progress_callback("Selecting best candidate...", 90)

    # Select best candidate
    # Priority: verified count > score > lower distance
    candidate_results.sort(
        key=lambda r: (r.checkpoints_verified, r.total_score, -r.avg_distance),
        reverse=True
    )

    best = candidate_results[0]

    log("")
    log("Results:")
    for r in candidate_results:
        marker = " <-- BEST" if r == best else ""
        log(f"  {r.frame_offset:+d} frames: {r.checkpoints_verified}/{r.checkpoints_total} verified, "
            f"score={r.total_score:.2f}{marker}")

    # Require at least 1 verified checkpoint
    if best.checkpoints_verified == 0:
        log("")
        log("WARNING: No candidates passed verification, falling back to audio correlation")

        return VideoVerifiedResult(
            success=False,
            offset_ms=audio_offset_ms,
            offset_frames=round(audio_offset_ms / frame_duration_ms),
            confidence=0.3,
            method='audio-fallback',
            best_candidate=best,
            all_candidates=candidate_results,
            error='No candidates passed frame verification'
        )

    # Calculate confidence based on verified checkpoints
    confidence = best.checkpoints_verified / best.checkpoints_total

    log("")
    log(f"SELECTED: {best.frame_offset:+d} frames ({best.offset_ms:+.3f}ms)")
    log(f"Verified: {best.checkpoints_verified}/{best.checkpoints_total} checkpoints")
    log(f"Confidence: {confidence:.1%}")

    if progress_callback:
        progress_callback("Frame sync complete", 100)

    return VideoVerifiedResult(
        success=True,
        offset_ms=best.offset_ms,
        offset_frames=best.frame_offset,
        confidence=confidence,
        method='video-verified',
        best_candidate=best,
        all_candidates=candidate_results,
        error=None
    )
