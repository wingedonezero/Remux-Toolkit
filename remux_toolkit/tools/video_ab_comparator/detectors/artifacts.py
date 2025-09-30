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
        worst_ts = source.info.duration / 3  # Default timestamp

        for i in range(3):
            timestamp = source.info.duration * (i + 1) / 4.0
            frame = source.get_frame(timestamp)
            if frame is None:
                continue

            # Convert to LAB color space to isolate Luminance
            lab_image = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
            l_channel, _, _ = cv2.split(lab_image)

            # Use a bilateral filter to smooth out noise/grain while preserving edges
            smoothed_l = cv2.bilateralFilter(l_channel, 9, 75, 75)

            # Calculate local variance to find "flat" areas
            mean = cv2.boxFilter(smoothed_l, -1, (5, 5))
            mean_sq = cv2.boxFilter(smoothed_l**2, -1, (5, 5))
            variance = mean_sq - mean**2

            # Create a mask for low-variance regions
            low_variance_mask = (variance < 10).astype(np.uint8) * 255
            if np.sum(low_variance_mask) < low_variance_mask.size * 0.01:
                continue # Not enough flat area to analyze

            # In these flat areas, count the number of unique luminance values
            hist = cv2.calcHist([l_channel], [0], low_variance_mask, [256], [0, 256])

            # A healthy gradient has many unique values. Banding has few.
            num_colors = np.count_nonzero(hist)

            # Score inversely based on number of colors in flat areas
            score = max(0, 100 - (num_colors / 2.0))

            if score > worst_score:
                worst_score = score
                worst_ts = timestamp

        if worst_score == -1:
            return {'score': 0, 'summary': 'Not detected', 'worst_frame_timestamp': worst_ts}

        return {
            'score': worst_score,
            'summary': f"Score: {worst_score:.1f}",
            'worst_frame_timestamp': worst_ts
        }


class RingingDetector(BaseDetector):
    """Detects ringing/halos from over-sharpening."""

    @property
    def issue_name(self) -> str:
        return "Ringing / Halos"

    def run(self, source: VideoSource) -> dict:
        """Measures energy of halos around strong edges."""
        timestamp = source.info.duration * 0.5  # Sample middle
        frame = source.get_frame(timestamp)
        if frame is None:
            return {'score': -1, 'summary': 'Frame extract failed'}

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # Use a Laplacian filter to highlight edges and halos
        laplacian = cv2.Laplacian(gray, cv2.CV_64F)

        # Ringing artifacts have high energy in the Laplacian image
        ringing_energy = np.mean(np.abs(laplacian))

        # Heuristic scoring
        score = min(100, max(0, (ringing_energy - 3.0) * 15))

        return {
            'score': score,
            'summary': f"Energy: {ringing_energy:.2f}",
            'worst_frame_timestamp': timestamp
        }
