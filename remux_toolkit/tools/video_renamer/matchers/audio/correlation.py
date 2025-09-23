import numpy as np
from scipy.signal import correlate
from pathlib import Path
from typing import Tuple, Optional, List
import statistics
import subprocess

from remux_toolkit.tools.video_renamer.core.matcher import BaseMatcher

class CorrelationMatcher(BaseMatcher):
    """
    Audio correlation matching using a pre-processed reference 'template' method,
    based on the user-provided, proven logic.
    """
    def _decode_audio_for_corr(self, file_path: Path, stream_index: int) -> Optional[np.ndarray]:
        """
        Private helper to decode audio using the exact, high-quality settings needed for correlation.
        """
        try:
            cmd = [
                'ffmpeg', '-nostdin', '-v', 'error',
                '-i', str(file_path),
                '-map', f'0:{stream_index}',
                '-resampler', 'soxr', # Use the high-quality SoX resampler
                '-ac', '1',
                '-ar', '48000',
                '-f', 'f32le',
                '-'
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            if result.returncode == 0 and result.stdout:
                return np.frombuffer(result.stdout, dtype=np.float32)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    def _calculate_chunk_times(self, duration_s: float) -> Optional[List[float]]:
        chunk_count = 20
        chunk_dur_s = 15.0
        start_pct, end_pct = 5.0, 95.0

        scan_start_s = duration_s * (start_pct / 100.0)
        scan_end_s = duration_s * (end_pct / 100.0)

        scannable_duration = (scan_end_s - scan_start_s) - chunk_dur_s
        if scannable_duration < 0:
            return None

        return np.linspace(scan_start_s, scan_start_s + scannable_duration, chunk_count).tolist()

    def get_template(self, file_path: Path, language: Optional[str]) -> Optional[dict]:
        stream_idx = self.get_audio_stream_index(file_path, language)
        if stream_idx is None:
            return None

        # --- FIXED: Use the new internal, high-quality decoder ---
        full_audio = self._decode_audio_for_corr(file_path, stream_idx)

        if full_audio is None or len(full_audio) == 0:
            return None

        sample_rate = 48000
        duration_s = len(full_audio) / float(sample_rate)

        chunk_times = self._calculate_chunk_times(duration_s)
        if not chunk_times:
            return None

        chunks = self._extract_chunks_at_times(full_audio, chunk_times, sample_rate)

        return {'chunks': chunks, 'times': chunk_times}

    def _extract_chunks_at_times(self, full_audio: np.ndarray, start_times: List[float], sample_rate: int) -> List[np.ndarray]:
        chunks = []
        chunk_dur_s = 15.0
        chunk_len_samples = int(chunk_dur_s * sample_rate)

        for start_s in start_times:
            start_sample = int(start_s * sample_rate)
            end_sample = start_sample + chunk_len_samples
            if end_sample <= len(full_audio):
                chunks.append(full_audio[start_sample:end_sample])
        return chunks

    def compare_templates(self, ref_chunks: List[np.ndarray], remux_chunks: List[np.ndarray]) -> Tuple[float, float]:
        accepted_delays_ms = []
        num_pairs = min(len(ref_chunks), len(remux_chunks))

        MIN_CHUNK_CONFIDENCE = 0.70

        if num_pairs == 0:
            return 0.0, 0.0

        for i in range(num_pairs):
            ref_chunk = ref_chunks[i]
            remux_chunk = remux_chunks[i]

            r = (ref_chunk - np.mean(ref_chunk)) / (np.std(ref_chunk) + 1e-9)
            t = (remux_chunk - np.mean(remux_chunk)) / (np.std(remux_chunk) + 1e-9)
            c = correlate(r, t, mode='full', method='fft')

            k = np.argmax(np.abs(c))

            score = np.abs(c[k]) / (np.sqrt(np.sum(r**2) * np.sum(t**2)) + 1e-9)

            if score >= MIN_CHUNK_CONFIDENCE:
                lag_samples = k - (len(t) - 1)
                accepted_delays_ms.append((lag_samples / 48000.0) * 1000.0)

        # --- Use the "close" version's scoring logic ---
        MIN_ACCEPTED_CHUNKS = 15

        if len(accepted_delays_ms) >= MIN_ACCEPTED_CHUNKS:
            final_score = len(accepted_delays_ms) / num_pairs
            median_delay = statistics.median(accepted_delays_ms)
            return final_score, median_delay
        else:
            return 0.0, 0.0

    def compare(self, ref_path: Path, remux_path: Path, language: Optional[str] = None) -> Tuple[float, str]:
        # This method is not used by the new pipeline.
        return 0.0, "CorrelationMatcher requires the batch pipeline."
