# remux_toolkit/tools/video_ab_comparator/detectors/interlace.py

import subprocess
import re
import cv2
import numpy as np
from .base_detector import BaseDetector
from ..core.source import VideoSource
from typing import List

class CombingDetector(BaseDetector):
    @property
    def issue_name(self) -> str:
        return "Interlace Combing"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        # First, use FFmpeg's idet for overall assessment
        dur = max(0.0, float(source.info.duration or 0.0))
        if dur < 2.0:
            return {'score': 0, 'summary': 'Video too short'}

        # Traditional idet analysis
        idet_score, idet_summary = self._run_idet_analysis(source, dur)

        # Frame-based combing detection for high-motion scenes
        if frame_list and len(frame_list) > 2:
            motion_combing_score = self._detect_motion_combing(frame_list)
            field_order_issues = self._check_field_order(frame_list)

            # Combine scores
            final_score = max(idet_score, motion_combing_score)

            # Build comprehensive summary
            summary_parts = [idet_summary]

            if motion_combing_score > 10:
                summary_parts.append(f"Motion combing: {motion_combing_score:.1f}%")

            if field_order_issues:
                summary_parts.append("Field order issues detected")

            # Check for hybrid content
            if abs(idet_score - motion_combing_score) > 30:
                summary_parts.append("Hybrid progressive/interlaced")

            return {
                'score': final_score,
                'summary': " | ".join(summary_parts),
                'worst_frame_timestamp': dur / 2.0
            }

        return {'score': idet_score, 'summary': idet_summary, 'worst_frame_timestamp': dur / 2.0}

    def _run_idet_analysis(self, source, dur):
        """Traditional idet filter analysis."""
        start = max(0.0, dur / 3.0)
        cmd = [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info",
            "-ss", f"{start:.3f}", "-t", "60",
            "-i", str(source.path),
            "-vf", "idet", "-an", "-f", "null", "-"
        ]

        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out = p.stdout or ""

        tff = bff = prog = und = 0
        for line in out.splitlines():
            m = re.search(r"TFF:(\d+)\s+BFF:(\d+)\s+Progressive:(\d+)\s+Undetermined:(\d+)", line, re.I)
            if m:
                tff += int(m.group(1))
                bff += int(m.group(2))
                prog += int(m.group(3))
                und += int(m.group(4))

        total = max(1, tff + bff + prog + und)
        combed = tff + bff
        score = min(100, int(100.0 * combed / total))

        if combed == 0 and prog > 0:
            summary = "Progressive"
        elif combed > prog:
            summary = f"Interlaced (TFF:{tff} BFF:{bff})"
        else:
            summary = f"Mixed (TFF:{tff} BFF:{bff} Prog:{prog})"

        return score, summary

    def _detect_motion_combing(self, frames: List[np.ndarray]) -> float:
        """Detect combing artifacts in high-motion scenes."""
        combing_scores = []

        for i in range(1, len(frames)-1):
            prev_frame = cv2.cvtColor(frames[i-1], cv2.COLOR_BGR2GRAY)
            curr_frame = cv2.cvtColor(frames[i], cv2.COLOR_BGR2GRAY)
            next_frame = cv2.cvtColor(frames[i+1], cv2.COLOR_BGR2GRAY)

            # Calculate motion
            motion = np.mean(cv2.absdiff(prev_frame, next_frame))

            if motion > 10:  # Only check high-motion frames
                # Check for horizontal line artifacts (combing)
                even_lines = curr_frame[::2, :]
                odd_lines = curr_frame[1::2, :]

                # Resize to same height for comparison
                min_height = min(even_lines.shape[0], odd_lines.shape[0])
                even_lines = even_lines[:min_height]
                odd_lines = odd_lines[:min_height]

                # Check difference between fields
                field_diff = np.mean(np.abs(even_lines.astype(float) - odd_lines.astype(float)))

                # Check vertical edges (combing creates sawtooth patterns)
                edges = cv2.Canny(curr_frame, 50, 150)
                horizontal_edges = cv2.Sobel(edges, cv2.CV_64F, 0, 1)
                sawtooth_score = np.std(horizontal_edges)

                if field_diff > 15 and sawtooth_score > 30:
                    combing_scores.append(min(100, (field_diff - 15) * 3))
                else:
                    combing_scores.append(0)
            else:
                combing_scores.append(0)

        return np.mean(combing_scores) if combing_scores else 0

    def _check_field_order(self, frames: List[np.ndarray]) -> bool:
        """Check for field order issues."""
        if len(frames) < 3:
            return False

        field_order_changes = 0
        prev_dominant = None

        for i in range(1, len(frames)-1):
            curr = cv2.cvtColor(frames[i], cv2.COLOR_BGR2GRAY)

            # Compare even and odd field motion
            even_motion = np.mean(np.abs(curr[::2, :].astype(float) -
                                        cv2.cvtColor(frames[i-1], cv2.COLOR_BGR2GRAY)[::2, :].astype(float)))
            odd_motion = np.mean(np.abs(curr[1::2, :].astype(float) -
                                       cv2.cvtColor(frames[i-1], cv2.COLOR_BGR2GRAY)[1::2, :].astype(float)))

            current_dominant = "even" if even_motion < odd_motion else "odd"

            if prev_dominant and current_dominant != prev_dominant:
                field_order_changes += 1

            prev_dominant = current_dominant

        return field_order_changes > len(frames) * 0.3
