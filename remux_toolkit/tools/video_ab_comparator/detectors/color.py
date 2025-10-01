# remux_toolkit/tools/video_ab_comparator/detectors/color.py

import cv2
import numpy as np
from .base_detector import BaseDetector
from ..core.source import VideoSource
from typing import List

class ChromaShiftDetector(BaseDetector):
    @property
    def issue_name(self) -> str: return "Chroma Shift"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        if not frame_list:
            return {'score': -1, 'summary': 'Frame extract failed'}

        frame = frame_list[len(frame_list) // 2] # Use middle frame

        ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
        y, cr, _ = cv2.split(ycrcb)
        cr_up = cv2.resize(cr, (y.shape[1], y.shape[0]), interpolation=cv2.INTER_CUBIC)
        y_edges = cv2.Canny(y, 50, 150)
        cr_edges = cv2.Canny(cr_up, 50, 150)
        try:
            shift, _ = cv2.phaseCorrelate(np.float32(y_edges), np.float32(cr_edges))
            dx, dy = shift
            score = min(100, np.sqrt(dx**2 + dy**2) * 50)
            summary = f"Shift: ({dx:.2f}, {dy:.2f}) px"
        except cv2.error:
            score, summary = 0, "No significant edges"
        return {'score': score, 'summary': summary, 'worst_frame_timestamp': 0.0}

class RainbowingDetector(BaseDetector):
    @property
    def issue_name(self) -> str: return "Rainbowing / Cross-Color"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        v_stream = source.info.video_stream
        if not v_stream: return {'score': -1}

        scores, frame_idx = [], 0
        threshold = 1.0

        for frame in frame_list:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            detail_mask = (np.abs(gray - cv2.GaussianBlur(gray, (0, 0), 3)) > 10).astype(np.uint8)
            if np.sum(detail_mask) == 0:
                scores.append(0); frame_idx += 1; continue

            ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
            _, cr, cb = cv2.split(ycrcb)
            energy = (np.std(cr, where=(detail_mask > 0)) + np.std(cb, where=(detail_mask > 0))) / 2.0
            score = min(100, max(0, (energy - 8.0) * 8.0))
            scores.append(score); frame_idx += 1

        if not scores: return {'score': 0, 'summary': 'Not detected'}

        scores_arr = np.array(scores)
        avg_score, peak_score = np.mean(scores_arr), np.max(scores_arr)
        occurrence_rate = np.sum(scores_arr > threshold) / len(scores_arr) * 100
        worst_idx = np.argmax(scores_arr)
        worst_ts = worst_idx / v_stream.fps if v_stream.fps > 0 else 0

        return {'score': avg_score, 'summary': f"Avg: {avg_score:.1f} | Peak: {peak_score:.1f} | Occ: {occurrence_rate:.1f}%", 'worst_frame_timestamp': worst_ts}

class ColorCastDetector(BaseDetector):
    @property
    def issue_name(self) -> str: return "Color Cast"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        if not frame_list: return {'score': 0, 'summary': 'Analysis failed'}

        b_means, g_means, r_means = [], [], []
        for frame in frame_list:
            small_frame = cv2.resize(frame, (16, 16), interpolation=cv2.INTER_AREA)
            b, g, r = np.mean(small_frame, axis=(0, 1))
            b_means.append(b); g_means.append(g); r_means.append(r)

        color_dev = np.std([np.mean(b_means), np.mean(g_means), np.mean(r_means)])
        score = min(100, max(0, (color_dev - 2.0) * 10))
        return {'score': score, 'summary': f"Avg Deviation: {color_dev:.2f}", 'worst_frame_timestamp': 0.0}
