import numpy as np
from pathlib import Path
from typing import Tuple, Optional, List
from scenedetect import open_video, SceneManager
from scenedetect.detectors import ContentDetector
from remux_toolkit.tools.video_renamer.core.matcher import BaseMatcher

class SceneDetectionMatcher(BaseMatcher):
    """Scene-based video matching"""

    def __init__(self, cache, config, app_data_dir: Path):
        super().__init__(cache, config, app_data_dir)

    def compare(self, ref_path: Path, remux_path: Path, language: Optional[str] = None) -> Tuple[float, str]:
        ref_scenes = self._get_scene_list(ref_path)
        remux_scenes = self._get_scene_list(remux_path)

        if not ref_scenes or not remux_scenes:
            return 0.0, "Failed to detect scenes"

        similarity = self._compare_scene_patterns(ref_scenes, remux_scenes)
        info = f"Scene pattern matching"
        return similarity, info

    def _get_scene_list(self, path: Path) -> Optional[List[float]]:
        cached = self.cache.get_scenes(path)
        if cached:
            return cached

        try:
            video = open_video(str(path))
            scene_manager = SceneManager()
            scene_manager.add_detector(ContentDetector(threshold=30.0))
            scene_manager.detect_scenes(video, show_progress=False)
            scene_list = scene_manager.get_scene_list()
            durations = []
            for start, end in scene_list:
                durations.append(end.get_seconds() - start.get_seconds())
            if durations:
                self.cache.set_scenes(path, durations)
            return durations
        except Exception:
            return None

    def _compare_scene_patterns(self, scenes1: List[float], scenes2: List[float]) -> float:
        total1, total2 = sum(scenes1), sum(scenes2)
        if total1 == 0 or total2 == 0:
            return 0.0

        norm1 = [s / total1 for s in scenes1]
        norm2 = [s / total2 for s in scenes2]

        try:
            import librosa
            arr1 = np.array(norm1).reshape(-1, 1)
            arr2 = np.array(norm2).reshape(-1, 1)
            D, _ = librosa.sequence.dtw(arr1.T, arr2.T)
            distance = D[-1, -1]
            return float(1 / (1 + distance))
        except ImportError:
            min_len = min(len(norm1), len(norm2))
            if min_len < 3: return 0.0
            correlation = np.corrcoef(norm1[:min_len], norm2[:min_len])[0, 1]
            return max(0, correlation)
