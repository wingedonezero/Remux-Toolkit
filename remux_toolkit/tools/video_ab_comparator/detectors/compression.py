# remux_toolkit/tools/video_ab_comparator/detectors/compression.py

import cv2
import numpy as np
from .base_detector import BaseDetector
from ..core.source import VideoSource
from typing import List

class BlockingDetector(BaseDetector):
    @property
    def issue_name(self) -> str:
        return "Compression Artifacts"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        v_stream = source.info.video_stream
        if not v_stream:
            return {'score': -1}

        artifact_scores = {
            'blocking': [],
            'mosquito': [],
            'dct_ringing': [],
            'mpeg2': [],
            'h264': []
        }

        for frame_idx, frame in enumerate(frame_list):
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            height, width = gray.shape

            # 1. Traditional blocking detection (8x8 and 16x16 blocks)
            blocking_8 = self._detect_blocking(gray, 8)
            blocking_16 = self._detect_blocking(gray, 16)
            artifact_scores['blocking'].append(max(blocking_8, blocking_16))

            # 2. Mosquito noise detection (around edges)
            mosquito_score = self._detect_mosquito_noise(gray)
            artifact_scores['mosquito'].append(mosquito_score)

            # 3. DCT ringing detection
            dct_score = self._detect_dct_ringing(gray)
            artifact_scores['dct_ringing'].append(dct_score)

            # 4. MPEG-2 specific artifacts (softer blocks, color bleeding)
            mpeg2_score = self._detect_mpeg2_artifacts(frame)
            artifact_scores['mpeg2'].append(mpeg2_score)

            # 5. H.264 specific artifacts (sharper blocks, deblocking filter artifacts)
            h264_score = self._detect_h264_artifacts(frame)
            artifact_scores['h264'].append(h264_score)

        # Compile results
        if not artifact_scores['blocking']:
            return {'score': 0, 'summary': 'Not detected'}

        # Determine codec type based on artifact patterns
        avg_mpeg2 = np.mean(artifact_scores['mpeg2'])
        avg_h264 = np.mean(artifact_scores['h264'])

        codec_type = "MPEG-2" if avg_mpeg2 > avg_h264 else "H.264/AVC"

        # Calculate overall score
        blocking_avg = np.mean(artifact_scores['blocking'])
        mosquito_avg = np.mean(artifact_scores['mosquito'])
        dct_avg = np.mean(artifact_scores['dct_ringing'])

        # Weighted combination
        overall_score = (blocking_avg * 0.4 + mosquito_avg * 0.3 + dct_avg * 0.3)

        # Build summary
        summary_parts = [f"{codec_type} artifacts"]

        if blocking_avg > 20:
            summary_parts.append(f"Blocking: {blocking_avg:.1f}")
        if mosquito_avg > 15:
            summary_parts.append(f"Mosquito: {mosquito_avg:.1f}")
        if dct_avg > 15:
            summary_parts.append(f"DCT ringing: {dct_avg:.1f}")

        worst_idx = np.argmax([blocking_avg, mosquito_avg, dct_avg])
        worst_ts = worst_idx / v_stream.fps if v_stream.fps > 0 else 0

        return {
            'score': overall_score,
            'summary': " | ".join(summary_parts),
            'worst_frame_timestamp': worst_ts
        }

    def _detect_blocking(self, gray: np.ndarray, block_size: int) -> float:
        """Detect blocking artifacts at specific block size."""
        height, width = gray.shape

        # Calculate gradients
        grad_h = np.abs(cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3))
        grad_v = np.abs(cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3))

        # Check strength at block boundaries
        h_boundaries = grad_h[:, ::block_size]
        v_boundaries = grad_v[::block_size, :]

        # Check strength at non-boundaries
        h_non = np.mean([grad_h[:, i::block_size] for i in range(1, block_size)], axis=0)
        v_non = np.mean([grad_v[i::block_size, :] for i in range(1, block_size)], axis=0)

        # Ratio of boundary to non-boundary gradients
        h_ratio = np.mean(h_boundaries) / (np.mean(h_non) + 1e-10)
        v_ratio = np.mean(v_boundaries) / (np.mean(v_non) + 1e-10)

        # Higher ratio indicates more blocking
        score = min(100, max(0, ((h_ratio + v_ratio) - 2) * 20))
        return score

    def _detect_mosquito_noise(self, gray: np.ndarray) -> float:
        """Detect mosquito noise around high-contrast edges."""
        # Find strong edges
        edges = cv2.Canny(gray, 100, 200)

        # Dilate edges to create regions around them
        kernel = np.ones((5, 5), np.uint8)
        dilated_edges = cv2.dilate(edges, kernel)
        edge_surroundings = dilated_edges - edges

        # Check for noise in areas around edges
        noise_mask = edge_surroundings > 0
        if np.sum(noise_mask) == 0:
            return 0

        # Calculate local variance around edges
        local_var = cv2.Laplacian(gray, cv2.CV_64F)
        noise_variance = np.std(local_var[noise_mask])

        # Higher variance around edges indicates mosquito noise
        score = min(100, max(0, (noise_variance - 5) * 10))
        return score

    def _detect_dct_ringing(self, gray: np.ndarray) -> float:
        """Detect DCT ringing artifacts."""
        # Apply DCT to small blocks
        block_size = 8
        h, w = gray.shape
        ringing_scores = []

        for y in range(0, h - block_size, block_size):
            for x in range(0, w - block_size, block_size):
                block = gray[y:y+block_size, x:x+block_size].astype(np.float32)

                # Apply DCT
                dct_block = cv2.dct(block)

                # Check for high-frequency artifacts
                high_freq = np.abs(dct_block[4:, 4:])
                low_freq = np.abs(dct_block[:4, :4])

                if np.mean(low_freq) > 0:
                    ratio = np.mean(high_freq) / np.mean(low_freq)
                    if ratio > 0.1:  # Unexpected high frequency content
                        ringing_scores.append(ratio * 100)

        return np.mean(ringing_scores) if ringing_scores else 0

    def _detect_mpeg2_artifacts(self, frame: np.ndarray) -> float:
        """Detect MPEG-2 specific artifacts."""
        # MPEG-2 tends to have softer blocks and color bleeding
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # Check for soft blocking (gradual transitions at block boundaries)
        blur = cv2.GaussianBlur(gray, (3, 3), 1)
        diff = np.abs(gray.astype(float) - blur.astype(float))

        # MPEG-2 blocks are often at 16x16 boundaries
        block_pattern = np.zeros_like(diff)
        block_pattern[::16, :] = 1
        block_pattern[:, ::16] = 1

        soft_blocks = np.mean(diff * block_pattern)

        # Check for color bleeding
        b, g, r = cv2.split(frame)
        color_bleeding = np.std([np.std(b), np.std(g), np.std(r)])

        score = min(100, soft_blocks * 5 + color_bleeding)
        return score

    def _detect_h264_artifacts(self, frame: np.ndarray) -> float:
        """Detect H.264 specific artifacts."""
        # H.264 has sharper blocks and deblocking filter artifacts
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # Check for sharp blocking (sudden transitions)
        grad = np.abs(cv2.Sobel(gray, cv2.CV_64F, 1, 0)) + np.abs(cv2.Sobel(gray, cv2.CV_64F, 0, 1))

        # H.264 uses 4x4 and 16x16 blocks
        block_4 = self._detect_blocking(gray, 4)

        # Check for deblocking filter artifacts (overly smooth areas)
        blur = cv2.bilateralFilter(gray, 9, 75, 75)
        over_smooth = np.mean(np.abs(gray.astype(float) - blur.astype(float)))

        # H.264 artifacts are sharper
        sharpness = np.std(grad)

        score = min(100, block_4 * 0.5 + (10 - over_smooth) * 5 + sharpness * 0.1)
        return score
