# remux_toolkit/tools/video_ab_comparator/detectors/noise.py

import cv2
import numpy as np
from .base_detector import BaseDetector
from ..core.source import VideoSource
from typing import List

class DNRDetector(BaseDetector):
    @property
    def issue_name(self) -> str:
        return "Over-DNR / Waxiness"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        v_stream = source.info.video_stream
        if not v_stream:
            return {'score': -1}

        scores = []
        threshold = 5.0

        for frame in frame_list:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            # Find edges to exclude them from texture analysis
            edges = cv2.Canny(gray, 100, 200)
            edge_mask = cv2.dilate(edges, np.ones((5, 5), np.uint8))

            # Create texture mask (non-edge areas)
            texture_mask = cv2.bitwise_not(edge_mask)

            if np.sum(texture_mask) < 1000:
                # Not enough texture area to analyze
                scores.append(0)
                continue

            # Analyze texture detail in non-edge areas
            # Over-DNR removes fine texture, making surfaces waxy/plastic
            laplacian = cv2.Laplacian(gray, cv2.CV_64F)
            texture_detail = np.std(laplacian[texture_mask > 0])

            # Also check for unnatural smoothness
            # Calculate local variance in texture areas
            kernel = np.ones((5, 5), np.float32) / 25
            local_mean = cv2.filter2D(gray.astype(np.float32), -1, kernel)
            local_sq_mean = cv2.filter2D((gray.astype(np.float32))**2, -1, kernel)
            local_variance = local_sq_mean - local_mean**2

            avg_local_var = np.mean(local_variance[texture_mask > 0])

            # Score calculation:
            # High texture detail (>8) = natural grain (score near 0)
            # Medium detail (4-8) = mild DNR (score 20-40)
            # Low detail (<4) = heavy DNR/waxiness (score 40-80)
            # Very low variance (<10) adds to score (unnatural smoothness)

            detail_score = min(80, max(0, (10.0 - texture_detail) * 10.0))
            smoothness_penalty = min(20, max(0, (30 - avg_local_var) * 2))

            score = min(100, detail_score + smoothness_penalty * 0.5)
            scores.append(score)

        if not scores:
            return {'score': 0, 'summary': 'Not detected'}

        scores_arr = np.array(scores)
        avg_score = np.mean(scores_arr)
        peak_score = np.max(scores_arr)
        occurrence_rate = np.sum(scores_arr > threshold) / len(scores_arr) * 100

        worst_idx = np.argmax(scores_arr)
        worst_ts = worst_idx / v_stream.fps if v_stream.fps > 0 else 0

        return {
            'score': avg_score,
            'summary': f"Avg: {avg_score:.1f} | Peak: {peak_score:.1f} | Occ: {occurrence_rate:.1f}%",
            'worst_frame_timestamp': worst_ts
        }


class SharpeningDetector(BaseDetector):
    @property
    def issue_name(self) -> str:
        return "Excessive Sharpening"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        v_stream = source.info.video_stream
        if not v_stream:
            return {'score': -1}

        scores = []
        threshold = 5.0

        for frame in frame_list:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            # Calculate unsharp mask residue
            # Over-sharpening creates characteristic halos
            blurred = cv2.GaussianBlur(gray, (0, 0), 3)
            residue = gray.astype(np.float32) - blurred.astype(np.float32)

            # Find edges where sharpening halos appear
            edges = cv2.Canny(gray, 50, 150)
            edge_zones = cv2.dilate(edges, np.ones((7, 7), np.uint8))

            if np.sum(edge_zones) < 100:
                scores.append(0)
                continue

            # Analyze residue energy around edges
            edge_residue = np.abs(residue[edge_zones > 0])
            residue_energy = np.mean(edge_residue)

            # Check for overshoots (values too bright/dark near edges)
            overshoot_mask = np.abs(residue) > 20
            overshoot_ratio = np.sum(overshoot_mask[edge_zones > 0]) / np.sum(edge_zones > 0)

            # Also check for ringing patterns (oscillations from over-sharpening)
            laplacian = cv2.Laplacian(gray, cv2.CV_64F)
            laplacian_energy = np.mean(np.abs(laplacian[edge_zones > 0]))

            # Score calculation:
            # residue_energy < 4 = natural (score near 0)
            # residue_energy 4-8 = mild sharpening (score 20-40)
            # residue_energy 8-12 = moderate sharpening (score 40-60)
            # residue_energy > 12 = excessive sharpening (score 60-85)
            # High overshoot ratio adds additional penalty

            energy_score = min(85, max(0, (residue_energy - 4.0) * 8.0))
            overshoot_penalty = min(25, overshoot_ratio * 100)
            ringing_penalty = min(15, max(0, (laplacian_energy - 10) * 2))

            score = min(100, energy_score + overshoot_penalty * 0.4 + ringing_penalty * 0.3)
            scores.append(score)

        if not scores:
            return {'score': 0, 'summary': 'Not detected'}

        scores_arr = np.array(scores)
        avg_score = np.mean(scores_arr)
        peak_score = np.max(scores_arr)
        occurrence_rate = np.sum(scores_arr > threshold) / len(scores_arr) * 100

        worst_idx = np.argmax(scores_arr)
        worst_ts = worst_idx / v_stream.fps if v_stream.fps > 0 else 0

        return {
            'score': avg_score,
            'summary': f"Avg: {avg_score:.1f} | Peak: {peak_score:.1f} | Occ: {occurrence_rate:.1f}%",
            'worst_frame_timestamp': worst_ts
        }
