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

        frame = frame_list[len(frame_list) // 2]  # Use middle frame

        ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
        y, cr, _ = cv2.split(ycrcb)
        cr_up = cv2.resize(cr, (y.shape[1], y.shape[0]), interpolation=cv2.INTER_CUBIC)
        y_edges = cv2.Canny(y, 50, 150)
        cr_edges = cv2.Canny(cr_up, 50, 150)
        try:
            shift, _ = cv2.phaseCorrelate(np.float32(y_edges), np.float32(cr_edges))
            dx, dy = shift
            shift_magnitude = np.sqrt(dx**2 + dy**2)

            # Scale score based on shift magnitude
            # 0 pixels = no shift (score 0)
            # 0.5 pixels = minor shift (score ~25)
            # 1 pixel = noticeable shift (score ~50)
            # 2+ pixels = major shift (score approaches 100)
            score = min(100, shift_magnitude * 50)

            summary = f"Shift: ({dx:.2f}, {dy:.2f}) px = {shift_magnitude:.2f}px total"
        except cv2.error:
            score, summary = 0, "No significant edges for detection"
        return {'score': score, 'summary': summary, 'worst_frame_timestamp': 0.0}


class RainbowingDetector(BaseDetector):
    @property
    def issue_name(self) -> str:
        return "Rainbowing / Cross-Color"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        v_stream = source.info.video_stream
        if not v_stream:
            return {'score': -1}

        scores = []
        threshold = 5.0  # Only count frames with noticeable rainbowing

        for frame in frame_list:
            ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
            y, cr, cb = cv2.split(ycrcb)

            # Find high-detail/high-frequency areas where rainbowing occurs
            edges = cv2.Canny(y, 50, 150)
            dilated = cv2.dilate(edges, np.ones((3, 3), np.uint8))
            detail_mask = dilated > 0

            if np.sum(detail_mask) < 100:
                scores.append(0)
                continue

            # Check chroma variance in detailed areas
            # Rainbowing shows as unexpected chroma variation in high-detail luma areas
            cr_detail = cr[detail_mask]
            cb_detail = cb[detail_mask]

            cr_var = np.var(cr_detail)
            cb_var = np.var(cb_detail)

            # Combined chroma energy in detailed areas
            chroma_energy = (cr_var + cb_var) / 2.0

            # Score calculation:
            # chroma_energy < 20 = clean (score near 0)
            # chroma_energy 20-40 = mild rainbowing (score 20-40)
            # chroma_energy 40-60 = moderate rainbowing (score 40-60)
            # chroma_energy > 60 = heavy rainbowing (score approaches 80)
            score = min(80, max(0, (chroma_energy - 20.0) * 2.0))

            scores.append(score)

        if not scores:
            return {'score': 0, 'summary': 'Not detected'}

        scores_arr = np.array(scores)
        avg_score = np.mean(scores_arr)
        peak_score = np.max(scores_arr)

        # Count frames with noticeable rainbowing
        occurrence_rate = np.sum(scores_arr > threshold) / len(scores_arr) * 100

        worst_idx = np.argmax(scores_arr)
        worst_ts = worst_idx / v_stream.fps if v_stream.fps > 0 else 0

        return {
            'score': avg_score,
            'summary': f"Avg: {avg_score:.1f} | Peak: {peak_score:.1f} | Occ: {occurrence_rate:.1f}%",
            'worst_frame_timestamp': worst_ts
        }


class ColorCastDetector(BaseDetector):
    @property
    def issue_name(self) -> str:
        return "Color Cast"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        if not frame_list:
            return {'score': 0, 'summary': 'Analysis failed'}

        cast_scores = []
        a_values = []  # red/green axis
        b_values = []  # yellow/blue axis

        for frame in frame_list:
            # Sample center region to avoid letterboxing/pillarboxing
            h, w = frame.shape[:2]
            center_region = frame[h//4:3*h//4, w//4:3*w//4]

            # Convert to LAB color space for accurate color analysis
            lab = cv2.cvtColor(center_region, cv2.COLOR_BGR2LAB)
            l, a, b = cv2.split(lab)

            # Check for color temperature bias
            # In LAB: a=128 is neutral (green to red axis)
            #         b=128 is neutral (blue to yellow axis)
            avg_a = np.mean(a) - 128  # Center at 0
            avg_b = np.mean(b) - 128

            a_values.append(avg_a)
            b_values.append(avg_b)

            # Calculate color cast magnitude
            cast_magnitude = np.sqrt(avg_a**2 + avg_b**2)

            # Also check RGB channel balance for additional confirmation
            b_ch, g_ch, r_ch = cv2.split(center_region)
            channel_imbalance = np.std([np.mean(b_ch), np.mean(g_ch), np.mean(r_ch)])

            # Combine LAB cast detection with RGB imbalance
            # Score calculation:
            # cast_magnitude < 3 = neutral (score near 0)
            # cast_magnitude 3-8 = mild cast (score 15-40)
            # cast_magnitude 8-15 = moderate cast (score 40-70)
            # cast_magnitude > 15 = heavy cast (score approaches 90)
            score = min(90, max(0, (cast_magnitude - 3.0) * 5.0 + channel_imbalance * 1.5))

            cast_scores.append(score)

        avg_score = np.mean(cast_scores)
        avg_a = np.mean(a_values)
        avg_b = np.mean(b_values)
        avg_magnitude = np.sqrt(avg_a**2 + avg_b**2)

        # Classify the cast type
        cast_type = "Neutral"
        if avg_magnitude > 3.0:
            # Determine dominant cast direction
            if abs(avg_a) > abs(avg_b):
                # Red/Green dominant
                if avg_a > 0:
                    if avg_b > 0:
                        cast_type = "Red/Yellow"
                    else:
                        cast_type = "Red/Magenta"
                else:
                    if avg_b > 0:
                        cast_type = "Green/Yellow"
                    else:
                        cast_type = "Green/Cyan"
            else:
                # Blue/Yellow dominant
                if avg_b > 0:
                    if avg_a > 0:
                        cast_type = "Yellow/Red (Warm)"
                    else:
                        cast_type = "Yellow/Green"
                else:
                    if avg_a > 0:
                        cast_type = "Blue/Magenta"
                    else:
                        cast_type = "Blue/Cyan (Cool)"

        if avg_magnitude > 3.0:
            summary = f"{cast_type} cast | Magnitude: {avg_magnitude:.2f}"
        else:
            summary = f"Neutral color balance | Deviation: {avg_magnitude:.2f}"

        return {'score': avg_score, 'summary': summary, 'worst_frame_timestamp': 0.0}
