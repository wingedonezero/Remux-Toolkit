# remux_toolkit/tools/video_ab_comparator/detectors/artifacts.py

import cv2
import numpy as np
from .base_detector import BaseDetector
from ..core.source import VideoSource

class BandingDetector(BaseDetector):
    """Detects color banding in smooth gradients."""

    @property
    def issue_name(self) -> str:
        return "Color Banding"

    def run(self, source: VideoSource) -> dict:
        """Analyzes luminance histograms in low-variance areas."""
        worst_score = -1
        worst_ts = source.info.duration / 3

        for i in range(3):
            timestamp = source.info.duration * (i + 1) / 4.0
            frame = source.get_frame(timestamp)
            if frame is None: continue

            lab_image = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
            l_channel, _, _ = cv2.split(lab_image)
            smoothed_l = cv2.bilateralFilter(l_channel, 9, 75, 75)
            mean = cv2.boxFilter(smoothed_l, -1, (5, 5))
            mean_sq = cv2.boxFilter(smoothed_l**2, -1, (5, 5))
            variance = mean_sq - mean**2
            low_variance_mask = (variance < 10).astype(np.uint8) * 255
            if np.sum(low_variance_mask) < low_variance_mask.size * 0.01: continue

            hist = cv2.calcHist([l_channel], [0], low_variance_mask, [256], [0, 256])
            num_colors = np.count_nonzero(hist)
            score = max(0, 100 - (num_colors / 2.0))

            if score > worst_score:
                worst_score = score
                worst_ts = timestamp

        if worst_score == -1: return {'score': 0, 'summary': 'Not detected', 'worst_frame_timestamp': worst_ts}
        return {'score': worst_score, 'summary': f"Score: {worst_score:.1f}", 'worst_frame_timestamp': worst_ts}

class RingingDetector(BaseDetector):
    """Detects ringing/halos from over-sharpening."""

    @property
    def issue_name(self) -> str:
        return "Ringing / Halos"

    def run(self, source: VideoSource) -> dict:
        timestamp = source.info.duration * 0.5
        frame = source.get_frame(timestamp)
        if frame is None: return {'score': -1, 'summary': 'Frame extract failed'}

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        laplacian = cv2.Laplacian(gray, cv2.CV_64F)
        ringing_energy = np.mean(np.abs(laplacian))
        score = min(100, max(0, (ringing_energy - 3.0) * 15))

        return {'score': score, 'summary': f"Energy: {ringing_energy:.2f}", 'worst_frame_timestamp': timestamp}

class DotCrawlDetector(BaseDetector):
    """Detects dot crawl, a composite video artifact."""

    @property
    def issue_name(self) -> str:
        return "Dot Crawl"

    def run(self, source: VideoSource) -> dict:
        """Looks for high-frequency checkerboard patterns in the chroma."""
        timestamp = source.info.duration * 0.25
        frame = source.get_frame(timestamp)
        if frame is None: return {'score': -1, 'summary': 'Frame extract failed'}

        ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
        _, cr, cb = cv2.split(ycrcb)

        # Dot crawl appears as high-frequency noise in chroma
        cr_lap = cv2.Laplacian(cr, cv2.CV_64F)
        cb_lap = cv2.Laplacian(cb, cv2.CV_64F)

        dot_crawl_energy = np.mean(np.abs(cr_lap) + np.abs(cb_lap))

        # Heuristic scoring
        score = min(100, max(0, (dot_crawl_energy - 1.0) * 20))

        return {'score': score, 'summary': f"Energy: {dot_crawl_energy:.2f}", 'worst_frame_timestamp': timestamp}
