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
        timestamp = source.info.duration * 0.5
        frame = source.get_frame(timestamp)
        if frame is None: return {'score': -1, 'summary': 'Frame extract failed'}

        ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
        y_channel, cr_channel, _ = cv2.split(ycrcb)
        cr_channel_upscaled = cv2.resize(cr_channel, (y_channel.shape[1], y_channel.shape[0]), interpolation=cv2.INTER_CUBIC)
        y_edges = cv2.Canny(y_channel, 50, 150)
        cr_edges = cv2.Canny(cr_channel_upscaled, 50, 150)

        try:
            shift, _ = cv2.phaseCorrelate(np.float32(y_edges), np.float32(cr_edges))
            dx, dy = shift
            shift_magnitude = np.sqrt(dx**2 + dy**2)
            score = min(100, shift_magnitude * 50)
            summary = f"Shift: ({dx:.2f}, {dy:.2f}) px"
        except cv2.error:
            score = 0
            summary = "No significant edges"

        return {'score': score, 'summary': summary, 'worst_frame_timestamp': timestamp}

class RainbowingDetector(BaseDetector):
    """Detects rainbowing artifacts (cross-color) on fine patterns."""

    @property
    def issue_name(self) -> str:
        return "Rainbowing / Cross-Color"

    def run(self, source: VideoSource) -> dict:
        timestamp = source.info.duration * 0.75
        frame = source.get_frame(timestamp)
        if frame is None: return {'score': -1, 'summary': 'Frame extract failed'}

        ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
        _, cr, cb = cv2.split(ycrcb)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        high_pass = gray - cv2.GaussianBlur(gray, (0, 0), 3)
        detail_mask = (np.abs(high_pass) > 10).astype(np.uint8)

        if np.sum(detail_mask) == 0: return {'score': 0, 'summary': 'No detailed areas', 'worst_frame_timestamp': timestamp}

        cr_variance = np.std(cr, where=(detail_mask > 0))
        cb_variance = np.std(cb, where=(detail_mask > 0))
        chroma_energy = (cr_variance + cb_variance) / 2.0
        score = min(100, max(0, (chroma_energy - 5.0) * 10))

        return {'score': score, 'summary': f"Chroma Energy: {chroma_energy:.2f}", 'worst_frame_timestamp': timestamp}

class ColorCastDetector(BaseDetector):
    """Detects an overall color cast/tint in the image."""

    @property
    def issue_name(self) -> str:
        return "Color Cast"

    def run(self, source: VideoSource) -> dict:
        """Measures the deviation of the average color from neutral gray."""
        timestamp = source.info.duration * 0.5
        frame = source.get_frame(timestamp)
        if frame is None: return {'score': -1, 'summary': 'Frame extract failed'}

        # We assume that for an average frame, the mean of the color channels should be roughly equal.
        # A strong deviation implies a color cast.
        b, g, r = np.mean(frame, axis=(0, 1))

        # Calculate the standard deviation of the channel means. A low value is good (neutral).
        color_dev = np.std([b, g, r])

        # Heuristic scoring
        score = min(100, max(0, (color_dev - 2.0) * 10))

        return {'score': score, 'summary': f"Color Deviation: {color_dev:.2f}", 'worst_frame_timestamp': timestamp}
