# remux_toolkit/tools/video_ab_comparator/core/audio_correlation_frame_sync.py
"""
Audio-Correlation-Anchored Frame Sync for Video A/B Comparator.

This module adapts Video-Sync-GUI's subtitle-anchored-frame-snap methodology to use
audio correlation as anchors instead of subtitles. The approach:

1. Use audio correlation to get rough offset estimate
2. Select 3 checkpoints at 5%, 50%, 95% of video duration
3. For each checkpoint:
   - Extract reference frame + surrounding frames (±5 frame window)
   - Use perceptual hashing (dhash/phash)
   - Search in target video around audio-predicted position
   - Find exact frame match using sliding window
4. Verify checkpoint agreement
5. Calculate precise frame-level offset

This provides frame-perfect alignment by combining audio correlation's speed with
visual verification's accuracy.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict, Any
from pathlib import Path
import subprocess
import json
import math
import numpy as np
from PIL import Image


@dataclass
class CheckpointResult:
    """Result of frame matching at a single checkpoint."""
    checkpoint_idx: int
    checkpoint_time_sec: float
    source_center_frame: int
    target_matched_frame: int
    precise_offset_ms: float
    match_quality: float  # 0-1, based on hash distance
    matched_frames_count: int
    avg_hash_distance: float


@dataclass
class FrameSyncResult:
    """Result of audio-correlation frame sync."""
    success: bool
    offset_sec: float
    confidence: float
    checkpoints: List[CheckpointResult]
    agreement_status: str  # 'agree', 'disagree', 'insufficient'
    error: Optional[str] = None


# ============================================================================
# Frame Timing Utilities (CFR mode)
# ============================================================================

def time_to_frame_floor(time_ms: float, fps: float) -> int:
    """
    Convert timestamp to frame number (floor mode).

    Which frame is displaying at this time?
    Uses floor-based frame boundaries for consistency.

    Args:
        time_ms: Timestamp in milliseconds
        fps: Frame rate

    Returns:
        Frame number (0-indexed)
    """
    frame_duration_ms = 1000.0 / fps
    epsilon = 1e-6  # Protect against FP errors
    return int((time_ms + epsilon) / frame_duration_ms)


def frame_to_time_floor(frame_num: int, fps: float) -> float:
    """
    Convert frame number to timestamp (start of frame).

    When does frame N start?

    Args:
        frame_num: Frame number (0-indexed)
        fps: Frame rate

    Returns:
        Timestamp in milliseconds
    """
    frame_duration_ms = 1000.0 / fps
    return frame_num * frame_duration_ms


# ============================================================================
# FPS Detection
# ============================================================================

def detect_video_fps(video_path: str) -> float:
    """
    Detect video frame rate using ffprobe.

    Handles fractional rates like "24000/1001" → 23.976

    Args:
        video_path: Path to video file

    Returns:
        Frame rate as float, defaults to 23.976 if detection fails
    """
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=r_frame_rate',
            '-of', 'json',
            video_path
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

        if result.returncode != 0:
            print(f"ffprobe failed, using default fps 23.976")
            return 23.976

        data = json.loads(result.stdout)
        streams = data.get('streams', [])

        if not streams:
            print(f"No video stream found, using default fps 23.976")
            return 23.976

        r_frame_rate = streams[0].get('r_frame_rate', '24000/1001')

        # Handle fractional rates
        if '/' in r_frame_rate:
            num, denom = r_frame_rate.split('/')
            fps = float(num) / float(denom)
        else:
            fps = float(r_frame_rate)

        return fps

    except Exception as e:
        print(f"FPS detection failed: {e}, using default 23.976")
        return 23.976


def get_video_duration(video_path: str) -> float:
    """
    Get video duration in seconds using ffprobe.

    Args:
        video_path: Path to video file

    Returns:
        Duration in seconds, defaults to 0.0 if detection fails
    """
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'json',
            video_path
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

        if result.returncode != 0:
            return 0.0

        data = json.loads(result.stdout)
        duration = float(data.get('format', {}).get('duration', 0.0))

        return duration

    except Exception as e:
        print(f"Duration detection failed: {e}")
        return 0.0


# ============================================================================
# VideoReader with VapourSynth Support
# ============================================================================

class VideoReader:
    """
    Efficient video reader with VapourSynth + FFMS2 support.

    Priority order:
    1. VapourSynth + FFMS2 (fastest - persistent index caching)
    2. pyffms2 (fast - indexed seeking)
    3. FFmpeg (slow fallback)
    """

    def __init__(self, video_path: str, temp_dir: Optional[Path] = None):
        self.video_path = str(video_path)
        self.temp_dir = temp_dir
        self.vs_clip = None
        self.ffms2_source = None
        self.fps = None
        self.use_vapoursynth = False
        self.use_ffms2 = False

        # Try VapourSynth first
        if self._try_vapoursynth():
            print(f"[VideoReader] Using VapourSynth + FFMS2 for {Path(video_path).name}")
            return

        # Try pyffms2
        if self._try_ffms2():
            print(f"[VideoReader] Using pyffms2 for {Path(video_path).name}")
            return

        # Fallback to FFmpeg
        self.fps = detect_video_fps(self.video_path)
        print(f"[VideoReader] Using FFmpeg fallback for {Path(video_path).name}")

    def _get_index_cache_path(self) -> Path:
        """Generate cache path for FFMS2 index."""
        import hashlib
        import os

        video_path_obj = Path(self.video_path)

        # Get file metadata for cache invalidation
        stat = os.stat(self.video_path)
        file_size = stat.st_size
        mtime = int(stat.st_mtime)

        # Include parent directory to distinguish sources
        parent_dir = video_path_obj.parent.name

        if not parent_dir or parent_dir == '.':
            path_hash = hashlib.md5(str(video_path_obj.resolve()).encode()).hexdigest()[:8]
            cache_key = f"{video_path_obj.stem}_{path_hash}_{file_size}_{mtime}"
        else:
            cache_key = f"{parent_dir}_{video_path_obj.stem}_{file_size}_{mtime}"

        # Use temp_dir if provided
        if self.temp_dir:
            cache_dir = self.temp_dir / "ffindex"
            cache_dir.mkdir(parents=True, exist_ok=True)
        else:
            import tempfile
            cache_dir = Path(tempfile.gettempdir()) / "remux_toolkit_ffindex"
            cache_dir.mkdir(parents=True, exist_ok=True)

        return cache_dir / f"{cache_key}.ffindex"

    def _try_vapoursynth(self) -> bool:
        """Try to initialize VapourSynth with FFMS2 plugin."""
        try:
            import vapoursynth as vs

            core = vs.core

            # Check if ffms2 plugin is available
            if not hasattr(core, 'ffms2'):
                return False

            # Generate cache path
            index_path = self._get_index_cache_path()

            if index_path.exists():
                print(f"[VideoReader] Reusing FFMS2 index: {index_path.name}")
            else:
                print(f"[VideoReader] Creating FFMS2 index (this may take 1-2 minutes)...")

            # Load video with index caching
            clip = core.ffms2.Source(
                source=self.video_path,
                cachefile=str(index_path)
            )

            self.vs_clip = clip
            self.fps = clip.fps_num / clip.fps_den
            self.use_vapoursynth = True

            print(f"[VideoReader] VapourSynth ready (FPS: {self.fps:.3f})")
            return True

        except ImportError:
            return False
        except AttributeError:
            return False
        except Exception as e:
            print(f"[VideoReader] VapourSynth init failed: {e}")
            return False

    def _try_ffms2(self) -> bool:
        """Try to initialize pyffms2."""
        try:
            import ffms2

            print(f"[VideoReader] Creating FFMS2 index (this may take 1-2 minutes)...")

            # Create indexer and generate index
            indexer = ffms2.Indexer(self.video_path)
            index = indexer.do_indexing2()

            # Get first video track
            track_number = index.get_first_indexed_track_of_type(ffms2.FFMS_TYPE_VIDEO)

            # Create video source from index
            self.ffms2_source = ffms2.VideoSource(self.video_path, track_number, index)
            self.use_ffms2 = True

            # Get video properties
            self.fps = self.ffms2_source.properties.FPSNumerator / self.ffms2_source.properties.FPSDenominator

            print(f"[VideoReader] FFMS2 ready (FPS: {self.fps:.3f})")
            return True

        except ImportError:
            return False
        except Exception as e:
            print(f"[VideoReader] FFMS2 init failed: {e}")
            return False

    def get_frame_at_index(self, frame_num: int) -> Optional[Image.Image]:
        """
        Extract frame by frame number (0-indexed).

        Args:
            frame_num: Frame index

        Returns:
            PIL Image (grayscale), or None on failure
        """
        if self.use_vapoursynth and self.vs_clip:
            return self._get_frame_vapoursynth_by_index(frame_num)
        elif self.use_ffms2 and self.ffms2_source:
            return self._get_frame_ffms2_by_index(frame_num)
        else:
            # FFmpeg fallback
            if not self.fps:
                return None
            time_ms = frame_num * 1000.0 / self.fps
            return self._get_frame_ffmpeg(time_ms)

    def _get_frame_vapoursynth_by_index(self, frame_num: int) -> Optional[Image.Image]:
        """Extract frame using VapourSynth (frame-accurate)."""
        try:
            # Clamp to valid range
            frame_num = max(0, min(frame_num, len(self.vs_clip) - 1))

            # Get frame directly by index
            frame = self.vs_clip.get_frame(frame_num)

            # Extract Y (luma) plane as grayscale
            y_plane = np.asarray(frame[0])

            # Normalize bit depth to 8-bit
            if y_plane.dtype == np.uint16:
                max_val = y_plane.max()
                if max_val <= 1023:  # 10-bit
                    y_plane = (y_plane >> 2).astype(np.uint8)
                else:  # 12-bit or 16-bit
                    y_plane = (y_plane >> 8).astype(np.uint8)
            elif y_plane.dtype != np.uint8:
                y_plane = y_plane.astype(np.uint8)

            return Image.fromarray(y_plane, 'L')

        except Exception as e:
            print(f"[VideoReader] VapourSynth frame extraction failed: {e}")
            return None

    def _get_frame_ffms2_by_index(self, frame_num: int) -> Optional[Image.Image]:
        """Extract frame using pyffms2 (frame-accurate)."""
        try:
            # Clamp to valid range
            frame_num = max(0, min(frame_num, self.ffms2_source.properties.NumFrames - 1))

            # Get frame directly by index
            frame = self.ffms2_source.get_frame(frame_num)

            # Get Y plane
            frame_array = frame.planes[0]

            # Normalize bit depth
            if frame_array.dtype == np.uint16:
                max_val = frame_array.max()
                if max_val <= 1023:  # 10-bit
                    frame_array = (frame_array >> 2).astype(np.uint8)
                else:  # 12-bit or 16-bit
                    frame_array = (frame_array >> 8).astype(np.uint8)
            elif frame_array.dtype != np.uint8:
                frame_array = frame_array.astype(np.uint8)

            return Image.fromarray(frame_array, 'L')

        except Exception as e:
            print(f"[VideoReader] FFMS2 frame extraction failed: {e}")
            return None

    def _get_frame_ffmpeg(self, time_ms: float) -> Optional[Image.Image]:
        """Extract frame using FFmpeg (slow fallback)."""
        import tempfile
        import os

        try:
            time_sec = time_ms / 1000.0

            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                tmp_path = tmp.name

            cmd = [
                'ffmpeg', '-v', 'error',
                '-ss', f'{time_sec:.3f}',
                '-i', self.video_path,
                '-vframes', '1',
                '-pix_fmt', 'gray',  # Grayscale
                '-q:v', '2',
                '-y',
                tmp_path
            ]

            result = subprocess.run(cmd, capture_output=True, timeout=30)

            if result.returncode != 0:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                return None

            frame = Image.open(tmp_path)
            frame.load()
            os.unlink(tmp_path)

            return frame

        except Exception:
            return None

    def close(self):
        """Release video resources."""
        if self.vs_clip:
            self.vs_clip = None
        if self.ffms2_source:
            self.ffms2_source = None


# ============================================================================
# Perceptual Hashing
# ============================================================================

def compute_frame_hash(frame: Image.Image, hash_size: int = 8, method: str = 'dhash'):
    """
    Compute perceptual hash of a frame.

    Args:
        frame: PIL Image object (grayscale)
        hash_size: Hash size (8x8 = 64 bits, 16x16 = 256 bits)
        method: Hash method ('dhash', 'phash', 'average_hash')

    Returns:
        ImageHash object, or None on failure
    """
    try:
        import imagehash

        if method == 'dhash':
            return imagehash.dhash(frame, hash_size=hash_size)
        elif method == 'phash':
            return imagehash.phash(frame, hash_size=hash_size)
        else:  # 'average_hash'
            return imagehash.average_hash(frame, hash_size=hash_size)

    except ImportError:
        print("[FrameSync] ERROR: imagehash not installed. Install with: pip install imagehash")
        return None
    except Exception as e:
        print(f"[FrameSync] Hash computation failed: {e}")
        return None


# ============================================================================
# Checkpoint Selection
# ============================================================================

def select_checkpoints(duration_sec: float, num_checkpoints: int = 3) -> List[float]:
    """
    Select checkpoint times at strategic points in the video.

    Selects at 5%, 50%, 95% of duration (avoiding first/last 2 minutes).

    Args:
        duration_sec: Video duration in seconds
        num_checkpoints: Number of checkpoints (default: 3)

    Returns:
        List of checkpoint times in seconds
    """
    # Avoid first/last 2 minutes (120 seconds)
    safe_margin = 120.0
    safe_start = safe_margin
    safe_end = max(duration_sec - safe_margin, safe_start + 60)
    safe_duration = safe_end - safe_start

    if num_checkpoints == 3:
        # At 5%, 50%, 95% of safe range
        positions = [0.05, 0.50, 0.95]
    else:
        # Evenly distributed
        positions = [i / (num_checkpoints - 1) for i in range(num_checkpoints)]

    checkpoints = [safe_start + pos * safe_duration for pos in positions]

    return checkpoints


# ============================================================================
# Sliding Window Frame Matching
# ============================================================================

def match_frame_at_checkpoint(
    source_reader: VideoReader,
    target_reader: VideoReader,
    checkpoint_time_sec: float,
    audio_offset_sec: float,
    fps_source: float,
    fps_target: float,
    window_radius: int = 5,
    search_range_frames: int = 48,  # ±2 seconds at 24fps
    hash_size: int = 8,
    hash_algorithm: str = 'dhash',
    hash_threshold: int = 5,
    progress_callback=None
) -> Optional[CheckpointResult]:
    """
    Match frames at a checkpoint using sliding window + perceptual hashing.

    Algorithm:
    1. Convert checkpoint time to source center frame
    2. Extract source window: [center-radius, ..., center, ..., center+radius]
    3. Compute perceptual hashes for all source frames
    4. Predict target center using audio offset
    5. Search ±search_range_frames around prediction
    6. For each target position, extract window and compute hashes
    7. Find position with best hash match (lowest total distance)
    8. Calculate precise offset from matched frame times

    Args:
        source_reader: VideoReader for source video
        target_reader: VideoReader for target video
        checkpoint_time_sec: Checkpoint time in seconds
        audio_offset_sec: Audio correlation offset (target_time = source_time - offset)
        fps_source: Source video frame rate
        fps_target: Target video frame rate
        window_radius: Frames before/after center (default: 5 = 11 frame window)
        search_range_frames: Search ±N frames around prediction
        hash_size: Hash size (default: 8 = 64-bit hash)
        hash_algorithm: Hash method ('dhash', 'phash', 'average_hash')
        hash_threshold: Max hamming distance per frame
        progress_callback: Optional progress callback

    Returns:
        CheckpointResult or None on failure
    """
    checkpoint_time_ms = checkpoint_time_sec * 1000.0

    # Calculate source center frame
    source_center_frame = time_to_frame_floor(checkpoint_time_ms, fps_source)

    # Calculate sub-frame offset (how far into the frame)
    source_frame_start_ms = frame_to_time_floor(source_center_frame, fps_source)
    sub_frame_offset_ms = checkpoint_time_ms - source_frame_start_ms

    print(f"\n[FrameSync] Checkpoint at {checkpoint_time_sec:.2f}s")
    print(f"[FrameSync]   Source center frame: {source_center_frame}")
    print(f"[FrameSync]   Sub-frame offset: {sub_frame_offset_ms:.3f}ms")

    # Build source window
    source_window_frames = list(range(
        source_center_frame - window_radius,
        source_center_frame + window_radius + 1
    ))

    # Skip if window would include negative frames
    if source_window_frames[0] < 0:
        print(f"[FrameSync]   ERROR: Window starts before frame 0")
        return None

    # Extract and hash source frames
    source_hashes = []
    for frame_num in source_window_frames:
        frame = source_reader.get_frame_at_index(frame_num)
        if frame is None:
            print(f"[FrameSync]   ERROR: Could not extract source frame {frame_num}")
            return None

        frame_hash = compute_frame_hash(frame, hash_size, hash_algorithm)
        if frame_hash is None:
            print(f"[FrameSync]   ERROR: Could not hash source frame {frame_num}")
            return None

        source_hashes.append(frame_hash)

    print(f"[FrameSync]   Extracted {len(source_hashes)} source frames")

    # Predict target center using audio offset
    target_center_time_ms = checkpoint_time_ms - (audio_offset_sec * 1000.0)
    target_center_frame = time_to_frame_floor(target_center_time_ms, fps_target)

    print(f"[FrameSync]   Predicted target center: frame {target_center_frame} ({target_center_time_ms:.1f}ms)")

    # Search range - ensure all values are integers for range()
    search_start_frame = int(max(window_radius, target_center_frame - search_range_frames))
    search_end_frame = int(target_center_frame + search_range_frames)

    print(f"[FrameSync]   Searching frames {search_start_frame} to {search_end_frame} ({search_end_frame - search_start_frame + 1} positions)")

    # Slide window through target and find best match
    best_target_center = target_center_frame
    best_total_distance = float('inf')
    best_matched_count = 0
    best_avg_distance = float('inf')

    for target_center in range(search_start_frame, search_end_frame + 1):
        target_window_frames = list(range(
            target_center - window_radius,
            target_center + window_radius + 1
        ))

        # Skip if window includes negative frames
        if target_window_frames[0] < 0:
            continue

        # Extract and hash target frames
        target_hashes = []
        window_valid = True

        for frame_num in target_window_frames:
            frame = target_reader.get_frame_at_index(frame_num)
            if frame is None:
                window_valid = False
                break

            frame_hash = compute_frame_hash(frame, hash_size, hash_algorithm)
            if frame_hash is None:
                window_valid = False
                break

            target_hashes.append(frame_hash)

        if not window_valid:
            continue

        # Calculate total hash distance for this alignment
        total_distance = 0
        matched_count = 0

        for source_hash, target_hash in zip(source_hashes, target_hashes):
            distance = source_hash - target_hash  # Hamming distance
            total_distance += distance

            if distance <= hash_threshold:
                matched_count += 1

        # Update best match
        if total_distance < best_total_distance:
            best_total_distance = total_distance
            best_target_center = target_center
            best_matched_count = matched_count
            best_avg_distance = total_distance / len(source_hashes)

        # Progress update
        if progress_callback and (target_center - search_start_frame) % 10 == 0:
            progress = (target_center - search_start_frame) / (search_end_frame - search_start_frame + 1)
            progress_callback(f"Searching checkpoint frames... {progress*100:.0f}%")

    # Calculate match quality
    window_size = len(source_hashes)
    match_quality = best_matched_count / window_size

    # Require at least 70% of frames to match
    if match_quality < 0.7:
        print(f"[FrameSync]   POOR MATCH: Only {best_matched_count}/{window_size} frames matched ({match_quality:.1%})")
        return None

    # Calculate precise offset with sub-frame preservation
    target_frame_start_ms = frame_to_time_floor(best_target_center, fps_target)
    target_time_ms = target_frame_start_ms + sub_frame_offset_ms  # Add sub-frame offset back

    # Precise offset: target_time - source_time
    precise_offset_ms = target_time_ms - checkpoint_time_ms

    frame_adjustment = best_target_center - target_center_frame

    print(f"[FrameSync]   Best match: target frame {best_target_center}")
    print(f"[FrameSync]   Frame adjustment: {frame_adjustment:+d} frames from prediction")
    print(f"[FrameSync]   Match quality: {best_matched_count}/{window_size} frames ({match_quality:.1%})")
    print(f"[FrameSync]   Avg hash distance: {best_avg_distance:.1f}")
    print(f"[FrameSync]   Precise offset: {precise_offset_ms:+.3f}ms")

    return CheckpointResult(
        checkpoint_idx=0,  # Will be set by caller
        checkpoint_time_sec=checkpoint_time_sec,
        source_center_frame=source_center_frame,
        target_matched_frame=best_target_center,
        precise_offset_ms=precise_offset_ms,
        match_quality=match_quality,
        matched_frames_count=best_matched_count,
        avg_hash_distance=best_avg_distance
    )


# ============================================================================
# Main Entry Point
# ============================================================================

def audio_correlation_frame_sync(
    source_a_path: str,
    source_b_path: str,
    audio_offset_sec: float,
    fps_a: Optional[float] = None,
    fps_b: Optional[float] = None,
    num_checkpoints: int = 3,
    window_radius: int = 5,
    search_range_frames: int = 48,
    hash_size: int = 8,
    hash_algorithm: str = 'dhash',
    hash_threshold: int = 5,
    agreement_tolerance_ms: float = 100.0,
    temp_dir: Optional[Path] = None,
    progress_callback=None
) -> FrameSyncResult:
    """
    Audio-correlation-anchored frame synchronization.

    This function adapts the subtitle-anchored-frame-snap approach to use
    audio correlation as anchors. It:

    1. Uses audio correlation offset as starting point
    2. Selects checkpoints at 5%, 50%, 95% of duration
    3. For each checkpoint, uses sliding window frame matching
    4. Verifies checkpoint agreement
    5. Returns precise frame-level offset

    Args:
        source_a_path: Path to source A video
        source_b_path: Path to source B video
        audio_offset_sec: Audio correlation offset (B_time = A_time - offset)
        fps_a: Source A frame rate (auto-detected if None)
        fps_b: Source B frame rate (auto-detected if None)
        num_checkpoints: Number of checkpoints (default: 3)
        window_radius: Frames before/after center (default: 5)
        search_range_frames: Search ±N frames around prediction (default: 48)
        hash_size: Hash size (default: 8)
        hash_algorithm: Hash method (default: 'dhash')
        hash_threshold: Max hamming distance per frame (default: 5)
        agreement_tolerance_ms: Max deviation between checkpoints (default: 100ms)
        temp_dir: Optional temp directory for FFMS2 index caching
        progress_callback: Optional progress callback(message, progress_pct)

    Returns:
        FrameSyncResult with success status, offset, confidence, and checkpoint details
    """
    print(f"\n{'='*70}")
    print(f"Audio-Correlation-Anchored Frame Sync")
    print(f"{'='*70}")
    print(f"Source A: {Path(source_a_path).name}")
    print(f"Source B: {Path(source_b_path).name}")
    print(f"Audio offset: {audio_offset_sec:.6f}s ({audio_offset_sec*1000:.3f}ms)")

    if progress_callback:
        progress_callback("Initializing video readers...", 5)

    # Detect FPS if not provided
    if fps_a is None:
        fps_a = detect_video_fps(source_a_path)
    if fps_b is None:
        fps_b = detect_video_fps(source_b_path)

    print(f"FPS: A={fps_a:.3f}, B={fps_b:.3f}")

    # Get duration from source A
    duration_a = get_video_duration(source_a_path)
    if duration_a == 0.0:
        return FrameSyncResult(
            success=False,
            offset_sec=audio_offset_sec,
            confidence=0.0,
            checkpoints=[],
            agreement_status='error',
            error='Failed to detect video duration'
        )

    print(f"Duration: {duration_a:.1f}s ({duration_a/60:.1f} minutes)")

    # Select checkpoints
    checkpoint_times = select_checkpoints(duration_a, num_checkpoints)
    print(f"\nCheckpoints: {[f'{t:.1f}s' for t in checkpoint_times]}")

    if progress_callback:
        progress_callback("Opening video files...", 10)

    # Open video readers
    try:
        source_a_reader = VideoReader(source_a_path, temp_dir)
        source_b_reader = VideoReader(source_b_path, temp_dir)
    except Exception as e:
        return FrameSyncResult(
            success=False,
            offset_sec=audio_offset_sec,
            confidence=0.0,
            checkpoints=[],
            agreement_status='error',
            error=f'Failed to open videos: {e}'
        )

    # Process each checkpoint
    checkpoint_results = []

    for i, checkpoint_time in enumerate(checkpoint_times):
        if progress_callback:
            progress = 15 + int(70 * (i / len(checkpoint_times)))
            progress_callback(f"Processing checkpoint {i+1}/{len(checkpoint_times)}...", progress)

        result = match_frame_at_checkpoint(
            source_a_reader,
            source_b_reader,
            checkpoint_time,
            audio_offset_sec,
            fps_a,
            fps_b,
            window_radius,
            search_range_frames,
            hash_size,
            hash_algorithm,
            hash_threshold,
            progress_callback
        )

        if result:
            result.checkpoint_idx = i + 1
            checkpoint_results.append(result)

    # Clean up
    source_a_reader.close()
    source_b_reader.close()

    if progress_callback:
        progress_callback("Verifying checkpoint agreement...", 90)

    # Verify checkpoint agreement
    if len(checkpoint_results) == 0:
        print(f"\n[FrameSync] ERROR: No checkpoints matched successfully")
        return FrameSyncResult(
            success=False,
            offset_sec=audio_offset_sec,
            confidence=0.0,
            checkpoints=[],
            agreement_status='no_matches',
            error='No checkpoints matched successfully'
        )

    if len(checkpoint_results) == 1:
        print(f"\n[FrameSync] WARNING: Only 1 checkpoint matched (cannot verify agreement)")
        offset_ms = checkpoint_results[0].precise_offset_ms
        final_offset_sec = offset_ms / 1000.0
        confidence = checkpoint_results[0].match_quality

        return FrameSyncResult(
            success=True,
            offset_sec=final_offset_sec,
            confidence=confidence * 0.7,  # Reduce confidence due to insufficient data
            checkpoints=checkpoint_results,
            agreement_status='insufficient',
            error=None
        )

    # Check agreement between checkpoints
    offsets_ms = [r.precise_offset_ms for r in checkpoint_results]
    median_offset_ms = sorted(offsets_ms)[len(offsets_ms) // 2]
    max_deviation = max(abs(offset - median_offset_ms) for offset in offsets_ms)

    print(f"\n[FrameSync] Checkpoint offsets: {[f'{o:.3f}ms' for o in offsets_ms]}")
    print(f"[FrameSync] Median offset: {median_offset_ms:.3f}ms")
    print(f"[FrameSync] Max deviation: {max_deviation:.3f}ms")

    if max_deviation <= agreement_tolerance_ms:
        # Checkpoints agree
        print(f"[FrameSync] ✓ Checkpoints AGREE (within {agreement_tolerance_ms:.1f}ms tolerance)")

        # Use median offset
        final_offset_sec = median_offset_ms / 1000.0

        # Calculate average confidence
        avg_confidence = sum(r.match_quality for r in checkpoint_results) / len(checkpoint_results)

        print(f"[FrameSync] Final offset: {final_offset_sec:.6f}s ({final_offset_sec*1000:.3f}ms)")
        print(f"[FrameSync] Confidence: {avg_confidence:.2%}")

        return FrameSyncResult(
            success=True,
            offset_sec=final_offset_sec,
            confidence=avg_confidence,
            checkpoints=checkpoint_results,
            agreement_status='agree',
            error=None
        )
    else:
        # Checkpoints disagree
        print(f"[FrameSync] ✗ Checkpoints DISAGREE (spread {max_deviation:.3f}ms > {agreement_tolerance_ms:.1f}ms)")
        print(f"[FrameSync] This may indicate different cuts or timing drift")

        # Still return median but mark as uncertain
        final_offset_sec = median_offset_ms / 1000.0
        avg_confidence = sum(r.match_quality for r in checkpoint_results) / len(checkpoint_results)

        return FrameSyncResult(
            success=False,
            offset_sec=final_offset_sec,
            confidence=avg_confidence * 0.5,  # Heavily penalize confidence
            checkpoints=checkpoint_results,
            agreement_status='disagree',
            error=f'Checkpoints disagree: max deviation {max_deviation:.1f}ms'
        )

    if progress_callback:
        progress_callback("Frame sync complete", 100)
