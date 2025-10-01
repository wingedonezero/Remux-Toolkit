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


class RainbowingDetector(BaseDetector):
    @property
    def issue_name(self) -> str:
        return "Rainbowing / Cross-Color"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        v_stream = source.info.video_stream
        if not v_stream:
            return {'score': -1}

        scores = []
        perceptible_count = 0

        for frame_idx, frame in enumerate(frame_list):
            # Convert to YCrCb for better chroma analysis
            ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
            y, cr, cb = cv2.split(ycrcb)

            # 1. Find high-detail luma areas (where rainbowing typically occurs)
            # These are typically fine patterns like cross-hatching, brick walls, striped clothing
            y_freq = np.fft.fft2(y)
            y_freq_shift = np.fft.fftshift(y_freq)
            magnitude = np.abs(y_freq_shift)

            # Look for specific frequency patterns that cause NTSC artifacts
            h, w = magnitude.shape
            center_h, center_w = h//2, w//2

            # NTSC color subcarrier interference occurs at specific frequencies
            # Check for energy at ~3.58 MHz equivalent in spatial domain
            ntsc_artifact_band = magnitude[center_h-30:center_h+30, center_w-30:center_w+30]
            high_freq_energy = np.mean(ntsc_artifact_band)

            # 2. Check for abnormal chroma variation in high-detail areas
            # Real rainbowing shows as false colors in fine patterns

            # Edge detection to find detailed areas
            edges = cv2.Canny(y, 50, 150)
            dilated_edges = cv2.dilate(edges, np.ones((3,3), np.uint8))

            # Get chroma variance in detailed areas
            detail_mask = dilated_edges > 0

            if np.sum(detail_mask) > 100:  # Enough detail to analyze
                # Check for rapid chroma changes in detailed areas
                cr_detail = cr[detail_mask]
                cb_detail = cb[detail_mask]

                # Calculate local chroma variation
                cr_grad = cv2.Sobel(cr, cv2.CV_64F, 1, 0, ksize=3) + cv2.Sobel(cr, cv2.CV_64F, 0, 1, ksize=3)
                cb_grad = cv2.Sobel(cb, cv2.CV_64F, 1, 0, ksize=3) + cv2.Sobel(cb, cv2.CV_64F, 0, 1, ksize=3)

                cr_variation = np.std(cr_grad[detail_mask])
                cb_variation = np.std(cb_grad[detail_mask])

                # 3. Check for characteristic rainbow patterns
                # Real rainbowing creates periodic color shifts

                # Analyze color periodicity in detailed regions
                if cr_variation > 8 and cb_variation > 8:
                    # Check if the chroma changes form periodic patterns
                    cr_row_sample = cr[h//2, :]  # Sample middle row
                    cb_row_sample = cb[h//2, :]

                    # Autocorrelation to detect periodicity
                    cr_autocorr = np.correlate(cr_row_sample, cr_row_sample, mode='same')
                    cb_autocorr = np.correlate(cb_row_sample, cb_row_sample, mode='same')

                    # Look for peaks indicating periodic patterns
                    cr_peaks = self._find_periodicity(cr_autocorr)
                    cb_peaks = self._find_periodicity(cb_autocorr)

                    has_periodicity = cr_peaks > 2 or cb_peaks > 2
                else:
                    has_periodicity = False
                    cr_variation = 0
                    cb_variation = 0

                # 4. Perceptibility check - is it visible to human eye?
                # Consider: chroma strength, area affected, and pattern regularity

                chroma_strength = (cr_variation + cb_variation) / 2
                affected_area = np.sum(detail_mask) / (h * w) * 100  # Percentage of frame

                # Perceptibility threshold based on multiple factors
                if chroma_strength > 15 and affected_area > 1 and has_periodicity:
                    # Strong, widespread, periodic = definitely visible
                    perceptible = True
                    score = min(100, chroma_strength * 3)
                elif chroma_strength > 10 and affected_area > 3:
                    # Moderate but widespread = likely visible
                    perceptible = True
                    score = min(80, chroma_strength * 2.5)
                elif chroma_strength > 20:
                    # Very strong even if localized = visible
                    perceptible = True
                    score = min(70, chroma_strength * 2)
                else:
                    # Below perceptibility threshold
                    perceptible = False
                    score = 0

                if perceptible:
                    perceptible_count += 1
            else:
                score = 0

            scores.append(score)

        if not scores:
            return {'score': 0, 'summary': 'Not detected'}

        scores_arr = np.array(scores)
        avg_score = np.mean(scores_arr)
        peak_score = np.max(scores_arr)
        perceptible_rate = (perceptible_count / len(scores_arr)) * 100

        # Only report if genuinely perceptible
        if perceptible_rate < 5:
            return {'score': 0, 'summary': 'Below perceptible threshold'}

        worst_idx = np.argmax(scores_arr)
        worst_ts = worst_idx / v_stream.fps if v_stream.fps > 0 else 0

        summary = f"Perceptible in {perceptible_rate:.1f}% | Peak: {peak_score:.1f}"

        return {
            'score': avg_score,
            'summary': summary,
            'worst_frame_timestamp': worst_ts
        }

    def _find_periodicity(self, autocorr: np.ndarray) -> int:
        """Count periodic peaks in autocorrelation."""
        # Normalize
        autocorr = autocorr / (np.max(np.abs(autocorr)) + 1e-10)

        # Find peaks
        peaks = []
        for i in range(10, len(autocorr) - 10):
            if autocorr[i] > autocorr[i-1] and autocorr[i] > autocorr[i+1] and autocorr[i] > 0.3:
                peaks.append(i)

        return len(peaks)


class DotCrawlDetector(BaseDetector):
    @property
    def issue_name(self) -> str:
        return "Dot Crawl"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        v_stream = source.info.video_stream
        if not v_stream:
            return {'score': -1}

        scores = []
        perceptible_count = 0

        # Dot crawl needs temporal analysis (comparing consecutive frames)
        if len(frame_list) < 2:
            return {'score': 0, 'summary': 'Insufficient frames'}

        for i in range(len(frame_list) - 1):
            curr_frame = frame_list[i]
            next_frame = frame_list[i + 1]

            # Convert to YCrCb
            curr_ycrcb = cv2.cvtColor(curr_frame, cv2.COLOR_BGR2YCrCb)
            next_ycrcb = cv2.cvtColor(next_frame, cv2.COLOR_BGR2YCrCb)

            curr_y, curr_cr, curr_cb = cv2.split(curr_ycrcb)
            next_y, next_cr, next_cb = cv2.split(next_ycrcb)

            # 1. Dot crawl appears as moving dots along edges
            # Find edges in luma channel
            edges = cv2.Canny(curr_y, 100, 200)

            # Dilate edges to create inspection zones
            kernel = np.ones((5, 5), np.uint8)
            edge_zones = cv2.dilate(edges, kernel)

            if np.sum(edge_zones) < 100:
                scores.append(0)
                continue

            # 2. Check for temporal chroma instability along edges
            # Real dot crawl shows as crawling/moving colored dots

            cr_diff = np.abs(curr_cr.astype(float) - next_cr.astype(float))
            cb_diff = np.abs(curr_cb.astype(float) - next_cb.astype(float))

            # Focus on edge areas where dot crawl occurs
            cr_edge_diff = cr_diff[edge_zones > 0]
            cb_edge_diff = cb_diff[edge_zones > 0]

            # 3. Analyze the pattern of chroma changes
            # Dot crawl creates a specific speckled pattern, not uniform changes

            cr_variance = np.var(cr_edge_diff)
            cb_variance = np.var(cb_edge_diff)

            # Check for high-frequency speckled pattern
            cr_high_freq = self._detect_speckle_pattern(cr_diff, edge_zones)
            cb_high_freq = self._detect_speckle_pattern(cb_diff, edge_zones)

            # 4. Check for characteristic "crawling" motion
            # Real dot crawl moves in a specific pattern frame to frame

            if i > 0 and i < len(frame_list) - 2:
                # Three-frame analysis for motion pattern
                prev_cr = cv2.split(cv2.cvtColor(frame_list[i-1], cv2.COLOR_BGR2YCrCb))[1]
                next2_cr = cv2.split(cv2.cvtColor(frame_list[i+1], cv2.COLOR_BGR2YCrCb))[1]

                # Check if the pattern is moving (crawling)
                motion_pattern = self._detect_crawling_motion(
                    prev_cr, curr_cr, next_cr, next2_cr, edge_zones
                )
            else:
                motion_pattern = 0

            # 5. Perceptibility assessment
            # Consider: intensity, area affected, pattern characteristics

            avg_chroma_change = (np.mean(cr_edge_diff) + np.mean(cb_edge_diff)) / 2
            speckle_intensity = (cr_high_freq + cb_high_freq) / 2
            affected_pixels = np.sum(edge_zones > 0)
            frame_coverage = affected_pixels / (curr_y.shape[0] * curr_y.shape[1]) * 100

            # Calculate perceptibility score
            if speckle_intensity > 30 and avg_chroma_change > 8 and motion_pattern > 0.5:
                # Classic dot crawl pattern - highly visible
                perceptible = True
                score = min(100, speckle_intensity * 2 + motion_pattern * 20)
            elif speckle_intensity > 20 and avg_chroma_change > 5 and frame_coverage > 2:
                # Moderate dot crawl - visible in edge areas
                perceptible = True
                score = min(70, speckle_intensity * 1.5 + avg_chroma_change * 3)
            elif speckle_intensity > 15 and motion_pattern > 0.3:
                # Light dot crawl - visible to trained eye
                perceptible = True
                score = min(50, speckle_intensity + motion_pattern * 10)
            else:
                # Below perceptibility threshold
                perceptible = False
                score = 0

            if perceptible:
                perceptible_count += 1

            scores.append(score)

        if not scores:
            return {'score': 0, 'summary': 'Not detected'}

        scores_arr = np.array(scores)
        avg_score = np.mean(scores_arr)
        peak_score = np.max(scores_arr)
        perceptible_rate = (perceptible_count / len(scores_arr)) * 100

        # Only report if genuinely perceptible
        if perceptible_rate < 5:
            return {'score': 0, 'summary': 'Below perceptible threshold'}

        worst_idx = np.argmax(scores_arr)
        worst_ts = worst_idx / v_stream.fps if v_stream.fps > 0 else 0

        # Classify severity
        if avg_score > 60:
            severity = "Severe"
        elif avg_score > 30:
            severity = "Moderate"
        else:
            severity = "Light"

        summary = f"{severity} | Visible in {perceptible_rate:.1f}% | Peak: {peak_score:.1f}"

        return {
            'score': avg_score,
            'summary': summary,
            'worst_frame_timestamp': worst_ts
        }

    def _detect_speckle_pattern(self, diff_channel: np.ndarray, mask: np.ndarray) -> float:
        """Detect high-frequency speckled pattern characteristic of dot crawl."""
        # Apply mask to focus on edge areas
        masked_diff = diff_channel * (mask > 0).astype(float)

        # Use local variance to detect speckle
        kernel_size = 3
        kernel = np.ones((kernel_size, kernel_size)) / (kernel_size * kernel_size)
        local_mean = cv2.filter2D(masked_diff, -1, kernel)
        local_var = cv2.filter2D(masked_diff**2, -1, kernel) - local_mean**2

        # High local variance indicates speckled pattern
        speckle_score = np.mean(local_var[mask > 0]) if np.sum(mask > 0) > 0 else 0

        return speckle_score

    def _detect_crawling_motion(self, prev: np.ndarray, curr: np.ndarray,
                                next: np.ndarray, next2: np.ndarray,
                                mask: np.ndarray) -> float:
        """Detect the characteristic crawling motion of dot crawl artifacts."""
        # Calculate motion vectors of the chroma pattern
        motion1 = np.abs(curr.astype(float) - prev.astype(float))
        motion2 = np.abs(next.astype(float) - curr.astype(float))
        motion3 = np.abs(next2.astype(float) - next.astype(float))

        # In true dot crawl, the pattern moves consistently
        # Calculate correlation between motion fields
        masked_motion1 = motion1[mask > 0]
        masked_motion2 = motion2[mask > 0]
        masked_motion3 = motion3[mask > 0]

        if len(masked_motion1) > 0:
            # Check for consistent motion pattern
            correlation12 = np.corrcoef(masked_motion1.flatten(), masked_motion2.flatten())[0, 1]
            correlation23 = np.corrcoef(masked_motion2.flatten(), masked_motion3.flatten())[0, 1]

            # High correlation indicates crawling motion
            if not np.isnan(correlation12) and not np.isnan(correlation23):
                return (abs(correlation12) + abs(correlation23)) / 2

        return 0
