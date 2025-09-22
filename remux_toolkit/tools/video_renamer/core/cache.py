"""
core/cache.py - Memory management and caching
"""

import threading
from pathlib import Path
from typing import Dict, Optional, Any, Tuple
import numpy as np

class MediaCache:
    """Centralized cache for media data to avoid re-processing"""

    def __init__(self, max_audio_mb: int = 500):
        self._lock = threading.RLock()
        self.max_audio_bytes = max_audio_mb * 1024 * 1024

        # Caches
        self._duration_cache: Dict[Path, float] = {}
        self._stream_info_cache: Dict[Path, list] = {}
        self._audio_cache: Dict[Tuple[Path, int, int], np.ndarray] = {}
        self._audio_cache_size = 0

        # Video caches
        self._video_hash_cache: Dict[Tuple[Path, str], Any] = {}
        self._scene_cache: Dict[Path, list] = {}

        # Audio fingerprint caches
        self._chromaprint_cache: Dict[Tuple[Path, int], str] = {}
        self._mfcc_cache: Dict[Tuple[Path, int], np.ndarray] = {}

    def clear(self):
        """Clear all caches"""
        with self._lock:
            self._duration_cache.clear()
            self._stream_info_cache.clear()
            self._audio_cache.clear()
            self._audio_cache_size = 0
            self._video_hash_cache.clear()
            self._scene_cache.clear()
            self._chromaprint_cache.clear()
            self._mfcc_cache.clear()

    def get_duration(self, path: Path) -> Optional[float]:
        """Get cached duration or None"""
        return self._duration_cache.get(path)

    def set_duration(self, path: Path, duration: float):
        """Cache duration"""
        with self._lock:
            self._duration_cache[path] = duration

    def get_stream_info(self, path: Path) -> Optional[list]:
        """Get cached stream info or None"""
        return self._stream_info_cache.get(path)

    def set_stream_info(self, path: Path, info: list):
        """Cache stream info"""
        with self._lock:
            self._stream_info_cache[path] = info

    def get_audio(self, path: Path, stream_idx: int, sample_rate: int) -> Optional[np.ndarray]:
        """Get cached audio or None"""
        key = (path, stream_idx, sample_rate)
        return self._audio_cache.get(key)

    def set_audio(self, path: Path, stream_idx: int, sample_rate: int, audio: np.ndarray):
        """Cache audio with memory management"""
        with self._lock:
            key = (path, stream_idx, sample_rate)

            # Check if we need to evict old entries
            audio_bytes = audio.nbytes
            if self._audio_cache_size + audio_bytes > self.max_audio_bytes:
                self._evict_audio(audio_bytes)

            self._audio_cache[key] = audio
            self._audio_cache_size += audio_bytes

    def _evict_audio(self, needed_bytes: int):
        """Evict oldest audio entries to make room"""
        # Simple FIFO eviction
        while self._audio_cache and self._audio_cache_size + needed_bytes > self.max_audio_bytes:
            key = next(iter(self._audio_cache))
            audio = self._audio_cache.pop(key)
            self._audio_cache_size -= audio.nbytes

    def get_video_hashes(self, path: Path, method: str) -> Optional[Any]:
        """Get cached video hashes"""
        key = (path, method)
        return self._video_hash_cache.get(key)

    def set_video_hashes(self, path: Path, method: str, hashes: Any):
        """Cache video hashes"""
        with self._lock:
            key = (path, method)
            self._video_hash_cache[key] = hashes

    def get_scenes(self, path: Path) -> Optional[list]:
        """Get cached scene list"""
        return self._scene_cache.get(path)

    def set_scenes(self, path: Path, scenes: list):
        """Cache scene list"""
        with self._lock:
            self._scene_cache[path] = scenes

    def get_chromaprint(self, path: Path, stream_idx: int) -> Optional[str]:
        """Get cached chromaprint fingerprint"""
        key = (path, stream_idx)
        return self._chromaprint_cache.get(key)

    def set_chromaprint(self, path: Path, stream_idx: int, fingerprint: str):
        """Cache chromaprint fingerprint"""
        with self._lock:
            key = (path, stream_idx)
            self._chromaprint_cache[key] = fingerprint

    def get_mfcc(self, path: Path, stream_idx: int) -> Optional[np.ndarray]:
        """Get cached MFCC features"""
        key = (path, stream_idx)
        return self._mfcc_cache.get(key)

    def set_mfcc(self, path: Path, stream_idx: int, features: np.ndarray):
        """Cache MFCC features"""
        with self._lock:
            key = (path, stream_idx)
            self._mfcc_cache[key] = features
