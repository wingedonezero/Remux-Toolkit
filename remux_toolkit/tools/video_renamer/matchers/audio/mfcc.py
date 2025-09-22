import numpy as np
import librosa
from pathlib import Path
from typing import Tuple, Optional
from remux_toolkit.tools.video_renamer.core.matcher import BaseMatcher
from remux_toolkit.tools.video_renamer.utils.media import extract_audio_segment, get_media_duration

class MFCCMatcher(BaseMatcher):
    """Lightweight MFCC-based audio matching"""

    def __init__(self, cache, config, app_data_dir: Path):
        super().__init__(cache, config, app_data_dir)

    def compare(self, ref_path: Path, remux_path: Path, language: Optional[str] = None) -> Tuple[float, str]:
        ref_features = self._get_mfcc_features(ref_path, language)
        remux_features = self._get_mfcc_features(remux_path, language)
        if ref_features is None or remux_features is None:
            return 0.0, "Failed to extract MFCC features"
        similarity = self._cosine_similarity(ref_features, remux_features)
        return similarity, f"MFCC similarity"

    def _get_mfcc_features(self, path: Path, language: Optional[str]) -> Optional[np.ndarray]:
        stream_idx = self.get_audio_stream_index(path, language)
        if stream_idx is None: return None

        cached = self.cache.get_mfcc(path, stream_idx)
        if cached is not None: return cached

        sr = 22050
        audio = self.cache.get_audio(path, stream_idx, sr)
        if audio is None:
            duration = get_media_duration(path)
            duration_limit = 120

            # --- MODIFICATION: Use percentage from config ---
            start_percent = self.config.get('analysis_start_percent', 15) / 100.0
            start_time = 0
            if duration and duration > duration_limit:
                 start_time = duration * start_percent
            # --- END MODIFICATION ---

            audio = extract_audio_segment(path, stream_idx, sr, start_time=start_time, duration_limit=duration_limit)
            if audio is None:
                return None

        try:
            mfccs = librosa.feature.mfcc(y=audio, sr=sr, n_mfcc=13)
            features = np.mean(mfccs, axis=1)
            self.cache.set_mfcc(path, stream_idx, features)
            return features
        except Exception:
            return None

    def _cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        dot_product = np.dot(a, b)
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0: return 0.0
        return float(dot_product / (norm_a * norm_b))
