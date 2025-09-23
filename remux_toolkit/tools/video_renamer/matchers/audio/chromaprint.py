import subprocess
import json
from pathlib import Path
from typing import Tuple, Optional, Any
from collections import Counter

from remux_toolkit.tools.video_renamer.core.matcher import BaseMatcher
from remux_toolkit.tools.video_renamer.utils.media import get_media_duration

class ChromaprintMatcher(BaseMatcher):
    """Audio fingerprinting using Chromaprint/AcoustID"""

    def __init__(self, cache, config, app_data_dir: Path):
        super().__init__(cache, config, app_data_dir)

    def compare(self, ref_path: Path, remux_path: Path, language: Optional[str] = None) -> Tuple[float, str]:
        # This method is not used by the primary fingerprinting pipeline, but is updated for completeness.
        ref_fp_data = self.get_fingerprint(ref_path, language)
        remux_fp_data = self.get_fingerprint(remux_path, language)

        if not ref_fp_data or not remux_fp_data:
            return 0.0, "Failed to generate full fingerprint for comparison"

        score, offset = self.compare_fingerprints(ref_fp_data, remux_fp_data)
        return score, f"Chromaprint similarity: {score:.1%}, offset={offset:.2f}s"

    def get_fingerprint(self, path: Path, language: Optional[str] = None) -> Optional[Any]:
        """
        Generates a fingerprint using an in-memory buffer to avoid "Broken pipe" errors.
        It processes the full file for typical episodes and caps the duration for longer files.
        """
        stream_idx = self.get_audio_stream_index(path, language)
        if stream_idx is None: return None

        cached = self.cache.get_chromaprint(path, stream_idx)
        if cached: return cached

        try:
            # --- MODIFIED: Process audio as stereo (2 channels) instead of mono ---
            audio_rate, audio_channels, audio_format = 16000, 2, 's16le'

            algorithm = self.config.get('chromaprint_algorithm', 2)

            duration = get_media_duration(path)
            time_limit_args = []
            if duration and duration > 2700: # 45 minutes
                time_limit_args = ['-t', '900'] # 15 minutes

            ffmpeg_cmd = [
                'ffmpeg', '-nostdin', '-v', 'error', '-i', str(path),
                '-map', f'0:{stream_idx}',
                *time_limit_args,
                '-ac', str(audio_channels), # Force stereo output for consistency
                '-ar', str(audio_rate), '-f', audio_format, '-'
            ]

            ffmpeg_process = subprocess.run(ffmpeg_cmd, capture_output=True, timeout=300)
            if ffmpeg_process.returncode != 0:
                print(f"ffmpeg failed for {path.name}: {ffmpeg_process.stderr.decode('utf-8', errors='ignore')}")
                return None

            audio_data = ffmpeg_process.stdout
            if not audio_data:
                return None

            fpcalc_cmd = [
                'fpcalc',
                '-algorithm', str(algorithm),
                '-raw', '-json', '-rate', str(audio_rate),
                '-channels', str(audio_channels), # Tell fpcalc to expect stereo
                '-format', audio_format, '-'
            ]

            fpcalc_process = subprocess.run(fpcalc_cmd, input=audio_data, capture_output=True, timeout=300)
            if fpcalc_process.returncode != 0:
                print(f"fpcalc failed for {path.name}: {fpcalc_process.stderr.decode('utf-8', errors='ignore')}")
                return None

            result = json.loads(fpcalc_process.stdout.decode('utf-8'))
            fingerprint = result.get('fingerprint')

            bytes_per_sample = 2 # for s16le (16 bit)
            calculated_duration = len(audio_data) / (audio_rate * bytes_per_sample * audio_channels)

            if fingerprint and calculated_duration > 0:
                fp_str = ','.join(map(str, fingerprint))
                fp_data = (calculated_duration, fp_str)
                self.cache.set_chromaprint(path, stream_idx, fp_data)
                return fp_data

        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
            print(f"Error generating full fingerprint for {path.name}: {e}")
            pass
        return None

    def compare_fingerprints(self, fp1: Any, fp2: Any) -> Tuple[float, float]:
        """
        Compares two fingerprints using a 1-to-1 sequential chunk comparison.
        """
        if not (isinstance(fp1, (list, tuple)) and len(fp1) == 2 and
                isinstance(fp2, (list, tuple)) and len(fp2) == 2):
            return 0.0, 0.0

        ref_duration, ref_fp_str = fp1
        remux_duration, remux_fp_str = fp2

        ref_v = [int(x) for x in ref_fp_str.split(',')]
        remux_v = [int(x) for x in remux_fp_str.split(',')]

        if not ref_v or not remux_v or ref_duration <= 0 or remux_duration <= 0:
            return 0.0, 0.0

        rate = len(ref_v) / ref_duration
        CHUNK_SECONDS = 60
        STEP_SECONDS = 15
        CHUNK_LEN = int(CHUNK_SECONDS * rate)
        STEP_LEN = int(STEP_SECONDS * rate) if int(STEP_SECONDS * rate) > 0 else 1

        ref_chunks = [ref_v[i:i + CHUNK_LEN] for i in range(0, len(ref_v) - CHUNK_LEN + 1, STEP_LEN)]
        remux_chunks = [remux_v[i:i + CHUNK_LEN] for i in range(0, len(remux_v) - CHUNK_LEN + 1, STEP_LEN)]

        if not ref_chunks or not remux_chunks:
            return 0.0, 0.0

        pair_scores = []
        num_pairs_to_compare = min(len(ref_chunks), len(remux_chunks))

        for i in range(num_pairs_to_compare):
            score = self._calc_similarity(ref_chunks[i], remux_chunks[i])
            pair_scores.append(score)

        if not pair_scores:
            return 0.0, 0.0

        final_score = sum(pair_scores) / len(pair_scores)

        offset_seconds = 0.0

        return final_score, offset_seconds

    def _calc_similarity(self, v1: list, v2: list) -> float:
        """Helper method to calculate bitwise similarity between two integer vectors."""
        min_len = min(len(v1), len(v2))
        if min_len == 0: return 0.0

        v1, v2 = v1[:min_len], v2[:min_len]

        matches, total_bits = 0, 0
        for i1, i2 in zip(v1, v2):
            xor = i1 ^ i2
            matches += 32 - bin(xor).count('1')
            total_bits += 32
        return matches / total_bits if total_bits > 0 else 0.0
