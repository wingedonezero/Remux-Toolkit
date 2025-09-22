import numpy as np
from scipy.ndimage import maximum_filter
from hashlib import sha1
from pathlib import Path
from typing import Tuple, Optional, Any, Set, Dict
from collections import defaultdict

from remux_toolkit.tools.video_renamer.core.matcher import BaseMatcher
from remux_toolkit.tools.video_renamer.utils.media import get_media_duration, extract_audio_segment

# --- Constants for the algorithm ---
SPECTROGRAM_FFT_SIZE = 4096
SPECTROGRAM_WINDOW_SIZE = 4096
SPECTROGRAM_OVERLAP_RATIO = 0.5
PEAK_NEIGHBORHOOD_SIZE = 20
TARGET_ZONE_ANCHOR_DISTANCE_TIME = 10
TARGET_ZONE_HEIGHT_FREQ = 200
TARGET_ZONE_WIDTH_TIME = 60

class PeakMatcher(BaseMatcher):
    """
    A self-contained audio matcher inspired by Panako/Shazam, using peak-finding,
    combinatorial hashing, and temporal chaining for accuracy.
    """

    def __init__(self, cache, config, app_data_dir: Path):
        super().__init__(cache, config, app_data_dir)

    def compare(self, ref_path: Path, remux_path: Path, language: Optional[str] = None) -> Tuple[float, str]:
        raise NotImplementedError("PeakMatcher should be used via the batch pipeline.")

    def get_fingerprint(self, path: Path, language: Optional[str] = None) -> Optional[Any]:
        """Generates a fingerprint (a dictionary of hash -> time offset) for an audio file."""
        try:
            import librosa
        except ImportError:
            print("ERROR: librosa is not installed. Please run 'pip install librosa'")
            return None

        cached_fp = self.cache.get_peak_fingerprint(path, language)
        if cached_fp is not None:
            return cached_fp

        duration = get_media_duration(path)
        if not duration: return None

        stream_idx = self.get_audio_stream_index(path, language)
        if stream_idx is None: return None

        start_percent = self.config.get('analysis_start_percent', 15) / 100.0
        start_time = duration * start_percent
        analysis_duration = 120

        audio = extract_audio_segment(path, stream_idx, 22050, start_time=start_time, duration_limit=analysis_duration)
        if audio is None or len(audio) == 0: return None

        try:
            spectrogram = np.abs(librosa.stft(
                audio, n_fft=SPECTROGRAM_FFT_SIZE,
                hop_length=int(SPECTROGRAM_WINDOW_SIZE * (1 - SPECTROGRAM_OVERLAP_RATIO)),
                win_length=SPECTROGRAM_WINDOW_SIZE
            ))
        except Exception as e:
            print(f"Error creating spectrogram for {path.name}: {e}")
            return None

        local_max = maximum_filter(spectrogram, size=PEAK_NEIGHBORHOOD_SIZE, mode='constant')
        peaks = (spectrogram == local_max) & (spectrogram > np.median(spectrogram) * 1.5)
        peak_coords = np.argwhere(peaks)
        if len(peak_coords) < 10: return None

        # The fingerprint is now a dictionary mapping a hash to its absolute time offset
        fingerprint: Dict[str, int] = {}
        for freq1, time1 in peak_coords:
            start_time_offset = time1 + TARGET_ZONE_ANCHOR_DISTANCE_TIME
            end_time_offset = start_time_offset + TARGET_ZONE_WIDTH_TIME

            targets = peak_coords[
                (peak_coords[:, 1] >= start_time_offset) &
                (peak_coords[:, 1] < end_time_offset) &
                (np.abs(peak_coords[:, 0] - freq1) < TARGET_ZONE_HEIGHT_FREQ)
            ]

            for freq2, time2 in targets:
                time_delta = time2 - time1
                hash_input = f"{freq1}|{freq2}|{time_delta}".encode('utf-8')
                h = sha1(hash_input).hexdigest()[0:20]
                fingerprint[h] = time1

        self.cache.set_peak_fingerprint(path, language, fingerprint)
        return fingerprint

    def compare_fingerprints(self, fp1: Any, fp2: Any) -> float:
        """
        Compares two fingerprints using temporal chaining to find the most consistent
        sequence of matching hashes.
        """
        if not isinstance(fp1, dict) or not isinstance(fp2, dict): return 0.0

        # Find the hashes that exist in both fingerprints
        matching_hashes = set(fp1.keys()).intersection(set(fp2.keys()))
        if not matching_hashes: return 0.0

        # For each matching hash, calculate the time offset between the two files
        # offset = (time in file 2) - (time in file 1)
        time_offsets = defaultdict(int)
        for h in matching_hashes:
            offset = fp2[h] - fp1[h]
            time_offsets[offset] += 1

        if not time_offsets: return 0.0

        # The best match is the one with the most hashes sharing the same time offset.
        # This is our "chain".
        best_chain_length = max(time_offsets.values())

        # The confidence is the ratio of the longest chain to the total number of hashes
        # in the smaller fingerprint. This measures how much of the file aligns correctly.
        total_hashes = min(len(fp1), len(fp2))
        confidence = best_chain_length / total_hashes if total_hashes > 0 else 0.0

        return confidence
