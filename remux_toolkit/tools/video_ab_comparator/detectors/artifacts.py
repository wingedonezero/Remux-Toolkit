# remux_toolkit/tools/video_ab_comparator/detectors/artifacts.py

import cv2
import numpy as np
from .base_detector import BaseDetector
from ..core.source import VideoSource
from typing import List

class BandingDetector(BaseDetector):
    @property
    def issue_name(self) -> str: return "Color Banding"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        v_stream = source.info.video_stream
        if not v_stream: return {'score': -1}

        scores, frame_idx = [], 0
        threshold = 1.0

        for frame in frame_list:
            lab_image = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
            l_channel, _, _ = cv2.split(lab_image)
            mean = cv2.boxFilter(l_channel.astype(np.float32), -1, (5, 5))
            mean_sq = cv2.boxFilter(l_channel.astype(np.float32)**2, -1, (5, 5))
            variance = mean_sq - mean**2
            low_variance_mask = (variance < 20).astype(np.uint8) * 255

            if np.sum(low_variance_mask) < low_variance_mask.size * 0.01:
                scores.append(0)
                frame_idx += 1
                continue

            hist = cv2.calcHist([l_channel], [0], low_variance_mask, [256], [0, 256])
            score = max(0, 100 - (np.count_nonzero(hist) / 2.0))
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

class RingingDetector(BaseDetector):
    @property
    def issue_name(self) -> str: return "Ringing / Halos"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        v_stream = source.info.video_stream
        if not v_stream: return {'score': -1}

        scores, frame_idx = [], 0
        threshold = 1.0

        for frame in frame_list:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            laplacian = cv2.Laplacian(gray, cv2.CV_64F)
            energy = np.mean(np.abs(laplacian))
            score = min(100, max(0, (energy - 5.0) * 10.0))
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

class DotCrawlDetector(BaseDetector):
    @property
    def issue_name(self) -> str: return "Dot Crawl"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        v_stream = source.info.video_stream
        if not v_stream: return {'score': -1}

        scores, frame_idx = [], 0
        threshold = 1.0

        for frame in frame_list:
            ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
            _, cr, cb = cv2.split(ycrcb)
            cr_lap = cv2.Laplacian(cr, cv2.CV_64F)
            cb_lap = cv2.Laplacian(cb, cv2.CV_64F)
            energy = np.mean(np.abs(cr_lap) + np.abs(cb_lap))
            score = min(100, max(0, (energy - 2.0) * 12.0))
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
