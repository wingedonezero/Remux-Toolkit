# remux_toolkit/tools/video_ab_comparator/detectors/compression.py
import cv2
import numpy as np
from .base_detector import BaseDetector
from ..core.source import VideoSource
from typing import List

class BlockingDetector(BaseDetector):
    @property
    def issue_name(self) -> str: return "Compression Blocking"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        v_stream = source.info.video_stream
        if not v_stream: return {'score': -1}

        scores, frame_idx = [], 0
        threshold = 1.0

        for frame in frame_list:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            height, width = gray.shape
            grad_h = np.abs(cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3))
            grad_v = np.abs(cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3))
            h_strength = np.sum([grad_h[:, j] for j in range(0, width, 8)])
            v_strength = np.sum([grad_v[j, :] for j in range(0, height, 8)])

            strength = (h_strength + v_strength) / (height * width * 0.01)
            score = min(100, max(0, (strength - 15.0) * 6.0))
            scores.append(score)
            frame_idx += 1

        if not scores: return {'score': 0, 'summary': 'Not detected'}

        scores_arr = np.array(scores)
        avg_score = np.mean(scores_arr)
        peak_score = np.max(scores_arr)
        occurrences = np.sum(scores_arr > threshold)
        occurrence_rate = (occurrences / len(scores_arr)) * 100 if scores else 0
        worst_idx = np.argmax(scores_arr)
        worst_ts = worst_idx / v_stream.fps if v_stream.fps > 0 else 0

        return {
            'score': avg_score,
            'summary': f"Avg: {avg_score:.1f} | Peak: {peak_score:.1f} | Occ: {occurrence_rate:.1f}%",
            'worst_frame_timestamp': worst_ts
        }
