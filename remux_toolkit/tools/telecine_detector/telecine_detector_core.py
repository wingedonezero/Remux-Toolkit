# remux_toolkit/tools/telecine_detector/telecine_detector_core.py

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from .telecine_detector_config import DEFAULTS

# --- HELPER FUNCTIONS ---
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".m2ts", ".ts", ".vob", ".mpg", ".mpeg", ".avi", ".mov", ".wmv", ".m2v"}

def which(cmd: str) -> Optional[str]:
    for p in os.environ.get("PATH", "").split(os.pathsep):
        fp = os.path.join(p, cmd)
        if os.path.isfile(fp) and os.access(fp, os.X_OK):
            return fp
    return None

def collect_video_paths(paths: List[str]) -> List[str]:
    # (Implementation is the same, kept for self-containment)
    out: List[str] = []
    for p in paths:
        if os.path.isfile(p):
            if os.path.splitext(p)[1].lower() in VIDEO_EXTS:
                out.append(os.path.abspath(p))
        elif os.path.isdir(p):
            for root, _, files in os.walk(p):
                for name in files:
                    if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                        out.append(os.path.abspath(os.path.join(root, name)))
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq

@dataclass
class IdetResult:
    """Stores the parsed output from the idet filter."""
    # Multi-frame (most important)
    multi_tff: int = 0
    multi_bff: int = 0
    multi_prog: int = 0
    multi_und: int = 0
    # Single-frame
    single_tff: int = 0
    single_bff: int = 0
    single_prog: int = 0
    single_und: int = 0
    # Repeated fields
    rep_neither: int = 0
    rep_top: int = 0
    rep_bottom: int = 0

    raw_output: str = ""
    error: str = ""

    @property
    def total_frames(self) -> int:
        return self.multi_tff + self.multi_bff + self.multi_prog + self.multi_und

    def get_verdict(self, threshold_pct: int = 90) -> str:
        if self.error: return "Error"
        if self.total_frames == 0: return "No data"
        prog_percent = (self.multi_prog / self.total_frames) * 100
        interlaced_count = self.multi_tff + self.multi_bff
        if prog_percent >= threshold_pct: return "Telecined (Film)"
        if interlaced_count > self.multi_prog: return "Interlaced (Video)"
        if self.multi_prog > interlaced_count: return "Progressive"
        return "Undetermined"

    def get_summary_text(self) -> str:
        """Generates a clean, formatted summary of all stats."""
        if self.error: return f"Error: {self.error}"
        if self.total_frames == 0: return "No frame data was parsed from the FFmpeg output."

        summary = (
            "idet Analysis Summary\n"
            "=======================\n\n"
            f"Multi-Frame Detection (Primary Verdict):\n"
            f"  - Progressive: {self.multi_prog}\n"
            f"  - Top Field First (TFF): {self.multi_tff}\n"
            f"  - Bottom Field First (BFF): {self.multi_bff}\n"
            f"  - Undetermined: {self.multi_und}\n\n"
            f"Single-Frame Detection:\n"
            f"  - Progressive: {self.single_prog}\n"
            f"  - Top Field First (TFF): {self.single_tff}\n"
            f"  - Bottom Field First (BFF): {self.single_bff}\n"
            f"  - Undetermined: {self.single_und}\n\n"
            f"Repeated Fields:\n"
            f"  - Neither: {self.rep_neither}\n"
            f"  - Top: {self.rep_top}\n"
            f"  - Bottom: {self.rep_bottom}\n\n"
            "-----------------------\nFull FFmpeg Log:\n-----------------------\n"
        )
        return summary

class Worker(QObject):
    finished = pyqtSignal(str, IdetResult)
    def __init__(self, settings: dict): super().__init__(); self.settings = settings
    @pyqtSlot(str)
    def analyze(self, file_path: str):
        result = IdetResult()
        try:
            duration = self.settings.get('scan_duration_s', DEFAULTS['scan_duration_s'])
            cmd = ["ffmpeg", "-nostdin", "-t", str(duration), "-i", file_path, "-vf", "idet", "-an", "-sn", "-dn", "-f", "null", "-"]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
            _, stderr = proc.communicate(timeout=duration + 15)
            result.raw_output = stderr

            for line in stderr.splitlines():
                if "Repeated Fields:" in line:
                    parts = line.split("Repeated Fields:")[-1]
                    m = re.search(r'Neither:\s*(\d+)\s*Top:\s*(\d+)\s*Bottom:\s*(\d+)', parts)
                    if m: result.rep_neither, result.rep_top, result.rep_bottom = map(int, m.groups())
                elif "Single frame detection:" in line:
                    parts = line.split("Single frame detection:")[-1]
                    m = re.search(r'TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)', parts)
                    if m: result.single_tff, result.single_bff, result.single_prog, result.single_und = map(int, m.groups())
                elif "Multi frame detection:" in line:
                    parts = line.split("Multi frame detection:")[-1]
                    m = re.search(r'TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)', parts)
                    if m: result.multi_tff, result.multi_bff, result.multi_prog, result.multi_und = map(int, m.groups())
        except subprocess.TimeoutExpired: result.error = "Analysis timed out."
        except Exception as e: result.error = f"An unexpected error occurred: {e}"
        self.finished.emit(file_path, result)
