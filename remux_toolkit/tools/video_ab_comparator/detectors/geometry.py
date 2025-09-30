# remux_toolkit/tools/video_ab_comparator/detectors/geometry.py

import cv2
import numpy as np
from .base_detector import BaseDetector
from ..core.source import VideoSource

class AspectRatioDetector(BaseDetector):
    """Detects black bars (letterboxing/pillarboxing) and aspect ratio issues."""

    @property
    def issue_name(self) -> str:
        return "Aspect Ratio"

    def run(self, source: VideoSource) -> dict:
        """Finds the active video area and compares it to the container's aspect ratio."""
        timestamp = source.info.duration * 0.5
        frame = source.get_frame(timestamp)

        if frame is None:
            return {'score': -1, 'summary': 'Frame extract failed'}

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # Threshold the image to find non-black areas
        _, thresh = cv2.threshold(gray, 15, 255, cv2.THRESH_BINARY)

        # Find contours of the non-black areas
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        if not contours:
            return {'score': 90, 'summary': 'Empty frame detected', 'worst_frame_timestamp': timestamp}

        # Find the bounding box of the largest contour, which is the active video area
        x, y, w, h = cv2.boundingRect(np.concatenate(contours))

        container_w, container_h = gray.shape[1], gray.shape[0]

        # Check for black bars
        if w < container_w or h < container_h:
            summary = f"Pillar/Letterboxed ({w}x{h})"
            # High score if there are significant black bars
            score = ((container_w - w) + (container_h - h)) / (container_w + container_h) * 100
        else:
            summary = "Fullscreen"
            score = 0

        return {
            'score': score,
            'summary': summary,
            'worst_frame_timestamp': timestamp
        }
