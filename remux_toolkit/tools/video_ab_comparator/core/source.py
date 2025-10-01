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
import av
import io

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
    def __init__(self, source: Union[Path, bytes]):
        self.source = source
        self.path_name = str(source) if isinstance(source, Path) else "in-memory-chunk"
        self.info: Optional[SourceInfo] = None

    def probe(self) -> bool:
        try:
            cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams']
            input_data = None
            if isinstance(self.source, Path):
                cmd.append(str(self.source))
            else:
                cmd.append('-')
                input_data = self.source

            result = subprocess.run(cmd, input=input_data, capture_output=True, check=True)
            output = result.stdout.decode('utf-8') if isinstance(result.stdout, bytes) else result.stdout
            data = json.loads(output)

            format_data = data.get('format', {})
            self.info = SourceInfo(path=self.path_name, format_name=format_data.get('format_name', 'N/A'), duration=float(format_data.get('duration', 0.0)), bitrate=format_data.get('bit_rate', 'N/A'))

            video_streams = [s for s in data.get('streams', []) if s.get('codec_type') == 'video']
            if video_streams:
                s_data = video_streams[0]
                stream = StreamInfo(index=s_data.get('index'), codec_type='video', codec_name=s_data.get('codec_name'), resolution=f"{s_data.get('width')}x{s_data.get('height')}", dar=s_data.get('display_aspect_ratio'), colorspace=s_data.get('color_space'), frame_rate=s_data.get('r_frame_rate'), fps=_safe_fraction_to_fps(s_data.get('avg_frame_rate')), frame_count=int(s_data.get('nb_frames', 0)), bitrate=s_data.get('bit_rate'))
                self.info.streams.append(stream)
                self.info.video_stream = stream

            if self.info.video_stream and self.info.video_stream.frame_count == 0 and self.info.duration > 0:
                self.info.video_stream.frame_count = int(self.info.duration * self.info.video_stream.fps)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError, IndexError) as e:
            print(f"ffprobe failed for {self.path_name}: {e}")
            return False

    def get_frame_iterator(self, start_time: float = 0.0, scan_duration: Optional[float] = None) -> Iterator[np.ndarray]:
        source_obj = io.BytesIO(self.source) if isinstance(self.source, bytes) else str(self.source)
        try:
            with av.open(source_obj) as container:
                stream = container.streams.video[0]
                end_time = (start_time + scan_duration) if scan_duration is not None else float('inf')

                if start_time > 0:
                    container.seek(int(start_time * 1_000_000), backward=True, any_frame=False, stream=stream)

                for frame in container.decode(stream):
                    frame_ts = frame.pts * stream.time_base
                    if frame_ts >= start_time:
                        if frame_ts <= end_time:
                            yield frame.to_ndarray(format='bgr24')
                        else:
                            break
        except Exception as e: # Broadened exception catch
            print(f"PyAV iterator failed for {self.path_name}: {e}")
            return

    def get_frame(self, timestamp: float, *, accurate: bool = False) -> Optional[np.ndarray]:
        source_obj = io.BytesIO(self.source) if isinstance(self.source, bytes) else str(self.source)
        try:
            with av.open(source_obj) as container:
                stream = container.streams.video[0]

                if accurate:
                    container.seek(int(timestamp * 1_000_000), backward=True, any_frame=False, stream=stream)
                    for frame in container.decode(stream):
                        if frame.pts * stream.time_base >= timestamp:
                            return frame.to_ndarray(format='bgr24')
                else:
                    container.seek(int(timestamp * 1_000_000), backward=True, any_frame=True, stream=stream)
                    # --- FIX ---
                    # Use a loop to gracefully handle cases where seek lands at the end
                    for frame in container.decode(stream):
                        return frame.to_ndarray(format='bgr24')
                    # --- END FIX ---
        except Exception as e: # --- FIX: Corrected exception handling ---
            print(f"PyAV failed to get frame at {timestamp}s for {self.path_name}: {e}")
        return None

    def generate_fingerprints(self, num_frames: int = 100) -> List[imagehash.ImageHash]:
        fingerprints: List[imagehash.ImageHash] = []
        if not self.info or self.info.duration < 1: return []

        timestamps = np.linspace(self.info.duration * 0.1, self.info.duration * 0.9, num_frames)
        for ts in timestamps:
            frame = self.get_frame(ts, accurate=False)
            if frame is not None:
                pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                fingerprints.append(imagehash.average_hash(pil))
        return fingerprints
