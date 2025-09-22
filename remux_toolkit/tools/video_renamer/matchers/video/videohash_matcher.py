from pathlib import Path
from typing import Tuple, Optional, Any

try:
    from videohash import VideoHash
except ImportError:
    print("ERROR: videohash is not installed. Please run 'pip install videohash'")

from remux_toolkit.tools.video_renamer.core.matcher import BaseMatcher

class VideoHashMatcher(BaseMatcher):
    """
    Video matching using the videohash library, which is based on Facebook's PDQ algorithm.
    """

    def __init__(self, cache, config, app_data_dir: Path):
        super().__init__(cache, config, app_data_dir)

    def compare(self, ref_path: Path, remux_path: Path, language: Optional[str] = None) -> Tuple[float, str]:
        # This matcher uses the fingerprinting pipeline
        raise NotImplementedError("VideoHashMatcher should be used via the batch pipeline.")

    def get_fingerprint(self, path: Path, language: Optional[str] = None) -> Optional[str]:
        """
        Generates a videohash fingerprint string for a video file.
        """
        cached_fp = self.cache.get_videohash(path)
        if cached_fp:
            return cached_fp

        try:
            vh = VideoHash(path=str(path))
            fingerprint = vh.hash_hex
            if fingerprint:
                self.cache.set_videohash(path, fingerprint)
            return fingerprint
        except Exception as e:
            print(f"Error generating videohash for {path.name}: {e}")
            return None

    def compare_fingerprints(self, fp1: Any, fp2: Any) -> float:
        """
        Compares two videohash fingerprint strings and returns a similarity score from 0.0 to 1.0.
        """
        if not isinstance(fp1, str) or not isinstance(fp2, str):
            return 0.0

        try:
            # --- DEFINITIVE FIX: Create VideoHash objects from the hex strings to compare them ---
            # The library overloads the subtraction operator to get the bit difference.
            vh1 = VideoHash(hash_hex=fp1)
            vh2 = VideoHash(hash_hex=fp2)
            bit_difference = vh1 - vh2

            hash_length_bits = len(fp1) * 4
            if hash_length_bits == 0:
                return 0.0

            similarity = (hash_length_bits - bit_difference) / hash_length_bits
            return similarity
        except Exception as e:
            print(f"Error comparing video hashes: {e}")
            return 0.0
