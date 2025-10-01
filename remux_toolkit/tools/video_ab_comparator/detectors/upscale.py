# remux_toolkit/tools/video_ab_comparator/detectors/upscale.py

from .base_detector import BaseDetector
from ..core.source import VideoSource
import cv2
import numpy as np
from typing import List
from scipy import signal, fftpack

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
                if not frame_list or len(frame_list) < 5:
                    return {'score': -1, 'summary': 'Insufficient frames'}

                upscale_scores = []

                # Analyze multiple frames for consistency
                for frame in frame_list[::max(1, len(frame_list)//5)][:5]:
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

                    # 1. Frequency domain analysis
                    f_transform = fftpack.fft2(gray)
                    f_shift = fftpack.fftshift(f_transform)
                    magnitude_spectrum = np.abs(f_shift)

                    # Check high frequency content
                    h, w = magnitude_spectrum.shape
                    center_h, center_w = h//2, w//2

                    # Define frequency bands
                    total_energy = np.sum(magnitude_spectrum**2)
                    high_freq_mask = np.zeros_like(magnitude_spectrum)
                    cv2.circle(high_freq_mask, (center_w, center_h), min(h, w)//3, 1, -1)
                    high_freq_mask = 1 - high_freq_mask
                    high_freq_energy = np.sum((magnitude_spectrum * high_freq_mask)**2)
                    high_freq_ratio = high_freq_energy / (total_energy + 1e-10)

                    # 2. Edge pattern analysis
                    edges = cv2.Canny(gray, 50, 150)

                    # Check for stair-stepping patterns (common in upscaling)
                    kernel_45 = np.array([[0, 0, 1], [0, 1, 0], [1, 0, 0]], dtype=np.float32)
                    kernel_135 = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float32)

                    diag_response_45 = cv2.filter2D(edges.astype(np.float32), -1, kernel_45)
                    diag_response_135 = cv2.filter2D(edges.astype(np.float32), -1, kernel_135)

                    # Check for regular patterns indicating interpolation
                    staircase_score = np.std(diag_response_45) + np.std(diag_response_135)

                    # 3. Gradient consistency check
                    sobelx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
                    sobely = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
                    gradient_magnitude = np.sqrt(sobelx**2 + sobely**2)

                    # Upscaled content has more uniform gradients
                    gradient_variance = np.var(gradient_magnitude[gradient_magnitude > 10])

                    # 4. Local texture analysis
                    texture_scores = []
                    block_size = 32
                    for y in range(0, gray.shape[0] - block_size, block_size):
                        for x in range(0, gray.shape[1] - block_size, block_size):
                            block = gray[y:y+block_size, x:x+block_size]
                            block_std = np.std(block)
                            if block_std > 5:  # Skip uniform areas
                                laplacian = cv2.Laplacian(block, cv2.CV_64F)
                                texture_scores.append(np.var(laplacian))

                    avg_texture = np.mean(texture_scores) if texture_scores else 0

                    # Combine metrics
                    upscale_confidence = 0

                    # Low high-frequency content suggests upscaling
                    if high_freq_ratio < 0.15:
                        upscale_confidence += 30

                    # Regular staircase patterns suggest upscaling
                    if staircase_score > 1000:
                        upscale_confidence += 25

                    # Low gradient variance suggests interpolation
                    if gradient_variance < 500:
                        upscale_confidence += 25

                    # Low texture detail suggests upscaling
                    if avg_texture < 50:
                        upscale_confidence += 20

                    upscale_scores.append(upscale_confidence)

                final_score = np.mean(upscale_scores)

                if final_score > 70:
                    summary = f'Likely Upscaled (Confidence: {final_score:.1f}%)'
                elif final_score > 40:
                    summary = f'Possibly Upscaled (Confidence: {final_score:.1f}%)'
                else:
                    summary = f'Likely Native HD (Confidence: {100-final_score:.1f}%)'

                return {'score': final_score, 'summary': summary, 'worst_frame_timestamp': 0.0}

            except Exception as e:
                return {'score': -1, 'summary': f'Error during analysis: {e}'}

        return {'score': 0, 'summary': 'Standard Definition'}
