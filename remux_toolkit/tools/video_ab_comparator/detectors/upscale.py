# remux_toolkit/tools/video_ab_comparator/detectors/upscale.py

from .base_detector import BaseDetector
from ..core.source import VideoSource
import cv2
import numpy as np
import subprocess

class UpscaleDetector(BaseDetector):
    """Detects if a video source appears to be upscaled from a lower resolution."""

    @property
    def issue_name(self) -> str:
        return "Upscaled Video"

    def run(self, source: VideoSource) -> dict:
        """Analyzes video resolution and high-frequency detail."""
        if not source.info or not source.info.streams:
            return {'score': -1, 'summary': 'No video stream found'}

        video_stream = next((s for s in source.info.streams if s.codec_type == 'video'), None)
        if not video_stream or not video_stream.resolution:
            return {'score': -1, 'summary': 'Could not determine resolution'}

        width, height = map(int, video_stream.resolution.split('x'))

        # Simple check for common upscale patterns (e.g., DVD to 1080p)
        if height < 720:
             return {'score': 0, 'summary': f'Native SD ({height}p)'}

        if height >= 1080:
            # For HD content, analyze high-frequency detail.
            # A true HD source will have more detail than an upscaled one.
            try:
                # Extract a sample frame from the middle of the video
                frame = self._extract_frame(source.path, source.info.duration / 2, width, height)
                if frame is None:
                    return {'score': -1, 'summary': 'Failed to extract frame'}

                # Convert to grayscale and compute Laplacian variance
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()

                # These thresholds are heuristics and may need tuning
                if lap_var < 100:
                    return {'score': 90, 'summary': f'Likely Upscaled (Laplacian Variance: {lap_var:.2f})'}
                else:
                    return {'score': 10, 'summary': f'Likely Native HD (Laplacian Variance: {lap_var:.2f})'}

            except Exception as e:
                return {'score': -1, 'summary': f'Error during analysis: {e}'}

        return {'score': 0, 'summary': 'Standard Definition'}

    def _extract_frame(self, path, timestamp, width, height):
        """Extracts a single frame using ffmpeg."""
        cmd = [
            'ffmpeg', '-ss', str(timestamp), '-i', str(path),
            '-vframes', '1', '-f', 'image2pipe', '-pix_fmt', 'bgr24',
            '-vcodec', 'rawvideo', '-'
        ]
        proc = subprocess.run(cmd, capture_output=True, check=True)
        frame = np.frombuffer(proc.stdout, dtype='uint8').reshape((height, width, 3))
        return frame
