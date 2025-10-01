# remux_toolkit/tools/video_ab_comparator/core/source.py - Fixed frame extraction methods

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
import tempfile
import os

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
    def __init__(self, source: Union[Path, bytes]):
        self.source = source
        self.path_name = str(source) if isinstance(source, Path) else "in-memory-chunk"
        self.info: Optional[SourceInfo] = None
        self.path = str(source) if isinstance(source, Path) else None

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
            self.info = SourceInfo(
                path=self.path_name,
                format_name=format_data.get('format_name', 'N/A'),
                duration=float(format_data.get('duration', 0.0)),
                bitrate=format_data.get('bit_rate', 'N/A')
            )

            # Process all streams
            for s_data in data.get('streams', []):
                codec_type = s_data.get('codec_type')

                if codec_type == 'video':
                    stream = StreamInfo(
                        index=s_data.get('index'),
                        codec_type='video',
                        codec_name=s_data.get('codec_name'),
                        resolution=f"{s_data.get('width')}x{s_data.get('height')}",
                        dar=s_data.get('display_aspect_ratio'),
                        colorspace=s_data.get('color_space'),
                        frame_rate=s_data.get('r_frame_rate'),
                        fps=_safe_fraction_to_fps(s_data.get('avg_frame_rate', s_data.get('r_frame_rate'))),
                        frame_count=int(s_data.get('nb_frames', 0)),
                        bitrate=s_data.get('bit_rate')
                    )
                    self.info.streams.append(stream)
                    if not self.info.video_stream:
                        self.info.video_stream = stream

                elif codec_type == 'audio':
                    stream = StreamInfo(
                        index=s_data.get('index'),
                        codec_type='audio',
                        codec_name=s_data.get('codec_name'),
                        bitrate=s_data.get('bit_rate')
                    )
                    self.info.streams.append(stream)

            if self.info.video_stream and self.info.video_stream.frame_count == 0 and self.info.duration > 0:
                self.info.video_stream.frame_count = int(self.info.duration * self.info.video_stream.fps)

            return True

        except Exception as e:
            print(f"ffprobe failed for {self.path_name}: {e}")
            return False

    def get_frame_iterator(self, start_time: float = 0.0, scan_duration: Optional[float] = None) -> Iterator[np.ndarray]:
        """Iterate through frames with multiple fallback methods."""

        # Method 1: Try PyAV first (fastest if it works)
        frames_yielded = 0
        try:
            source_obj = io.BytesIO(self.source) if isinstance(self.source, bytes) else str(self.source)
            with av.open(source_obj) as container:
                stream = container.streams.video[0]
                if stream:
                    end_time = (start_time + scan_duration) if scan_duration is not None else float('inf')

                    if start_time > 0:
                        try:
                            container.seek(int(start_time * 1_000_000), backward=True, any_frame=False, stream=stream)
                        except:
                            container.seek(int(start_time * 1_000_000), backward=True, any_frame=True, stream=stream)

                    for frame in container.decode(stream):
                        try:
                            frame_ts = float(frame.pts * stream.time_base) if frame.pts else 0
                            if frame_ts >= start_time:
                                if frame_ts <= end_time:
                                    img = frame.to_ndarray(format='bgr24')
                                    if img is not None:
                                        frames_yielded += 1
                                        yield img
                                else:
                                    break
                        except Exception:
                            continue

            if frames_yielded > 0:
                return  # PyAV worked, we're done

        except Exception as e:
            print(f"PyAV iterator failed: {e}, trying OpenCV...")

        # Method 2: Try OpenCV (more compatible)
        if isinstance(self.source, Path):
            try:
                cap = cv2.VideoCapture(str(self.source))
                if cap.isOpened():
                    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
                    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

                    # Calculate frame range
                    start_frame = int(start_time * fps)
                    if scan_duration:
                        end_frame = min(start_frame + int(scan_duration * fps), total_frames)
                    else:
                        end_frame = total_frames

                    # Seek to start frame
                    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

                    for frame_idx in range(start_frame, end_frame):
                        ret, frame = cap.read()
                        if ret and frame is not None:
                            frames_yielded += 1
                            yield frame
                        else:
                            break

                    cap.release()

                if frames_yielded > 0:
                    return  # OpenCV worked

            except Exception as e:
                print(f"OpenCV iterator failed: {e}, trying FFmpeg...")

        # Method 3: FFmpeg frame extraction (slowest but most compatible)
        if isinstance(self.source, Path) and frames_yielded == 0:
            try:
                # Create temporary directory for frames
                with tempfile.TemporaryDirectory() as tmpdir:
                    # Extract frames using FFmpeg
                    cmd = [
                        'ffmpeg', '-v', 'quiet',
                        '-ss', str(start_time),
                        '-i', str(self.source),
                        '-t', str(scan_duration) if scan_duration else '2',
                        '-vf', 'fps=10',  # Extract 10 fps
                        '-frame_pts', '1',
                        os.path.join(tmpdir, 'frame_%04d.png')
                    ]

                    subprocess.run(cmd, check=True, timeout=10)

                    # Read extracted frames
                    frame_files = sorted([f for f in os.listdir(tmpdir) if f.endswith('.png')])
                    for frame_file in frame_files:
                        frame_path = os.path.join(tmpdir, frame_file)
                        frame = cv2.imread(frame_path)
                        if frame is not None:
                            yield frame

            except Exception as e:
                print(f"FFmpeg frame extraction also failed: {e}")

    def get_frame(self, timestamp: float, *, accurate: bool = False) -> Optional[np.ndarray]:
        """Get a single frame with multiple fallback methods."""

        # Method 1: Try PyAV
        try:
            source_obj = io.BytesIO(self.source) if isinstance(self.source, bytes) else str(self.source)
            with av.open(source_obj) as container:
                stream = container.streams.video[0]
                if stream:
                    seek_ts = int(timestamp * 1_000_000)

                    if accurate:
                        try:
                            container.seek(seek_ts, backward=True, any_frame=False, stream=stream)
                        except:
                            container.seek(seek_ts, backward=True, any_frame=True, stream=stream)

                        for frame in container.decode(stream):
                            if frame.pts is not None:
                                frame_ts = float(frame.pts * stream.time_base)
                                if frame_ts >= timestamp:
                                    return frame.to_ndarray(format='bgr24')
                    else:
                        container.seek(seek_ts, backward=True, any_frame=True, stream=stream)
                        for frame in container.decode(stream):
                            img = frame.to_ndarray(format='bgr24')
                            if img is not None:
                                return img

        except Exception as e:
            print(f"PyAV get_frame failed at {timestamp}s: {e}")

        # Method 2: OpenCV fallback
        if isinstance(self.source, Path):
            try:
                cap = cv2.VideoCapture(str(self.source))
                if cap.isOpened():
                    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
                    frame_number = int(timestamp * fps)
                    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
                    ret, frame = cap.read()
                    cap.release()

                    if ret and frame is not None:
                        return frame

            except Exception as e:
                print(f"OpenCV get_frame failed: {e}")

        # Method 3: FFmpeg single frame extraction
        if isinstance(self.source, Path):
            try:
                cmd = [
                    'ffmpeg', '-v', 'quiet',
                    '-ss', str(timestamp),
                    '-i', str(self.source),
                    '-frames:v', '1',
                    '-f', 'image2pipe',
                    '-pix_fmt', 'bgr24',
                    '-vcodec', 'rawvideo',
                    '-'
                ]

                result = subprocess.run(cmd, capture_output=True, timeout=5)
                if result.stdout:
                    # Parse raw video frame
                    if self.info and self.info.video_stream:
                        w, h = map(int, self.info.video_stream.resolution.split('x'))
                        frame = np.frombuffer(result.stdout, dtype=np.uint8)
                        if len(frame) == w * h * 3:
                            frame = frame.reshape((h, w, 3))
                            return frame

            except Exception as e:
                print(f"FFmpeg get_frame failed: {e}")

        return None

    def generate_fingerprints(self, num_frames: int = 100) -> List[imagehash.ImageHash]:
        """Generate perceptual hashes for alignment."""
        fingerprints: List[imagehash.ImageHash] = []
        if not self.info or self.info.duration < 1:
            return []

        timestamps = np.linspace(self.info.duration * 0.1, self.info.duration * 0.9, num_frames)
        for ts in timestamps:
            frame = self.get_frame(ts, accurate=False)
            if frame is not None:
                try:
                    pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                    fingerprints.append(imagehash.average_hash(pil))
                except:
                    continue
        return fingerprints
