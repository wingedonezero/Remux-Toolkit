# ===========================================
# utils/config.py - Configuration management
# ===========================================

import json
from pathlib import Path
from typing import Dict, Any

class Config:
    """Application configuration manager"""

    def __init__(self, config_file: str = "video_renamer_settings.json"):
        self.config_file = Path(config_file)
        self.defaults = {
            'ref_folder': '',
            'remux_folder': '',
            'language': '',
            'mode': 'Correlation (Audio)',
            'confidence': 75,
            'panako_jar': '',
            'cache_size_mb': 500,

            # --- NEW SETTING ---
            'analysis_start_percent': 15,

            # Correlation settings
            'correlation_chunks': 10,
            'correlation_chunk_duration': 15,
            'correlation_min_valid': 6,

            # Video settings
            'video_frames': 25,
            'video_hash_size': 16,

            # Audio settings
            'audio_sample_rate': 48000,
            'audio_duration_tolerance': 5.0
        }

    def load(self) -> Dict[str, Any]:
        """Load configuration from file"""
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    settings = json.load(f)
                    return {**self.defaults, **settings}
            except (json.JSONDecodeError, IOError):
                pass
        return self.defaults.copy()

    def save(self, settings: Dict[str, Any]):
        """Save configuration to file"""
        try:
            to_save = {}
            for key, value in settings.items():
                if key in self.defaults and value != self.defaults[key]:
                    to_save[key] = value
                elif key not in self.defaults:
                    to_save[key] = value

            with open(self.config_file, 'w') as f:
                json.dump(to_save, f, indent=2)
        except IOError:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value"""
        settings = self.load()
        return settings.get(key, default)
