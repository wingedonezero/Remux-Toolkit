# remux_toolkit/tools/video_ab_comparator/core/source.py

import subprocess
import json
from pathlib import Path
import cv2
import numpy as np
import imagehash
from PIL import Image
from typing import Optional, List, Iterator
from .models import SourceInfo, StreamInfo

def _safe_fraction_to_fps(r_frame_rate: Optional[str]) -> float:
    if not r_frame_rate or "/" not in str(r_frame_rate):
        try: return float(r_frame_rate)
        except (ValueError, TypeError): return 24.0
    try:
        n, d = map(float, str(r_frame_rate).split('/'))
        return n / d if d != 0 else 24.0
    except Exception:
        return 24.0

class VideoSource:
    def __init__(self, path: Path):
        self.path = path
        self.info: Optional[SourceInfo] = None
        self._ffmpeg_proc = None
        self.frame_size = 0

    def __enter__(self):
        if not self.info or not self.info.video_stream: return None
        v_stream = self.info.video_stream
        width, height = map(int, v_stream.resolution.split('x'))
        self.frame_size = width * height * 3
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', str(self.path), '-pix_fmt', 'bgr24', '-f', 'rawvideo', '-']
        self._ffmpeg_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._ffmpeg_proc:
            self._ffmpeg_proc.kill()
            self._ffmpeg_proc.wait()

    def read_frame(self) -> Optional[np.ndarray]:
        if not self._ffmpeg_proc or self.frame_size == 0: return None
        frame_bytes = self._ffmpeg_proc.stdout.read(self.frame_size)
        if len(frame_bytes) == 0: return None
        if len(frame_bytes) != self.frame_size: return None
        v_stream = self.info.video_stream
        width, height = map(int, v_stream.resolution.split('x'))
        return np.frombuffer(frame_bytes, dtype='uint8').reshape((height, width, 3))

    def get_frame_iterator(self, start_time: float, scan_duration: float = 2.0) -> Iterator[np.ndarray]:
        """A highly efficient generator to scan a small video segment."""
        v_stream = self.info.video_stream
        if not v_stream: return

        width, height = map(int, v_stream.resolution.split('x'))
        frame_size = width * height * 3

        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-ss', str(start_time), '-i', str(self.path),
            '-t', str(scan_duration),
            '-pix_fmt', 'bgr24', '-f', 'rawvideo', '-'
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        while True:
            frame_bytes = proc.stdout.read(frame_size)
            if not frame_bytes or len(frame_bytes) != frame_size:
                break
            yield np.frombuffer(frame_bytes, dtype='uint8').reshape((height, width, 3))

        proc.kill()
        proc.wait()

    def probe(self) -> bool:
        try:
            cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', str(self.path)]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding='utf-8')
            data = json.loads(result.stdout)
            format_data = data.get('format', {})
            self.info = SourceInfo(path=str(self.path), format_name=format_data.get('format_name', 'N/A'), duration=float(format_data.get('duration', 0.0)), bitrate=format_data.get('bit_rate', 'N/A'))
            video_streams = [s for s in data.get('streams', []) if s.get('codec_type') == 'video']
            audio_streams = [s for s in data.get('streams', []) if s.get('codec_type') == 'audio']
            if video_streams:
                s_data = video_streams[0]
                stream = StreamInfo(index=s_data.get('index'), codec_type='video', codec_name=s_data.get('codec_name'), resolution=f"{s_data.get('width')}x{s_data.get('height')}", dar=s_data.get('display_aspect_ratio'), colorspace=s_data.get('color_space'), frame_rate=s_data.get('r_frame_rate'), fps=_safe_fraction_to_fps(s_data.get('avg_frame_rate')), frame_count=int(s_data.get('nb_frames', 0)), bitrate=s_data.get('bit_rate'))
                self.info.streams.append(stream)
                self.info.video_stream = stream
            for s_data in audio_streams:
                self.info.streams.append(StreamInfo(index=s_data.get('index'), codec_type='audio', codec_name=s_data.get('codec_name'), bitrate=s_data.get('bit_rate')))
            if self.info.video_stream and self.info.video_stream.frame_count == 0:
                self.info.video_stream.frame_count = int(self.info.duration * self.info.video_stream.fps)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as e:
            print(f"ffprobe failed for {self.path}: {e}")
            return False

    def get_frame(self, timestamp: float, *, accurate: bool = False) -> Optional[np.ndarray]:
        if not self.info or not self.info.video_stream or not self.info.video_stream.resolution: return None
        width, height = map(int, self.info.video_stream.resolution.split('x'))
        cmd_base = ['ffmpeg', '-nostdin', '-hide_banner', '-y', '-loglevel', 'error']
        cmd_in = ['-i', str(self.path)]
        cmd_seek = ['-ss', str(timestamp)]
        cmd_out = ['-vframes', '1', '-pix_fmt', 'bgr24', '-f', 'image2pipe', '-vcodec', 'rawvideo', '-']
        cmd = cmd_base + cmd_seek + cmd_in + cmd_out if not accurate else cmd_base + cmd_in + cmd_seek + cmd_out
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        expected = width * height * 3
        if proc.returncode != 0 or len(proc.stdout) != expected: return None
        return np.frombuffer(proc.stdout, dtype='uint8').reshape((height, width, 3))

    def generate_fingerprints(self, num_frames: int = 100) -> List[imagehash.ImageHash]:
        fingerprints: List[imagehash.ImageHash] = []
        if not self.info or self.info.duration < 1: return []
        timestamps = np.linspace(self.info.duration * 0.1, self.info.duration * 0.9, num_frames)
        for ts in timestamps:
            frame = self.get_frame(ts, accurate=False)
            if frame is not None:
                # FIX was here: cv.COLOR_BGR2RGB -> cv2.COLOR_BGR2RGB
                pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                fingerprints.append(imagehash.average_hash(pil))
        return fingerprints
