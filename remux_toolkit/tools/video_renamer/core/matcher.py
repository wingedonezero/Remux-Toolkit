"""
core/matcher.py - Base matcher interface
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Tuple, Optional

class BaseMatcher(ABC):
    """Base class for all matching implementations"""

    def __init__(self, cache, config, app_data_dir: Path):
        self.cache = cache
        self.config = config
        self.app_data_dir = app_data_dir
        self._running = True

    def stop(self):
        """Stop the matching process"""
        self._running = False

    @abstractmethod
    def compare(self, ref_path: Path, remux_path: Path, language: Optional[str] = None) -> Tuple[float, str]:
        """
        Compare two files and return similarity score
        Returns: (confidence 0-1, info string)
        """
        pass

    def get_audio_stream_index(self, path: Path, language: Optional[str] = None) -> Optional[int]:
        """Helper to find the right audio stream, with clear logic and fallback."""
        from remux_toolkit.tools.video_renamer.utils.media import get_stream_info

        all_streams = self.cache.get_stream_info(path)
        if all_streams is None:
            all_streams = get_stream_info(path)
            self.cache.set_stream_info(path, all_streams)

        if not all_streams:
            return None

        audio_streams = [
            (s['index'], s) for s in all_streams if s.get('codec_type') == 'audio'
        ]

        if not audio_streams:
            return None

        if not language:
            return audio_streams[0][0]

        candidates = []
        for abs_index, stream in audio_streams:
            stream_lang = stream.get('tags', {}).get('language', '').lower()
            if stream_lang == language:
                title = stream.get('tags', {}).get('title', '').lower()
                is_non_commentary = 'commentary' not in title
                candidates.append({'index': abs_index, 'non_comm': is_non_commentary})

        if candidates:
            candidates.sort(key=lambda x: x['non_comm'], reverse=True)
            return candidates[0]['index']

        # Fallback to the first audio stream if no language match is found
        return audio_streams[0][0]
