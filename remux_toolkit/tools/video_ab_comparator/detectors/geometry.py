# remux_toolkit/tools/video_ab_comparator/detectors/geometry.py

import cv2
import numpy as np
from .base_detector import BaseDetector
from ..core.source import VideoSource
from typing import List

class AspectRatioDetector(BaseDetector):
    @property
    def issue_name(self) -> str: return "Aspect Ratio"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        if not frame_list:
            return {'score': -1, 'summary': 'Frame extract failed'}

        frame = frame_list[len(frame_list) // 2] # Use middle frame

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(gray, 15, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        if not contours:
            return {'score': 90, 'summary': 'Empty frame detected', 'worst_frame_timestamp': 0.0}

        x, y, w, h = cv2.boundingRect(np.concatenate(contours))
        container_w, container_h = gray.shape[1], gray.shape[0]

        if w < container_w or h < container_h:
            summary = f"Pillar/Letterboxed ({w}x{h})"
            score = ((container_w - w) + (container_h - h)) / (container_w + container_h) * 100
        else:
            summary, score = "Fullscreen", 0

        return {'score': score, 'summary': summary, 'worst_frame_timestamp': 0.0}
