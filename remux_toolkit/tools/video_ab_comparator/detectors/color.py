# remux_toolkit/tools/video_ab_comparator/detectors/color.py

import cv2
import numpy as np
from .base_detector import BaseDetector
from ..core.source import VideoSource

class ChromaShiftDetector(BaseDetector):
    """Detects spatial misalignment between luma and chroma channels."""

    @property
    def issue_name(self) -> str:
        return "Chroma Shift"

    def run(self, source: VideoSource) -> dict:
        """Calculates the offset between luma and chroma edge maps."""
        timestamp = source.info.duration * 0.5
        frame = source.get_frame(timestamp)

        if frame is None:
            return {'score': -1, 'summary': 'Frame extract failed'}

        # Convert to YCrCb and get Luma (Y) and one Chroma (Cr) channel
        ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR YCrCb)
        y_channel, cr_channel, _ = cv2.split(ycrcb)

        # Upscale chroma to match luma size for accurate comparison
        cr_channel_upscaled = cv2.resize(cr_channel, (y_channel.shape[1], y_channel.shape[0]), interpolation=cv2.INTER_CUBIC)

        # Find edges in both luma and chroma
        y_edges = cv2.Canny(y_channel, 50, 150)
        cr_edges = cv2.Canny(cr_channel_upscaled, 50, 150)

        # Use phase correlation to find the shift between the two edge maps
        try:
            shift, _ = cv2.phaseCorrelate(np.float32(y_edges), np.float32(cr_edges))
            dx, dy = shift

            # The score is the magnitude of the shift vector
            shift_magnitude = np.sqrt(dx**2 + dy**2)

            # Score scaling: a shift of more than 2 pixels is severe
            score = min(100, shift_magnitude * 50)
            summary = f"Shift: ({dx:.2f}, {dy:.2f}) px"
        except cv2.error:
            score = 0
            summary = "No significant edges"

        return {
            'score': score,
            'summary': summary,
            'worst_frame_timestamp': timestamp
        }


class RainbowingDetector(BaseDetector):
    """Detects rainbowing artifacts (cross-color) on fine patterns."""

    @property
    def issue_name(self) -> str:
        return "Rainbowing / Cross-Color"

    def run(self, source: VideoSource) -> dict:
        """Measures chroma variance in high-frequency luma areas."""
        timestamp = source.info.duration * 0.75
        frame = source.get_frame(timestamp)
        if frame is None:
            return {'score': -1, 'summary': 'Frame extract failed'}

        # Isolate Chroma channels
        ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
        _, cr, cb = cv2.split(ycrcb)

        # High-pass filter the luma channel to find detailed areas
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        high_pass = gray - cv2.GaussianBlur(gray, (0, 0), 3)

        # Create a mask where there's significant high-frequency detail
        detail_mask = (np.abs(high_pass) > 10).astype(np.uint8)

        if np.sum(detail_mask) == 0:
            return {'score': 0, 'summary': 'No detailed areas found', 'worst_frame_timestamp': timestamp}

        # Measure the variance of the chroma channels only in those detailed areas
        cr_variance = np.std(cr, where=(detail_mask > 0))
        cb_variance = np.std(cb, where=(detail_mask > 0))

        chroma_energy = (cr_variance + cb_variance) / 2.0

        # Heuristic scoring
        score = min(100, max(0, (chroma_energy - 5.0) * 10))

        return {
            'score': score,
            'summary': f"Chroma Energy: {chroma_energy:.2f}",
            'worst_frame_timestamp': timestamp
        }
