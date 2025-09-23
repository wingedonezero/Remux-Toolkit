# remux_toolkit/tools/video_renamer/matchers/audio/correlation.py
import numpy as np
from scipy.signal import correlate
from pathlib import Path
from typing import Tuple, Optional, List
import statistics

from remux_toolkit.tools.video_renamer.core.matcher import BaseMatcher
# --- FIXED: Import the utility function directly ---
from remux_toolkit.tools.video_renamer.utils.media import extract_audio_segment

class CorrelationMatcher(BaseMatcher):
    """
    Audio correlation matching using a pre-processed reference 'template' method,
    based on the user-provided, proven logic.
    """
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

        sample_rate = 48000
        num_channels = 1
        # --- FIXED: Call the imported function correctly and request mono audio ---
        full_audio = extract_audio_segment(file_path, stream_idx, sample_rate, num_channels=num_channels)

        if full_audio is None or len(full_audio) == 0:
            return None

        duration_s = len(full_audio) / float(sample_rate * num_channels)

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

        MIN_MATCH_CONFIDENCE = 0.70

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

            if score >= MIN_MATCH_CONFIDENCE:
                lag_samples = k - (len(t) - 1)
                accepted_delays_ms.append((lag_samples / 48000.0) * 1000.0)

        MIN_ACCEPTED_CHUNKS = 15

        if len(accepted_delays_ms) >= MIN_ACCEPTED_CHUNKS:
            final_score = len(accepted_delays_ms) / num_pairs
            median_delay = statistics.median(accepted_delays_ms)
            return final_score, median_delay
        else:
            return 0.0, 0.0

    def compare(self, ref_path: Path, remux_path: Path, language: Optional[str] = None) -> Tuple[float, str]:
        return 0.0, "CorrelationMatcher requires the batch pipeline."
