# remux_toolkit/tools/video_ab_comparator/detectors/compression.py

import cv2
import numpy as np
from .base_detector import BaseDetector
from ..core.source import VideoSource

class BlockingDetector(BaseDetector):
    """Detects 8x8 block artifacts from video compression."""

    @property
    def issue_name(self) -> str:
        return "Compression Blocking"

    def run(self, source: VideoSource) -> dict:
        """Analyzes multiple frames and reports the worst one."""
        worst_score = -1
        worst_ts = source.info.duration / 2

        for i in range(3):
            timestamp = source.info.duration * (i + 1) / 4.0
            frame = source.get_frame(timestamp)
            if frame is None:
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            height, width = gray.shape

            grad_h = np.abs(cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3))
            grad_v = np.abs(cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3))

            h_block_strength = np.sum([grad_h[:, i] for i in range(0, width, 8)])
            v_block_strength = np.sum([grad_v[i, :] for i in range(0, height, 8)])

            total_strength = (h_block_strength + v_block_strength) / (height * width * 0.01)
            score = min(100, max(0, (total_strength - 10) * 10))

            if score > worst_score:
                worst_score = score
                worst_ts = timestamp

        if worst_score == -1:
            return {'score': -1, 'summary': 'Frame extract failed'}

        return {
            'score': worst_score,
            'summary': f"Strength: {(worst_score/10)+10:.2f}",
            'worst_frame_timestamp': worst_ts
        }
