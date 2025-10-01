# remux_toolkit/tools/video_ab_comparator/detectors/telecine.py

import cv2
import numpy as np
import subprocess
import re
from .base_detector import BaseDetector
from ..core.source import VideoSource
from typing import List

class GhostingDetector(BaseDetector):
    @property
    def issue_name(self) -> str: return "Ghosting / Blending"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        v_stream = source.info.video_stream
        if not v_stream: return {'score': -1}

        scores, frame_idx = [], 0
        threshold = 1.0
        prev_frame = None

        for frame in frame_list:
            if prev_frame is not None:
                mean_diff = np.mean(cv2.absdiff(prev_frame, frame))
                score = 0
                if 5 < mean_diff < 20:
                    score = (mean_diff - 5) * 6.0
                scores.append(score)

            prev_frame = frame
            frame_idx += 1

        if not scores: return {'score': 0, 'summary': 'Not detected'}

        scores_arr = np.array(scores)
        avg_score = np.mean(scores_arr)
        peak_score = np.max(scores_arr)
        occurrences = np.sum(scores_arr > threshold)
        occurrence_rate = (occurrences / len(scores_arr)) * 100
        worst_idx = np.argmax(scores_arr) + 1
        worst_ts = worst_idx / v_stream.fps

        return {
            'score': avg_score,
            'summary': f"Avg: {avg_score:.1f} | Peak: {peak_score:.1f} | Occ: {occurrence_rate:.1f}%",
            'worst_frame_timestamp': worst_ts
        }

class CadenceDetector(BaseDetector):
    @property
    def issue_name(self) -> str: return "Cadence Irregularity"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        if not source.info or not source.info.duration:
             return {'score': -1, 'summary': 'No duration info'}

        command = ["ffmpeg", "-v", "error", "-i", str(source.path), "-vf", "pullup,idet", "-an", "-f", "null", "-"]
        try:
            result = subprocess.run(command, text=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE)
            output = result.stdout

            breaks = re.findall(r"pullup: drop score", output)
            num_breaks = len(breaks)
            breaks_per_minute = (num_breaks / source.info.duration) * 60 if source.info.duration > 0 else 0
            score = min(100, breaks_per_minute * 5)
            summary = f"{num_breaks} breaks ({breaks_per_minute:.1f}/min)"
        except Exception as e:
            score, summary = -1, f"Error: {e}"

        return {'score': score, 'summary': summary, 'worst_frame_timestamp': source.info.duration / 2.0}
