# remux_toolkit/tools/video_ab_comparator/detectors/upscale.py

from .base_detector import BaseDetector
from ..core.source import VideoSource
import cv2
import numpy as np
from typing import List, Optional, Tuple
from scipy import fftpack

class UpscaleDetector(BaseDetector):
    @property
    def issue_name(self) -> str:
        return "Upscaled Video"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        if not source.info or not source.info.streams:
            return {'score': -1, 'summary': 'No video stream found'}

        video_stream = next((s for s in source.info.streams if s.codec_type == 'video'), None)
        if not video_stream or not video_stream.resolution:
            return {'score': -1, 'summary': 'Could not determine resolution'}

        width, height = map(int, video_stream.resolution.split('x'))

        # Quick check for SD content
        if height < 720:
            return {'score': 0, 'summary': f'Native SD ({height}p)'}

        if height >= 1080:
            try:
                if not frame_list or len(frame_list) < 3:
                    return {'score': -1, 'summary': 'Insufficient frames'}

                detected_resolutions = []
                confidence_scores = []

                # Analyze multiple frames for consistency
                for frame in frame_list[::max(1, len(frame_list)//5)][:5]:
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)

                    # Use DCT method (inspired by resdet)
                    detected_height, height_conf = self._detect_source_resolution_dct(gray, height)
                    detected_width, width_conf = self._detect_source_resolution_dct(gray.T, width)

                    if detected_height and detected_width:
                        detected_resolutions.append((detected_width, detected_height))
                        confidence_scores.append((height_conf + width_conf) / 2)

                if detected_resolutions and confidence_scores:
                    # Find most common detection with weighted confidence
                    from collections import Counter
                    res_counts = Counter(detected_resolutions)

                    # Weight by confidence
                    weighted_detections = {}
                    for i, res in enumerate(detected_resolutions):
                        if res not in weighted_detections:
                            weighted_detections[res] = []
                        weighted_detections[res].append(confidence_scores[i])

                    # Find best detection
                    best_res = None
                    best_weighted_score = 0

                    for res, confs in weighted_detections.items():
                        count = len(confs)
                        avg_conf = np.mean(confs)
                        weighted_score = count * avg_conf

                        if weighted_score > best_weighted_score:
                            best_weighted_score = weighted_score
                            best_res = res

                    if best_res:
                        detected_w, detected_h = best_res

                        # Calculate upscale factor
                        if detected_h < height * 0.95:  # At least 5% difference
                            upscale_factor = height / detected_h
                            confidence = min(100, (best_weighted_score / len(frame_list)) * 100)

                            # Score calculation - not linear to avoid everything being 100
                            # Lower upscale factors get lower scores
                            if upscale_factor < 1.5:
                                # Mild upscaling (1080p from 900p, etc)
                                base_score = 30
                            elif upscale_factor < 2.0:
                                # Moderate upscaling (1080p from 720p)
                                base_score = 50
                            elif upscale_factor < 3.0:
                                # Heavy upscaling (1080p from 480p)
                                base_score = 70
                            else:
                                # Extreme upscaling
                                base_score = 85

                            # Adjust by confidence
                            score = min(100, base_score * (confidence / 100))

                            summary = f'Upscaled from {detected_w}x{detected_h} ({upscale_factor:.2f}x) - Confidence: {confidence:.0f}%'
                        else:
                            score = 0
                            summary = f'Native {width}x{height}'
                    else:
                        score = 0
                        summary = f'Native {width}x{height} (no upscaling detected)'
                else:
                    # Fallback to frequency analysis
                    score, summary = self._fallback_frequency_analysis(frame_list, width, height)

                return {'score': score, 'summary': summary, 'worst_frame_timestamp': 0.0}

            except Exception as e:
                return {'score': -1, 'summary': f'Error during analysis: {e}'}

        return {'score': 0, 'summary': 'Standard Definition'}

    def _detect_source_resolution_dct(self, channel: np.ndarray, current_resolution: int) -> Tuple[Optional[int], float]:
        """
        Detect source resolution using DCT zero-crossing method (resdet approach).

        Traditional upscaling creates inversions in the frequency domain at predictable points.
        Returns: (detected_resolution, confidence_score)
        """
        try:
            # Apply 2D DCT
            dct = fftpack.dct(fftpack.dct(channel.T, norm='ortho').T, norm='ortho')

            # Get magnitude spectrum
            magnitude = np.abs(dct)

            # Look for zero-crossings / inversions in high frequency bands
            test_results = []

            # Test common SD/HD resolutions
            if current_resolution > 1000:  # HD content
                test_resolutions = [480, 486, 576, 720, 900, 1080]
            else:
                test_resolutions = [480, 486, 576, 720]

            for test_res in test_resolutions:
                if test_res >= current_resolution * 0.95:  # Skip if too close
                    continue

                # Calculate where frequency boundary should be for this resolution
                freq_position = int((test_res / current_resolution) * magnitude.shape[0])

                if freq_position < 10 or freq_position >= magnitude.shape[0] - 10:
                    continue

                # Check for characteristic inversion pattern at boundary
                # Upscaling creates a noticeable dip in magnitude at the source resolution boundary

                # Get average magnitude at the boundary
                boundary_window = magnitude[freq_position-3:freq_position+3, :]
                avg_at_boundary = np.mean(boundary_window)

                # Compare to nearby frequencies
                before_window = magnitude[max(0, freq_position-15):freq_position-5, :]
                after_window = magnitude[freq_position+5:min(magnitude.shape[0], freq_position+15), :]

                if before_window.size == 0 or after_window.size == 0:
                    continue

                avg_before = np.mean(before_window)
                avg_after = np.mean(after_window)
                avg_nearby = (avg_before + avg_after) / 2

                # Upscaling creates a dip at the boundary
                if avg_nearby > 0 and avg_at_boundary < avg_nearby * 0.75:
                    # Calculate confidence based on how pronounced the dip is
                    dip_strength = (avg_nearby - avg_at_boundary) / avg_nearby
                    confidence = min(100, dip_strength * 150)
                    test_results.append((test_res, confidence))

            if test_results:
                # Return resolution with strongest inversion signal
                best_detection = max(test_results, key=lambda x: x[1])
                return best_detection[0], best_detection[1]

            return None, 0.0

        except Exception as e:
            print(f"DCT detection failed: {e}")
            return None, 0.0

    def _fallback_frequency_analysis(self, frame_list, width, height):
        """Fallback method using simple frequency analysis."""
        upscale_indicators = []

        for frame in frame_list[:5]:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            # 1. FFT analysis for high-frequency content
            f_transform = fftpack.fft2(gray)
            f_shift = fftpack.fftshift(f_transform)
            magnitude_spectrum = np.abs(f_shift)

            h, w = magnitude_spectrum.shape
            center_h, center_w = h//2, w//2

            # Check high frequency content
            total_energy = np.sum(magnitude_spectrum**2)

            # Create mask for high frequencies (outer ring)
            high_freq_mask = np.zeros_like(magnitude_spectrum)
            cv2.circle(high_freq_mask, (center_w, center_h), min(h, w)//3, 1, -1)
            high_freq_mask = 1 - high_freq_mask

            high_freq_energy = np.sum((magnitude_spectrum * high_freq_mask)**2)
            high_freq_ratio = high_freq_energy / (total_energy + 1e-10)

            # 2. Edge sharpness analysis
            edges = cv2.Canny(gray, 50, 150)
            edge_density = np.sum(edges > 0) / edges.size

            # 3. Texture detail analysis
            laplacian = cv2.Laplacian(gray, cv2.CV_64F)
            texture_variance = np.var(laplacian)

            # Combine indicators
            # Native HD should have: high freq content, sharp edges, good texture
            # Upscaled content lacks these

            indicators = {
                'low_hf_content': high_freq_ratio < 0.12,
                'low_edge_density': edge_density < 0.05,
                'low_texture': texture_variance < 100
            }

            upscale_indicators.append(sum(indicators.values()))

        # Calculate final score
        avg_indicators = np.mean(upscale_indicators)

        # Score based on how many indicators suggest upscaling
        # 0 indicators = likely native (score 0)
        # 1 indicator = possibly upscaled (score ~25)
        # 2 indicators = likely upscaled (score ~50)
        # 3 indicators = definitely upscaled (score ~75)

        score = min(100, avg_indicators * 25)

        if score > 60:
            summary = f'Likely Upscaled (Indicators: {avg_indicators:.1f}/3)'
        elif score > 35:
            summary = f'Possibly Upscaled (Indicators: {avg_indicators:.1f}/3)'
        else:
            summary = f'Likely Native HD (Indicators: {avg_indicators:.1f}/3)'

        return score, summary
