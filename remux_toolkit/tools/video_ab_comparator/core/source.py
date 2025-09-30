# remux_toolkit/tools/video_ab_comparator/core/source.py

import subprocess
import json
from pathlib import Path
import cv2
import numpy as np
import imagehash
from PIL import Image
from typing import Optional, List
from .models import SourceInfo, StreamInfo

class VideoSource:
    """Represents a single video source for analysis."""

    def __init__(self, path: Path):
        self.path = path
        self.info: Optional[SourceInfo] = None

    def probe(self) -> bool:
        """Runs ffprobe on the source file to gather technical metadata."""
        try:
            cmd = [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_format', '-show_streams', str(self.path)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding='utf-8')
            data = json.loads(result.stdout)

            format_data = data.get('format', {})
            self.info = SourceInfo(
                path=str(self.path),
                format_name=format_data.get('format_name', 'N/A'),
                duration=float(format_data.get('duration', 0.0)),
                bitrate=format_data.get('bit_rate', 'N/A')
            )

            for stream_data in data.get('streams', []):
                codec_type = stream_data.get('codec_type')
                if codec_type in ['video', 'audio']:
                    stream = StreamInfo(
                        index=stream_data.get('index'),
                        codec_type=codec_type,
                        codec_name=stream_data.get('codec_name'),
                        resolution=f"{stream_data.get('width')}x{stream_data.get('height')}" if codec_type == 'video' else None,
                        dar=stream_data.get('display_aspect_ratio') if codec_type == 'video' else None,
                        colorspace=stream_data.get('color_space') if codec_type == 'video' else None,
                        frame_rate=stream_data.get('r_frame_rate') if codec_type == 'video' else None,
                        bitrate=stream_data.get('bit_rate')
                    )
                    self.info.streams.append(stream)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError):
            return False

    # ---- frame IO ----

    def _read_raw_frame(self, cmd, width: int, height: int) -> Optional[np.ndarray]:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        expected = width * height * 3
        if proc.returncode != 0 or len(proc.stdout) != expected:
            return None
        arr = np.frombuffer(proc.stdout, dtype='uint8')
        if arr.size != expected:
            return None
        return arr.reshape((height, width, 3))

    def get_frame(self, timestamp: float, *, accurate: bool = False) -> Optional[np.ndarray]:
        """
        Extract one frame at timestamp.
        - accurate=False  -> FAST seek (-ss before -i), may be off ~1 frame (good for bulk sampling)
        - accurate=True   -> EXACT seek (-ss after -i), slower (use for viewer/SSIM refinement)
        """
        video_stream = next((s for s in self.info.streams if s.codec_type == 'video'), None)
        if not video_stream or not video_stream.resolution:
            return None

        width, height = map(int, video_stream.resolution.split('x'))

        if not accurate:
            cmd = [
                'ffmpeg', '-nostdin', '-hide_banner', '-y', '-loglevel', 'error',
                '-ss', str(timestamp), '-i', str(self.path),
                '-vframes', '1', '-pix_fmt', 'bgr24',
                '-f', 'image2pipe', '-vcodec', 'rawvideo', '-'
            ]
            return self._read_raw_frame(cmd, width, height)

        cmd = [
            'ffmpeg', '-nostdin', '-hide_banner', '-y', '-loglevel', 'error',
            '-i', str(self.path),
            '-ss', str(timestamp),
            '-vframes', '1', '-pix_fmt', 'bgr24',
            '-f', 'image2pipe', '-vcodec', 'rawvideo', '-'
        ]
        return self._read_raw_frame(cmd, width, height)

    # ---- fingerprints (fast path) ----

    def generate_fingerprints(self, num_frames: int = 100) -> List[imagehash.ImageHash]:
        """Generates perceptual hashes for a sample of frames (FAST seek)."""
        fingerprints: List[imagehash.ImageHash] = []
        if not self.info or self.info.duration < 1:
            return []
        timestamps = np.linspace(self.info.duration * 0.1, self.info.duration * 0.9, num_frames)
        for ts in timestamps:
            frame = self.get_frame(ts, accurate=False)  # fast path
            if frame is not None:
                pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                fingerprints.append(imagehash.average_hash(pil))
        return fingerprints
