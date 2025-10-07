# remux_toolkit/tools/video_ab_comparator/detectors/artifacts.py

import cv2
import numpy as np
from .base_detector import BaseDetector
from ..core.source import VideoSource
from typing import List

class BandingDetector(BaseDetector):
    @property
    def issue_name(self) -> str:
        return "Color Banding"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        v_stream = source.info.video_stream
        if not v_stream:
            return {'score': -1}

        scores = []
        threshold = 5.0  # Only count frames with noticeable banding

        for frame in frame_list:
            lab_image = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
            l_channel, _, _ = cv2.split(lab_image)

            # Calculate local variance to find smooth gradient areas
            mean = cv2.boxFilter(l_channel.astype(np.float32), -1, (5, 5))
            mean_sq = cv2.boxFilter(l_channel.astype(np.float32)**2, -1, (5, 5))
            variance = mean_sq - mean**2

            # Low variance areas are candidates for banding
            low_variance_mask = (variance < 20).astype(np.uint8) * 255

            # Need sufficient smooth areas to detect banding
            smooth_area_ratio = np.sum(low_variance_mask) / low_variance_mask.size

            if smooth_area_ratio < 0.05:  # Less than 5% smooth areas
                scores.append(0)
                continue

            # Analyze histogram in smooth areas to detect discrete bands
            hist = cv2.calcHist([l_channel], [0], low_variance_mask, [256], [0, 256])
            hist = hist.flatten()

            # Count distinct peaks (bands) in histogram
            # More peaks = smoother gradient, fewer peaks = more banding
            non_zero_bins = np.count_nonzero(hist)

            # Also check for "gaps" in histogram (characteristic of banding)
            if non_zero_bins > 0:
                # Normalize histogram
                hist_norm = hist / np.sum(hist)

                # Count significant gaps
                gaps = 0
                in_gap = False
                for i in range(1, len(hist_norm)):
                    if hist_norm[i] < 0.0001 and hist_norm[i-1] > 0.001:
                        in_gap = True
                    elif hist_norm[i] > 0.001 and in_gap:
                        gaps += 1
                        in_gap = False

                # Score calculation:
                # More bins used = less banding (better)
                # More gaps = more banding (worse)
                # High smooth area ratio with few bins = definite banding

                bin_score = max(0, 100 - (non_zero_bins / 2.0))
                gap_score = min(50, gaps * 10)
                area_score = smooth_area_ratio * 30

                score = min(100, (bin_score * 0.5 + gap_score * 0.3 + area_score * 0.2))
            else:
                score = 0

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


class RingingDetector(BaseDetector):
    @property
    def issue_name(self) -> str:
        return "Ringing / Halos"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        v_stream = source.info.video_stream
        if not v_stream:
            return {'score': -1}

        scores = []
        threshold = 5.0

        for frame in frame_list:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            # Detect edges where ringing occurs
            edges = cv2.Canny(gray, 50, 150)

            # Dilate edges to create zones around edges
            kernel = np.ones((5, 5), np.uint8)
            edge_zones = cv2.dilate(edges, kernel) - edges  # Area around edges only

            if np.sum(edge_zones) < 100:
                scores.append(0)
                continue

            # Apply Laplacian to detect oscillations (ringing)
            laplacian = cv2.Laplacian(gray, cv2.CV_64F)

            # Check for high-frequency oscillations around edges
            ringing_energy = np.mean(np.abs(laplacian[edge_zones > 0]))

            # Also check for halo effect (bright/dark bands around edges)
            # Create slightly wider zone
            wider_zone = cv2.dilate(edges, np.ones((9, 9), np.uint8)) - edge_zones - edges

            if np.sum(wider_zone) > 100:
                # Compare luminance in edge zones vs wider zones
                edge_zone_luma = np.mean(gray[edge_zones > 0])
                wider_zone_luma = np.mean(gray[wider_zone > 0])
                halo_strength = abs(edge_zone_luma - wider_zone_luma)
            else:
                halo_strength = 0

            # Score calculation:
            # ringing_energy < 5 = clean (score near 0)
            # ringing_energy 5-15 = mild ringing (score 20-50)
            # ringing_energy > 15 = heavy ringing (score approaches 80)
            energy_score = min(80, max(0, (ringing_energy - 5.0) * 5.0))
            halo_score = min(40, halo_strength * 2)

            score = min(100, energy_score + halo_score * 0.5)
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


