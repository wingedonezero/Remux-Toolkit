# remux_toolkit/tools/video_ab_comparator/detectors/audio.py

import subprocess
import json
import re
from .base_detector import BaseDetector
from ..core.source import VideoSource
from typing import List
import numpy as np

class AudioDetector(BaseDetector):
    @property
    def issue_name(self) -> str:
        return "Audio Analysis"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        audio_stream_info = next((s for s in source.info.streams if s.codec_type == 'audio'), None)

        if not audio_stream_info:
            return {'score': -1, 'summary': 'No audio stream found'}

        loudness = "N/A"
        try:
            cmd = [
                "ffmpeg", "-nostats", "-i", str(source.path),
                "-map", f"0:{audio_stream_info.index}",
                "-filter:a", "ebur128", "-t", "120",
                "-f", "null", "-"
            ]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            output = result.stdout
            match = re.search(r"Integrated loudness:\s+I:\s+(-?\d+\.\d+)\s+LUFS", output)
            if match:
                loudness = f"{float(match.group(1)):.1f} LUFS"
        except Exception:
            pass

        summary_parts = [f"Codec: {audio_stream_info.codec_name}", f"Loudness: {loudness}"]

        return {
            'score': 0,
            'summary': " | ".join(summary_parts),
            'data': {'Codec': audio_stream_info.codec_name, 'Loudness': loudness}
        }
