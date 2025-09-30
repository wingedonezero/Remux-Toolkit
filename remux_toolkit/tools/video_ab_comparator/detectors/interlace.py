# remux_toolkit/tools/video_ab_comparator/detectors/interlace.py

import subprocess
import re
from .base_detector import BaseDetector
from ..core.source import VideoSource

class CombingDetector(BaseDetector):
    @property
    def issue_name(self) -> str:
        return "Interlace Combing"

    def run(self, source: VideoSource) -> dict:
        duration = source.info.duration
        if duration < 10:
            return {'score': 0, 'summary': 'Video too short'}

        # Analyze a 60s window from the first third (fast seek is fine here)
        start_time = duration / 3
        command = [
            "ffmpeg", "-nostdin", "-hide_banner",
            "-loglevel", "info",          # <-- must be info so idet prints stats
            "-ss", str(start_time),       # fast seek
            "-i", str(source.path),
            "-t", "60",
            "-vf", "idet",
            "-an", "-sn", "-dn",          # ignore A/S/D streams to keep parsing clean
            "-f", "null", "-"
        ]

        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # idet writes to stderr on some builds
                text=True
            )
            output = result.stdout
        except Exception as e:
            return {'score': -1, 'summary': f"idet failed: {e}"}

        tff = bff = prog = und = 0

        for line in output.splitlines():
            m = re.search(r"Multi frame detection:.*TFF:(\d+).*BFF:(\d+).*Progressive:(\d+).*Undetermined:(\d+)", line)
            if m:
                tff += int(m.group(1)); bff += int(m.group(2)); prog += int(m.group(3)); und += int(m.group(4)); continue
            s = re.search(r"Single frame detection:.*TFF:(\d+).*BFF:(\d+).*Progressive:(\d+).*Undetermined:(\d+)", line)
            if s:
                tff += int(s.group(1)); bff += int(s.group(2)); prog += int(s.group(3)); und += int(s.group(4)); continue

        combed = tff + bff
        total = combed + prog + und
        rep_ts = start_time + 30.0

        if total == 0:
            return {'score': 0, 'summary': 'No frames analyzed', 'worst_frame_timestamp': rep_ts}

        ratio = combed / total
        score = min(100.0, max(0.0, ratio * 100.0))
        return {
            'score': score,
            'summary': f"Combed {combed}/{total} ({ratio*100:.1f}%)",
            'worst_frame_timestamp': rep_ts
        }
