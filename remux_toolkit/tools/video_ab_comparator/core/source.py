# remux_toolkit/tools/video_ab_comparator/core/source.py

import subprocess
import json
from pathlib import Path
import cv2
import numpy as np
import imagehash
from PIL import Image
from typing import Optional, List, Iterator, Union
from .models import SourceInfo, StreamInfo
import tempfile
import os

# Optional PyAV support for frame-accurate seeking
try:
    from .pyav_source import PyAVFrameExtractor, HAS_PYAV
except ImportError:
    HAS_PYAV = False
    PyAVFrameExtractor = None

def _safe_fraction_to_fps(r_frame_rate: Optional[str]) -> float:
    if not r_frame_rate or "/" not in str(r_frame_rate):
        try: return float(r_frame_rate) if r_frame_rate else 24.0
        except (ValueError, TypeError): return 24.0
    try:
        n, d = map(float, str(r_frame_rate).split('/'))
        return n / d if d != 0 else 24.0
    except Exception:
        return 24.0

class VideoSource:
    def __init__(self, source: Union[Path, bytes], use_pyav: bool = True):
        self.source = source
        self.path_name = str(source) if isinstance(source, Path) else "in-memory-chunk"
        self.info: Optional[SourceInfo] = None
        self.path = str(source) if isinstance(source, Path) else None

        # PyAV extractor (optional, for frame-accurate seeking)
        self.pyav_extractor: Optional[PyAVFrameExtractor] = None
        self._use_pyav = use_pyav and HAS_PYAV and isinstance(source, Path)

    def probe(self) -> bool:
        try:
            cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', str(self.source)]
            result = subprocess.run(cmd, capture_output=True, check=True, text=True)
            data = json.loads(result.stdout)
            format_data = data.get('format', {})
            self.info = SourceInfo(
                path=self.path_name,
                format_name=format_data.get('format_name', 'N/A'),
                duration=float(format_data.get('duration', 0.0)),
                bitrate=format_data.get('bit_rate', 'N/A')
            )
            for s_data in data.get('streams', []):
                if s_data.get('codec_type') == 'video':
                    stream = StreamInfo(
                        index=s_data.get('index'), codec_type='video', codec_name=s_data.get('codec_name'),
                        resolution=f"{s_data.get('width')}x{s_data.get('height')}", dar=s_data.get('display_aspect_ratio'),
                        colorspace=s_data.get('color_space'), frame_rate=s_data.get('r_frame_rate'),
                        fps=_safe_fraction_to_fps(s_data.get('avg_frame_rate', s_data.get('r_frame_rate'))),
                        frame_count=int(s_data.get('nb_frames', 0)), bitrate=s_data.get('bit_rate')
                    )
                    self.info.streams.append(stream)
                    if not self.info.video_stream: self.info.video_stream = stream
                elif s_data.get('codec_type') == 'audio':
                    self.info.streams.append(StreamInfo(index=s_data.get('index'), codec_type='audio', codec_name=s_data.get('codec_name'), bitrate=s_data.get('bit_rate')))
            if self.info.video_stream and self.info.video_stream.frame_count == 0 and self.info.duration > 0:
                self.info.video_stream.frame_count = int(self.info.duration * self.info.video_stream.fps)
            return True
        except Exception as e:
            print(f"ffprobe failed for {self.path_name}: {e}")
            return False

    def get_frame_iterator(self, start_time: float = 0.0, scan_duration: float = 2.0) -> Iterator[np.ndarray]:
        if not isinstance(self.source, Path): return
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                cmd = [
                    'ffmpeg', '-v', 'quiet', '-ss', str(start_time), '-i', str(self.source),
                    '-t', str(scan_duration), '-vf', 'fps=10', os.path.join(tmpdir, 'frame_%04d.png')
                ]
                subprocess.run(cmd, check=True, timeout=20)
                frame_files = sorted([f for f in os.listdir(tmpdir) if f.endswith('.png')])
                for frame_file in frame_files:
                    frame = cv2.imread(os.path.join(tmpdir, frame_file))
                    if frame is not None: yield frame
        except Exception as e:
            print(f"FFmpeg frame iterator failed: {e}")

    def get_frames_at_exact_timestamps(self, timestamps: List[float]) -> List[np.ndarray]:
        """
        Extract frames at exact timestamps using frame-accurate seeking.

        Uses VideoTimestamps-provided exact timestamps for frame-perfect extraction.
        Each frame is extracted individually with accurate seeking to ensure precision.

        Args:
            timestamps: List of exact timestamps (in seconds) from VideoTimestamps

        Returns:
            List of frames as numpy arrays
        """
        if not isinstance(self.source, Path) or not self.info or not self.info.video_stream:
            return []

        frames = []
        for ts in timestamps:
            # Use accurate seeking for each frame
            # Format timestamp with high precision (microseconds)
            frame = self.get_frame(ts, accurate=True)
            if frame is not None:
                frames.append(frame)

        return frames

    def initialize_pyav(self) -> bool:
        """
        Initialize PyAV extractor for frame-accurate seeking.

        Returns:
            True if successful, False otherwise
        """
        if not self._use_pyav or self.pyav_extractor:
            return False

        try:
            self.pyav_extractor = PyAVFrameExtractor(str(self.source))
            success = self.pyav_extractor.open()
            if success:
                print(f"PyAV extractor initialized for {self.path_name}")
            return success
        except Exception as e:
            print(f"PyAV initialization failed: {e}")
            self.pyav_extractor = None
            return False

    def close_pyav(self):
        """Close PyAV extractor if open."""
        if self.pyav_extractor:
            self.pyav_extractor.close()
            self.pyav_extractor = None

    def close(self):
        """Close all resources and release file handles."""
        self.close_pyav()
        # Clear other resources
        self.info = None
        # No other file handles to close - FFmpeg is subprocess-based

    def get_frame(self, timestamp: float, *, accurate: bool = False) -> Optional[np.ndarray]:
        if not isinstance(self.source, Path) or not self.info or not self.info.video_stream:
            return None

        # Try PyAV first if available and accurate seeking requested
        if accurate and self.pyav_extractor:
            try:
                frame_rgb = self.pyav_extractor.get_frame_at_timestamp(timestamp)
                if frame_rgb is not None:
                    # Convert RGB to BGR for consistency with FFmpeg output
                    return cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
            except Exception as e:
                print(f"PyAV extraction failed, falling back to FFmpeg: {e}")

        # FFmpeg fallback (original implementation)
        try:
            w, h = map(int, self.info.video_stream.resolution.split('x'))
            cmd = ['ffmpeg']
            if accurate: cmd.extend(['-ss', str(timestamp)])
            else: cmd.extend(['-ss', str(timestamp), '-skip_frame', 'nokey'])

            cmd.extend(['-i', str(self.source), '-vframes', '1', '-f', 'image2pipe',
                        '-pix_fmt', 'bgr24', '-vcodec', 'rawvideo', '-'])

            result = subprocess.run(cmd, capture_output=True, timeout=15)

            if result.returncode != 0 or not result.stdout:
                print(f"FFmpeg get_frame failed at {timestamp}s: {result.stderr.decode('utf-8', 'ignore')}")
                return None

            frame_size = w * h * 3
            if len(result.stdout) >= frame_size:
                frame = np.frombuffer(result.stdout, dtype=np.uint8, count=frame_size).reshape((h, w, 3))
                return frame
            else:
                print(f"FFmpeg get_frame failed: incorrect frame size. Expected {frame_size}, got {len(result.stdout)}")
                return None
        except Exception as e:
            print(f"Exception in get_frame at {timestamp}s: {e}")
            return None

    def generate_fingerprints(self, num_frames: int = 100) -> List[imagehash.ImageHash]:
        fingerprints: List[imagehash.ImageHash] = []
        if not self.info or self.info.duration < 1: return []
        timestamps = np.linspace(self.info.duration * 0.1, self.info.duration * 0.9, num_frames)
        for ts in timestamps:
            frame = self.get_frame(ts, accurate=False)
            if frame is not None:
                try:
                    pil = Image.fromarray(cv2.cvtColor(frame, cv.COLOR_BGR2RGB))
                    fingerprints.append(imagehash.average_hash(pil))
                except: continue
        return fingerprints
