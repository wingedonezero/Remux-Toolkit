# remux_toolkit/tools/video_ab_comparator/detectors/noise.py

import cv2
import numpy as np
from .base_detector import BaseDetector
from ..core.source import VideoSource

class DNRDetector(BaseDetector):
    """Detects overly aggressive DNR (Digital Noise Reduction) leading to waxy textures."""

    @property
    def issue_name(self) -> str:
        return "Over-DNR / Waxiness"

    def run(self, source: VideoSource) -> dict:
        """Measures high-frequency energy in non-edge regions."""
        timestamp = source.info.duration * 0.4 # Sample a different point
        frame = source.get_frame(timestamp)
        if frame is None:
            return {'score': -1, 'summary': 'Frame extract failed'}

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # Find strong edges, and then dilate them to create a mask
        # of areas to EXCLUDE from our analysis.
        edges = cv2.Canny(gray, 100, 200)
        dilated_edges = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=1)

        # The "texture mask" is everywhere that ISN'T a strong edge.
        texture_mask = cv2.bitwise_not(dilated_edges)

        # Calculate high-frequency content (similar to sharpening detector but inverted)
        laplacian = cv2.Laplacian(gray, cv2.CV_64F)

        # We only care about the high-frequency content within the texture areas.
        # A healthy image has texture detail (high laplacian variance). A waxy one does not.
        texture_detail = np.std(laplacian, where=(texture_mask > 0))

        # Heuristic scoring: very low variance in texture areas is bad.
        score = min(100, max(0, (10 - texture_detail) * 15))

        return {
            'score': score,
            'summary': f"Texture Detail: {texture_detail:.2f}",
            'worst_frame_timestamp': timestamp
        }


class SharpeningDetector(BaseDetector):
    """Detects excessive sharpening applied to the video."""

    @property
    def issue_name(self) -> str:
        return "Excessive Sharpening"

    def run(self, source: VideoSource) -> dict:
        """Measures high-frequency energy not associated with natural edges."""
        timestamp = source.info.duration * 0.6
        frame = source.get_frame(timestamp)
        if frame is None:
            return {'score': -1, 'summary': 'Frame extract failed'}

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # Unsharp masking: sharpened image = original + (original - blurred) * amount
        # We can find the "sharpening residue" by getting (original - blurred).
        blurred = cv2.GaussianBlur(gray, (0, 0), 3)
        residue = gray.astype(np.float32) - blurred.astype(np.float32)

        # Excessive sharpening creates high energy in this residue.
        sharpening_energy = np.mean(np.abs(residue))

        # Heuristic scoring
        score = min(100, max(0, (sharpening_energy - 2.0) * 20))

        return {
            'score': score,
            'summary': f"Residue Energy: {sharpening_energy:.2f}",
            'worst_frame_timestamp': timestamp
        }
