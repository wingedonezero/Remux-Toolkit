# remux_toolkit/tools/video_ab_comparator/detectors/audio.py

import subprocess
import json
import re
from .base_detector import BaseDetector
from ..core.source import VideoSource

class AudioDetector(BaseDetector):
    """Analyzes the primary audio stream for key metrics."""

    @property
    def issue_name(self) -> str:
        return "Audio Analysis"

    def run(self, source: VideoSource) -> dict:
        """Probes audio stream and measures loudness."""
        # Find the first audio stream
        audio_stream_info = None
        for s in source.info.streams:
            if s.codec_type == 'audio':
                audio_stream_info = s
                break

        if not audio_stream_info:
            return {'score': -1, 'summary': 'No audio stream found'}

        # --- Measure Loudness using ffmpeg's ebur128 filter ---
        loudness = "N/A"
        try:
            cmd = [
                "ffmpeg", "-nostats", "-i", str(source.path),
                "-map", f"0:{audio_stream_info.index}",
                "-filter:a", "ebur128", "-t", "120",  # Analyze 2 mins
                "-f", "null", "-"
            ]
            # FIX: valid stdout/stderr capture
            result = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            output = result.stdout

            # Find the Integrated loudness value in the output
            match = re.search(r"Integrated loudness:\s+I:\s+(-?\d+\.\d+)\s+LUFS", output)
            if match:
                loudness = f"{float(match.group(1)):.1f} LUFS"
        except Exception:
            pass  # Loudness measurement fails silently

        summary_parts = [
            f"Codec: {audio_stream_info.codec_name}",
            f"Loudness: {loudness}",
        ]

        # Note: This is a multi-part report, not a single score.
        return {
            'score': 0,  # Not a scored issue
            'summary': " | ".join(summary_parts),
            'data': {
                'Codec': audio_stream_info.codec_name,
                'Loudness': loudness
            }
        }
