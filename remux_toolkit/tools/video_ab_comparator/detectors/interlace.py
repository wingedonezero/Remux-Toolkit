# remux_toolkit/tools/video_ab_comparator/detectors/interlace.py

import subprocess
import re
from .base_detector import BaseDetector
from ..core.source import VideoSource

_IDET_SUM = re.compile(
    r"TFF:(\d+)\s+BFF:(\d+)\s+Progressive:(\d+)\s+Undetermined:(\d+)", re.I
)

class CombingDetector(BaseDetector):
    """Detects combing/interlace using ffmpeg's idet filter."""

    @property
    def issue_name(self) -> str:
        return "Interlace Combing"

    def run(self, source: VideoSource) -> dict:
        dur = max(0.0, float(source.info.duration or 0.0))
        if dur < 2.0:
            return {'score': 0, 'summary': 'Video too short'}

        # Look at a 60s window starting ~1/3 in (typical spot away from logos/credits)
        start = max(0.0, dur / 3.0)
        cmd = [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info",
            "-ss", f"{start:.3f}", "-t", "60",
            "-i", str(source.path),
            "-vf", "idet", "-an", "-f", "null", "-"
        ]

        # Use stdout=PIPE and stderr=STDOUT so we can parse everything from result.stdout
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out = p.stdout or ""

        tff=bff=prog=und=0
        for line in out.splitlines():
            m = _IDET_SUM.search(line)
            if m:
                tff += int(m.group(1)); bff += int(m.group(2)); prog += int(m.group(3)); und += int(m.group(4))

        total = max(1, tff + bff + prog + und)
        combed = tff + bff
        # heuristic score: more combed frames => higher score
        score = min(100, int(100.0 * combed / total))
        if combed == 0 and prog > 0:
            summary = "Progressive"
        elif combed > prog:
            summary = f"Likely Interlaced (TFF:{tff} BFF:{bff} Prog:{prog})"
        else:
            summary = f"Mixed/Undetermined (TFF:{tff} BFF:{bff} Prog:{prog})"

        # Worst-frame timestamp (approximate: center of the analysis window)
        return {'score': score, 'summary': summary, 'worst_frame_timestamp': start + 30.0}