class DotCrawlDetector(BaseDetector):
    @property
    def issue_name(self) -> str:
        return "Dot Crawl"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        v_stream = source.info.video_stream
        if not v_stream:
            return {'score': -1}

        scores = []
        threshold = 5.0

        # Dot crawl needs temporal analysis (comparing consecutive frames)
        if len(frame_list) < 2:
            return {'score': 0, 'summary': 'Insufficient frames for temporal analysis'}

        for i in range(len(frame_list) - 1):
            curr_frame = frame_list[i]
            next_frame = frame_list[i + 1]

            # Convert to YCrCb
            curr_ycrcb = cv2.cvtColor(curr_frame, cv2.COLOR_BGR2YCrCb)
            next_ycrcb = cv2.cvtColor(next_frame, cv2.COLOR_BGR2YCrCb)

            curr_y, curr_cr, curr_cb = cv2.split(curr_ycrcb)
            next_y, next_cr, next_cb = cv2.split(next_ycrcb)

            # Find edges in luma channel (dot crawl appears along edges)
            edges = cv2.Canny(curr_y, 100, 200)
            kernel = np.ones((5, 5), np.uint8)
            edge_zones = cv2.dilate(edges, kernel)

            if np.sum(edge_zones) < 100:
                scores.append(0)
                continue

            # Check for temporal chroma instability along edges
            cr_diff = np.abs(curr_cr.astype(float) - next_cr.astype(float))
            cb_diff = np.abs(curr_cb.astype(float) - next_cb.astype(float))

            # Focus on edge areas where dot crawl occurs
            cr_edge_diff = cr_diff[edge_zones > 0]
            cb_edge_diff = cb_diff[edge_zones > 0]

            # Calculate chroma change metrics
            avg_cr_change = np.mean(cr_edge_diff)
            avg_cb_change = np.mean(cb_edge_diff)
            var_cr_change = np.var(cr_edge_diff)
            var_cb_change = np.var(cb_edge_diff)

            avg_chroma_change = (avg_cr_change + avg_cb_change) / 2

            # Check for speckled pattern (characteristic of dot crawl)
            # High variance with moderate mean suggests moving dots
            speckle_indicator = (var_cr_change + var_cb_change) / 2

            # Score calculation:
            # avg_chroma_change < 3 = stable chroma (score near 0)
            # avg_chroma_change 3-10 with high variance = mild dot crawl (score 20-40)
            # avg_chroma_change > 10 with high variance = heavy dot crawl (score 40-70)

            if avg_chroma_change > 3 and speckle_indicator > 50:
                base_score = min(70, (avg_chroma_change - 3) * 5)
                variance_bonus = min(30, speckle_indicator / 10)
                score = min(100, base_score + variance_bonus * 0.5)
            else:
                score = 0

            scores.append(score)

        if not scores:
            return {'score': 0, 'summary': 'Not detected'}

        scores_arr = np.array(scores)
        avg_score = np.mean(scores_arr)
        peak_score = np.max(scores_arr)
        occurrence_rate = np.sum(scores_arr > threshold) / len(scores_arr) * 100

        worst_idx = np.argmax(scores_arr)
        worst_ts = worst_idx / v_stream.fps if v_stream.fps > 0 else 0

        # Classify severity
        if avg_score > 50:
            severity = "Severe"
        elif avg_score > 25:
            severity = "Moderate"
        elif avg_score > 10:
            severity = "Mild"
        else:
            severity = "Minimal"

        summary = f"{severity} | Avg: {avg_score:.1f} | Peak: {peak_score:.1f} | Occ: {occurrence_rate:.1f}%"

        return {
            'score': avg_score,
            'summary': summary,
            'worst_frame_timestamp': worst_ts
        }
