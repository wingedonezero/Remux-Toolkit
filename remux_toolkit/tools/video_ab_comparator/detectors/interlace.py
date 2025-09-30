# remux_toolkit/tools/video_ab_comparator/detectors/interlace.py

import subprocess
import re
from .base_detector import BaseDetector
from ..core.source import VideoSource

class CombingDetector(BaseDetector):
    """Detects combing artifacts from improper deinterlacing."""

    @property
    def issue_name(self) -> str:
        return "Interlace Combing"

    def run(self, source: VideoSource) -> dict:
        """Runs ffmpeg's idet filter to find combed frames."""
        duration = source.info.duration
        if duration < 10:
            return {'score': 0, 'summary': 'Video too short'}

        # Analyze a 60-second segment from the first third of the video
        start_time = duration / 3
        command = [
            "ffmpeg", "-ss", str(start_time), "-t", "60",
            "-i", str(source.path),
            "-vf", "idet", "-an", "-f", "null", "-"
        ]

        try:
            result = subprocess.run(command, capture_output=True, text=True, stderr=subprocess.STDOUT)
            output = result.stdout + result.stderr

            # Parse the idet summary output
            multi_frame_tff = float(re.search(r"TFF:\s+(\d+)", output).group(1))
            multi_frame_bff = float(re.search(r"BFF:\s+(\d+)", output).group(1))
            single_frame_tff = float(re.search(r"Single frame TFF:\s+(\d+)", output).group(1))
            single_frame_bff = float(re.search(r"Single frame BFF:\s+(\d+)", output).group(1))

            total_interlaced = multi_frame_tff + multi_frame_bff
            total_frames = total_interlaced + single_frame_tff + single_frame_bff

            if total_frames == 0:
                return {'score': 0, 'summary': 'No frames analyzed'}

            interlaced_percent = (total_interlaced / total_frames) * 100

            # Score is directly the percentage of interlaced frames
            score = interlaced_percent
            summary = f"{interlaced_percent:.1f}% combed"

            return {'score': score, 'summary': summary}

        except Exception as e:
            return {'score': -1, 'summary': f'Error: {e}'}
