import numpy as np
import imagehash
from PIL import Image
from pathlib import Path
from typing import Tuple, Optional, List
from remux_toolkit.tools.video_renamer.core.matcher import BaseMatcher
from remux_toolkit.tools.video_renamer.utils.media import extract_frames, get_media_duration

class PerceptualHashMatcher(BaseMatcher):
    """Improved perceptual hash video matching"""

    def __init__(self, cache, config, app_data_dir: Path):
        super().__init__(cache, config, app_data_dir)

    def compare(self, ref_path: Path, remux_path: Path, language: Optional[str] = None) -> Tuple[float, str]:
        ref_hashes = self._get_video_hashes(ref_path)
        remux_hashes = self._get_video_hashes(remux_path)
        if not ref_hashes or not remux_hashes:
            return 0.0, "Failed to extract video frames"
        similarity, offset = self._compare_hash_sequences(ref_hashes, remux_hashes)
        info = f"Video pHash, offset={offset}"
        return similarity, info

    def _get_video_hashes(self, path: Path) -> Optional[List]:
        cached = self.cache.get_video_hashes(path, "phash")
        if cached: return cached

        n_frames = 25
        duration = self.cache.get_duration(path)
        if not duration:
            from remux_toolkit.tools.video_renamer.utils.media import get_media_duration
            duration = get_media_duration(path)
            if duration: self.cache.set_duration(path, duration)
            else: return None

        # --- MODIFICATION: Use percentage from config ---
        edge_skip_percent = self.config.get('analysis_start_percent', 15) / 100.0
        # Ensure skip is not more than 40% to leave a valid middle section
        edge_skip = min(edge_skip_percent, 0.4)
        # --- END MODIFICATION ---

        start = duration * edge_skip
        end = duration * (1 - edge_skip)

        # Ensure start is not after end for very short files
        if start >= end:
            start = 0
            end = duration

        timestamps = np.linspace(start, end, n_frames)
        hashes = []

        for ts in timestamps:
            frame = extract_frames(path, [ts])
            if frame:
                try:
                    img = Image.fromarray(frame[0])
                    ph = imagehash.phash(img, hash_size=16)
                    dh = imagehash.dhash(img, hash_size=16)
                    hashes.append((str(ph), str(dh)))
                except Exception: continue

        if hashes: self.cache.set_video_hashes(path, "phash", hashes)
        return hashes if len(hashes) > 10 else None

    def _compare_hash_sequences(self, seq1: List, seq2: List) -> Tuple[float, int]:
        best_similarity = 0
        best_offset = 0
        for offset in range(-5, 6):
            if offset >= 0: s1, s2 = seq1[offset:], seq2
            else: s1, s2 = seq1, seq2[-offset:]
            min_len = min(len(s1), len(s2))
            if min_len < 10: continue
            s1, s2 = s1[:min_len], s2[:min_len]
            similarities = []
            for (ph1, dh1), (ph2, dh2) in zip(s1, s2):
                ph_sim = 1 - (bin(int(ph1, 16) ^ int(ph2, 16)).count('1') / (len(ph1) * 4))
                dh_sim = 1 - (bin(int(dh1, 16) ^ int(dh2, 16)).count('1') / (len(dh1) * 4))
                similarities.append((ph_sim + dh_sim) / 2)
            avg_similarity = np.mean(similarities)
            if avg_similarity > best_similarity:
                best_similarity = avg_similarity
                best_offset = offset
        return best_similarity, best_offset
