# remux_toolkit/tools/video_ab_comparator/core/pyav_source.py
"""
PyAV-based frame extraction for frame-accurate seeking.
Works with VideoTimestamps to ensure exact frame correspondence.
"""

from __future__ import annotations
import numpy as np
from typing import Optional, Tuple
from pathlib import Path

try:
    import av
    HAS_PYAV = True
except ImportError:
    HAS_PYAV = False
    print("WARNING: PyAV not installed. Install with: pip install av")

try:
    from VideoTimestamps import VideoTimestamps
    HAS_VIDEO_TIMESTAMPS = True
except ImportError:
    HAS_VIDEO_TIMESTAMPS = False


class PyAVFrameExtractor:
    """
    Frame-accurate video frame extractor using PyAV.

    Unlike FFmpeg timestamp-based seeking, PyAV seeks by frame number,
    ensuring frame-perfect accuracy when combined with VideoTimestamps.
    """

    def __init__(self, video_path: str):
        """
        Initialize PyAV extractor.

        Args:
            video_path: Path to video file
        """
        self.video_path = Path(video_path)
        self.container: Optional[av.container.InputContainer] = None
        self.video_stream: Optional[av.video.VideoStream] = None
        self.vts: Optional[VideoTimestamps] = None

        # Cache
        self.width: Optional[int] = None
        self.height: Optional[int] = None
        self.fps: Optional[float] = None
        self.total_frames: Optional[int] = None

        # Track last seek position for optimization
        self._last_frame_num: int = -1

    def open(self) -> bool:
        """
        Open video container and load VideoTimestamps.

        Returns:
            True if successful, False otherwise
        """
        if not HAS_PYAV:
            print("PyAV not available")
            return False

        try:
            # Open container
            self.container = av.open(str(self.video_path))
            self.video_stream = self.container.streams.video[0]

            # Get video properties
            self.width = self.video_stream.width
            self.height = self.video_stream.height
            self.fps = float(self.video_stream.average_rate)

            # Load VideoTimestamps if available
            if HAS_VIDEO_TIMESTAMPS:
                try:
                    self.vts = VideoTimestamps(str(self.video_path))
                    self.total_frames = len(self.vts)
                    print(f"Loaded VideoTimestamps: {self.total_frames} frames at {self.fps:.3f}fps")
                except Exception as e:
                    print(f"VideoTimestamps load failed: {e}, using PyAV only")
                    self.vts = None
                    # Estimate total frames from duration
                    if self.container.duration:
                        duration_sec = self.container.duration / av.time_base
                        self.total_frames = int(duration_sec * self.fps)

            return True

        except Exception as e:
            print(f"Failed to open video with PyAV: {e}")
            return False

    def close(self):
        """Close video container."""
        if self.container:
            self.container.close()
            self.container = None
            self.video_stream = None

    def get_frame_at_timestamp(self, timestamp: float) -> Optional[np.ndarray]:
        """
        Get frame at specific timestamp using VideoTimestamps.

        Args:
            timestamp: Time in seconds

        Returns:
            Frame as numpy array (RGB) or None
        """
        if not self.vts:
            # Fallback to estimated frame number
            frame_num = int(timestamp * self.fps)
        else:
            # Use VideoTimestamps to find exact frame
            timestamp_ms = timestamp * 1000.0

            # Binary search for closest frame
            for i in range(len(self.vts)):
                if self.vts[i] >= timestamp_ms:
                    # Check if previous frame is closer
                    if i > 0:
                        prev_diff = abs(self.vts[i-1] - timestamp_ms)
                        curr_diff = abs(self.vts[i] - timestamp_ms)
                        frame_num = i - 1 if prev_diff < curr_diff else i
                    else:
                        frame_num = i
                    break
            else:
                frame_num = len(self.vts) - 1

        return self.get_frame_at_index(frame_num)

    def get_frame_at_index(self, frame_num: int) -> Optional[np.ndarray]:
        """
        Get frame by exact frame number (frame-accurate seeking).

        Args:
            frame_num: Frame number (0-based)

        Returns:
            Frame as numpy array (RGB, shape: [H, W, 3]) or None
        """
        if not self.container or not self.video_stream:
            return None

        try:
            # Optimization: if seeking forward from current position, just decode
            # Otherwise, seek backwards which requires keyframe repositioning
            if frame_num < self._last_frame_num or frame_num > self._last_frame_num + 10:
                # Seek to frame using time_base
                # PyAV seeks using PTS (presentation timestamp)
                if self.vts:
                    # Use exact timestamp from VideoTimestamps
                    target_pts = int(self.vts[frame_num] * av.time_base / 1000.0)
                else:
                    # Estimate PTS from frame number
                    target_pts = int(frame_num / self.fps * av.time_base)

                # Seek to before target (to nearest keyframe)
                seek_pts = max(0, target_pts - int(0.5 * av.time_base))  # Seek 0.5s earlier
                self.container.seek(seek_pts, stream=self.video_stream)

            # Decode frames until we reach target
            current_frame = 0
            for packet in self.container.demux(self.video_stream):
                for frame in packet.decode():
                    # Get frame PTS and convert to frame number
                    if self.vts:
                        frame_pts_ms = frame.pts * frame.time_base * 1000.0
                        # Find which frame this is in VTS
                        for i in range(len(self.vts)):
                            if abs(self.vts[i] - frame_pts_ms) < 0.5:  # Within 0.5ms
                                current_frame = i
                                break
                    else:
                        # Estimate from PTS
                        current_frame = int(frame.pts * frame.time_base * self.fps)

                    if current_frame >= frame_num:
                        # Found the frame!
                        self._last_frame_num = frame_num

                        # Convert to numpy array (RGB)
                        frame_rgb = frame.to_ndarray(format='rgb24')
                        return frame_rgb

            # Couldn't find frame
            print(f"Warning: Could not find frame {frame_num}")
            return None

        except Exception as e:
            print(f"Error extracting frame {frame_num}: {e}")
            return None

    def __enter__(self):
        """Context manager entry."""
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
