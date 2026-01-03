# remux_toolkit/tools/video_ab_comparator/core/frame_mapper.py
"""
Frame-perfect mapping using VideoTimestamps library.
Maps corresponding frames between two videos using audio sync offset.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple, Dict
from pathlib import Path

try:
    from VideoTimestamps import VideoTimestamps
    HAS_VIDEO_TIMESTAMPS = True
except ImportError:
    HAS_VIDEO_TIMESTAMPS = False
    print("WARNING: VideoTimestamps not installed. Install with: pip install VideoTimestamps")


@dataclass
class FrameMapping:
    """Maps frame numbers between two videos."""
    frame_a: int  # Frame number in source A
    frame_b: int  # Corresponding frame number in source B
    timestamp_a: float  # Timestamp in source A (seconds)
    timestamp_b: float  # Timestamp in source B (seconds)
    exact_match: bool  # True if frames align perfectly


class FrameMapper:
    """
    Provides frame-perfect mapping between two video sources.

    Uses VideoTimestamps to get exact frame timecodes and maps frames
    between videos accounting for sync offset and potential FPS differences.
    """

    def __init__(self, source_a_path: str, source_b_path: str,
                 offset_sec: float, drift_ratio: float = 0.0):
        """
        Initialize frame mapper.

        Args:
            source_a_path: Path to source A video
            source_b_path: Path to source B video
            offset_sec: Audio sync offset in seconds (how much B is ahead of A)
            drift_ratio: FPS drift ratio (for variable frame rate)
        """
        self.source_a_path = Path(source_a_path)
        self.source_b_path = Path(source_b_path)
        self.offset_sec = offset_sec
        self.drift_ratio = drift_ratio

        self.vts_a: Optional[VideoTimestamps] = None
        self.vts_b: Optional[VideoTimestamps] = None

        if HAS_VIDEO_TIMESTAMPS:
            try:
                print(f"Loading frame timestamps for {self.source_a_path.name}...")
                self.vts_a = VideoTimestamps(str(source_a_path))

                print(f"Loading frame timestamps for {self.source_b_path.name}...")
                self.vts_b = VideoTimestamps(str(source_b_path))

                print(f"Frame mapper initialized: "
                      f"A has {len(self.vts_a)} frames, B has {len(self.vts_b)} frames")

            except Exception as e:
                print(f"Failed to initialize VideoTimestamps: {e}")
                self.vts_a = None
                self.vts_b = None
        else:
            print("VideoTimestamps not available. Frame mapping will use calculated positions.")

    def is_available(self) -> bool:
        """Check if frame mapping is available."""
        return HAS_VIDEO_TIMESTAMPS and self.vts_a is not None and self.vts_b is not None

    def get_frame_at_time(self, vts: VideoTimestamps, time_sec: float) -> Optional[int]:
        """
        Get frame number at specific timestamp.

        Args:
            vts: VideoTimestamps instance
            time_sec: Target time in seconds

        Returns:
            Frame number (0-based) or None if not found
        """
        if not vts:
            return None

        try:
            # Convert time to milliseconds
            time_ms = time_sec * 1000.0

            # VideoTimestamps provides frame timestamps
            # Find closest frame to target time
            for frame_num in range(len(vts)):
                frame_time = vts[frame_num]  # Get timestamp for this frame

                if frame_time >= time_ms:
                    # Check if previous frame is closer
                    if frame_num > 0:
                        prev_time = vts[frame_num - 1]
                        if abs(prev_time - time_ms) < abs(frame_time - time_ms):
                            return frame_num - 1

                    return frame_num

            # If we're past the end, return last frame
            return len(vts) - 1

        except Exception as e:
            print(f"Error getting frame at time {time_sec}: {e}")
            return None

    def map_frame_a_to_b(self, frame_a: int) -> Optional[FrameMapping]:
        """
        Map a frame from source A to corresponding frame in source B.

        Args:
            frame_a: Frame number in source A (0-based)

        Returns:
            FrameMapping with corresponding frame in B, or None if mapping fails
        """
        if not self.is_available():
            return None

        try:
            # Get timestamp for frame A
            timestamp_a_ms = self.vts_a[frame_a]
            timestamp_a = timestamp_a_ms / 1000.0

            # Calculate corresponding timestamp in B
            # Convention: ts_b = ts_a - offset
            # If drift exists: ts_b = ts_a - (offset + drift * ts_a)
            timestamp_b = timestamp_a - (self.offset_sec + self.drift_ratio * timestamp_a)

            # Find corresponding frame in B
            frame_b = self.get_frame_at_time(self.vts_b, timestamp_b)

            if frame_b is None:
                return None

            # Get actual timestamp of frame B
            timestamp_b_actual_ms = self.vts_b[frame_b]
            timestamp_b_actual = timestamp_b_actual_ms / 1000.0

            # Check if frames align exactly (within 1ms tolerance)
            exact_match = abs(timestamp_b_actual - timestamp_b) < 0.001

            return FrameMapping(
                frame_a=frame_a,
                frame_b=frame_b,
                timestamp_a=timestamp_a,
                timestamp_b=timestamp_b_actual,
                exact_match=exact_match
            )

        except (IndexError, Exception) as e:
            print(f"Error mapping frame {frame_a}: {e}")
            return None

    def map_timestamp_a_to_frame_b(self, timestamp_a: float) -> Optional[FrameMapping]:
        """
        Map a timestamp from source A to corresponding frame in source B.

        Args:
            timestamp_a: Timestamp in source A (seconds)

        Returns:
            FrameMapping with frame numbers and timestamps
        """
        if not self.is_available():
            return None

        # Find frame at timestamp in A
        frame_a = self.get_frame_at_time(self.vts_a, timestamp_a)

        if frame_a is None:
            return None

        return self.map_frame_a_to_b(frame_a)

    def calculate_frame_mapping_fallback(self, timestamp_a: float, fps_a: float,
                                        fps_b: float) -> Tuple[int, int]:
        """
        Fallback method when VideoTimestamps is not available.
        Uses simple FPS-based calculation.

        Args:
            timestamp_a: Timestamp in source A (seconds)
            fps_a: Frame rate of source A
            fps_b: Frame rate of source B

        Returns:
            (frame_a, frame_b) tuple
        """
        frame_a = int(timestamp_a * fps_a)

        # Calculate timestamp in B
        timestamp_b = timestamp_a - (self.offset_sec + self.drift_ratio * timestamp_a)
        frame_b = int(timestamp_b * fps_b)

        return frame_a, frame_b

    def get_exact_frame_timestamps(self, vts: VideoTimestamps, start_time: float,
                                   duration: float, target_fps: float = 10.0) -> list:
        """
        Get exact timestamps for frames in a time range using VideoTimestamps.

        This ensures frame-accurate extraction by using the actual frame timestamps
        from the video file instead of calculated values.

        Args:
            vts: VideoTimestamps instance
            start_time: Start time in seconds
            duration: Duration to extract in seconds
            target_fps: Target frame rate for extraction (default 10fps)

        Returns:
            List of exact timestamps (in seconds) from VideoTimestamps
        """
        if not vts:
            return []

        try:
            # Calculate time range
            end_time = start_time + duration

            # Find frame numbers for this range
            start_frame = self.get_frame_at_time(vts, start_time)
            end_frame = self.get_frame_at_time(vts, end_time)

            if start_frame is None or end_frame is None:
                return []

            # Calculate frame step to achieve target fps
            total_frames_in_range = end_frame - start_frame + 1
            native_fps = total_frames_in_range / duration if duration > 0 else 24.0

            # Calculate step size to get approximately target_fps
            frame_step = max(1, int(native_fps / target_fps))

            # Get exact timestamps for sampled frames
            timestamps = []
            for frame_num in range(start_frame, end_frame + 1, frame_step):
                if frame_num < len(vts):
                    # Get exact timestamp from VideoTimestamps (in milliseconds)
                    exact_ts_ms = vts[frame_num]
                    exact_ts_sec = exact_ts_ms / 1000.0
                    timestamps.append(exact_ts_sec)

            return timestamps

        except Exception as e:
            print(f"Error getting exact frame timestamps: {e}")
            return []

    def get_sync_quality_report(self, sample_points: int = 10) -> Dict:
        """
        Generate a report on sync quality across the video.

        Args:
            sample_points: Number of points to sample across video

        Returns:
            Dictionary with sync quality metrics
        """
        if not self.is_available():
            return {"error": "VideoTimestamps not available"}

        total_frames_a = len(self.vts_a)
        sample_frames = [int(i * total_frames_a / (sample_points + 1)) for i in range(1, sample_points + 1)]

        exact_matches = 0
        max_drift_ms = 0.0
        drift_values = []

        for frame_a in sample_frames:
            mapping = self.map_frame_a_to_b(frame_a)
            if mapping:
                if mapping.exact_match:
                    exact_matches += 1

                # Calculate drift (difference from expected)
                expected_ts_b = mapping.timestamp_a - self.offset_sec
                actual_ts_b = mapping.timestamp_b
                drift_ms = abs(expected_ts_b - actual_ts_b) * 1000.0

                drift_values.append(drift_ms)
                max_drift_ms = max(max_drift_ms, drift_ms)

        return {
            "total_samples": len(sample_frames),
            "exact_matches": exact_matches,
            "exact_match_rate": exact_matches / len(sample_frames) if sample_frames else 0.0,
            "max_drift_ms": max_drift_ms,
            "avg_drift_ms": sum(drift_values) / len(drift_values) if drift_values else 0.0,
            "offset_sec": self.offset_sec,
            "drift_ratio": self.drift_ratio
        }
