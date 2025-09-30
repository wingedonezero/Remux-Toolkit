# remux_toolkit/tools/video_ab_comparator/detectors/noise.py

import cv2
import numpy as np
from .base_detector import BaseDetector
from ..core.source import VideoSource

class DNRDetector(BaseDetector):
    @property
    def issue_name(self) -> str: return "Over-DNR / Waxiness"

    def run(self, source: VideoSource) -> dict:
        v_stream = source.info.video_stream
        if not v_stream: return {'score': -1}

        scores, frame_idx = [], 0
        threshold = 1.0

        with source as s:
            if not s: return {'score': -1}
            while (frame := s.read_frame()) is not None:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                edges = cv2.Canny(gray, 100, 200)
                texture_mask = cv2.bitwise_not(cv2.dilate(edges, np.ones((5, 5), np.uint8)))

                if np.sum(texture_mask) == 0:
                    texture_detail = 10
                else:
                    texture_detail = np.std(cv2.Laplacian(gray, cv2.CV_64F), where=(texture_mask > 0))

                # FORMULA RECALIBRATED: Less sensitive to waxiness
                score = min(100, max(0, (8.0 - texture_detail) * 12.0))
                scores.append(score)
                frame_idx += 1

        if not scores: return {'score': 0, 'summary': 'Not detected'}

        scores_arr = np.array(scores)
        avg_score, peak_score = np.mean(scores_arr), np.max(scores_arr)
        occurrences = np.sum(scores_arr > threshold)
        occurrence_rate = (occurrences / len(scores_arr)) * 100 if scores else 0
        worst_idx = np.argmax(scores_arr)
        worst_ts = worst_idx / v_stream.fps if v_stream.fps > 0 else 0

        return {'score': avg_score, 'summary': f"Avg: {avg_score:.1f} | Peak: {peak_score:.1f} | Occ: {occurrence_rate:.1f}%", 'worst_frame_timestamp': worst_ts}

class SharpeningDetector(BaseDetector):
    @property
    def issue_name(self) -> str: return "Excessive Sharpening"

    def run(self, source: VideoSource) -> dict:
        v_stream = source.info.video_stream
        if not v_stream: return {'score': -1}

        scores, frame_idx = [], 0
        threshold = 1.0

        with source as s:
            if not s: return {'score': -1}
            while (frame := s.read_frame()) is not None:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                residue = gray.astype(np.float32) - cv2.GaussianBlur(gray, (0, 0), 3).astype(np.float32)
                energy = np.mean(np.abs(residue))
                # FORMULA RECALIBRATED: Less sensitive to sharpening
                score = min(100, max(0, (energy - 4.0) * 10.0))
                scores.append(score)
                frame_idx += 1

        if not scores: return {'score': 0, 'summary': 'Not detected'}

        scores_arr = np.array(scores)
        avg_score, peak_score = np.mean(scores_arr), np.max(scores_arr)
        occurrences = np.sum(scores_arr > threshold)
        occurrence_rate = (occurrences / len(scores_arr)) * 100 if scores else 0
        worst_idx = np.argmax(scores_arr)
        worst_ts = worst_idx / v_stream.fps if v_stream.fps > 0 else 0

        return {'score': avg_score, 'summary': f"Avg: {avg_score:.1f} | Peak: {peak_score:.1f} | Occ: {occurrence_rate:.1f}%", 'worst_frame_timestamp': worst_ts}
