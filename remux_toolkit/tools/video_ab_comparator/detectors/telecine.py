# remux_toolkit/tools/video_ab_comparator/detectors/telecine.py

import cv2
import numpy as np
import subprocess
import re
from .base_detector import BaseDetector
from ..core.source import VideoSource

class GhostingDetector(BaseDetector):
    """Detects blended frames or ghosting from improper IVTC."""

    @property
    def issue_name(self) -> str:
        return "Ghosting / Blending"

    def run(self, source: VideoSource) -> dict:
        """Compares consecutive frames for low-motion edge overlap."""
        worst_score = -1
        worst_ts = source.info.duration / 2

        # Analyze three points in the video
        for i in range(1, 4):
            timestamp = source.info.duration * (i / 4.0)
            frame1 = source.get_frame(timestamp)

            # Get the next frame
            fps = eval(source.info.streams[0].frame_rate) if source.info.streams and source.info.streams[0].frame_rate else 24.0
            frame2 = source.get_frame(timestamp + (1.0 / fps))

            if frame1 is None or frame2 is None:
                continue

            # A blended frame is one where two frames are averaged together.
            # This results in edges from both frames being present at once,
            # creating a "ghost" or double-image effect.
            diff = cv2.absdiff(frame1, frame2)

            # In a normal scene, the difference between frames should be either
            # very low (no motion) or high and sharp (motion).
            # Ghosting creates a persistent, low-level difference across the frame.
            mean_diff = np.mean(diff)

            # A score is assigned if the difference is in a "ghosting range" - not zero, but not high motion.
            score = 0
            if 5 < mean_diff < 20:
                score = (mean_diff - 5) * 6.0

            if score > worst_score:
                worst_score = score
                worst_ts = timestamp

        if worst_score == -1:
             return {'score': 0, 'summary': 'Not detected', 'worst_frame_timestamp': worst_ts}

        return {
            'score': worst_score,
            'summary': f"Avg Frame Diff: { (worst_score/6.0)+5 :.2f}",
            'worst_frame_timestamp': worst_ts
        }

class CadenceDetector(BaseDetector):
    """Analyzes the consistency of the 3:2 pulldown pattern (telecine)."""

    @property
    def issue_name(self) -> str:
        return "Cadence Irregularity"

    def run(self, source: VideoSource) -> dict:
        """Uses ffmpeg's pullup filter to detect breaks in cadence."""
        if not source.info or not source.info.duration:
             return {'score': -1, 'summary': 'No duration info'}

        duration_to_scan = min(source.info.duration, 180) # Scan up to 3 minutes

        command = [
            "ffmpeg", "-v", "error", "-i", str(source.path),
            "-t", str(duration_to_scan),
            "-vf", "pullup,idet", "-an", "-f", "null", "-"
        ]

        try:
            result = subprocess.run(command, capture_output=True, text=True, stderr=subprocess.STDOUT)
            output = result.stdout + result.stderr

            # The pullup filter logs the number of "breaks" in the 3:2 pattern.
            # More breaks mean a more irregular, juddery playback.
            breaks = re.findall(r"pullup: drop score", output)
            num_breaks = len(breaks)

            # Score based on breaks per minute
            breaks_per_minute = (num_breaks / duration_to_scan) * 60 if duration_to_scan > 0 else 0

            score = min(100, breaks_per_minute * 5)
            summary = f"{num_breaks} breaks ({breaks_per_minute:.1f}/min)"

        except Exception as e:
            score = -1
            summary = f"Error: {e}"

        return {
            'score': score,
            'summary': summary,
            'worst_frame_timestamp': source.info.duration / 2 # No specific frame for this
        }
