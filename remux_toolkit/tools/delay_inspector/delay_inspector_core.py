# remux_toolkit/tools/delay_inspector/delay_inspector_core.py

import os
import sys
import json
import math
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional

from PyQt6.QtCore import QThreadPool, QRunnable, pyqtSignal, QObject

# ------------------------------ Helpers ------------------------------ #

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".m2ts", ".ts", ".vob", ".mpg", ".mpeg", ".avi", ".mov", ".wmv", ".m2v"}

def which(cmd: str) -> Optional[str]:
    for p in os.environ.get("PATH", "").split(os.pathsep):
        fp = os.path.join(p, cmd)
        if os.path.isfile(fp) and os.access(fp, os.X_OK):
            return fp
    return None

def collect_video_paths(paths: List[str]) -> List[str]:
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

def run_cmd_get_stdout(cmd: List[str]) -> str:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()

def ffprobe_video_start(path: str) -> Optional[float]:
    out = run_cmd_get_stdout([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=start_time",
        "-of", "default=nk=1:nw=1",
        path
    ])
    if not out: return None
    try:
        return float(out)
    except Exception:
        return None

def ffprobe_list_starts(path: str, select: str) -> List[Dict]:
    out = run_cmd_get_stdout([
        "ffprobe", "-v", "error",
        "-select_streams", select,
        "-show_entries", "stream=index,start_time",
        "-of", "csv=p=0",
        path
    ])
    rows: List[Dict] = []
    if not out:
        return rows
    for line in out.splitlines():
        parts = line.split(",")
        if len(parts) < 1:
            continue
        try:
            idx = int(parts[0])
        except Exception:
            continue
        start = 0.0
        if len(parts) >= 2 and parts[1] not in ("", "N/A", None):
            try:
                start = float(parts[1])
            except Exception:
                start = 0.0
        rows.append({"index": idx, "start": start})
    return rows

def ffprobe_stream_meta(path: str) -> Dict[int, Dict]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    meta: Dict[int, Dict] = {}
    if proc.returncode != 0:
        return meta
    try:
        data = json.loads(proc.stdout or "{}")
        for s in data.get("streams", []):
            idx = s.get("index")
            if idx is None:
                continue
            tags = s.get("tags") or {}
            meta[int(idx)] = {
                "codec_type": s.get("codec_type", ""),
                "codec_name": s.get("codec_name", ""),
                "language": tags.get("language", "und"),
                "title": tags.get("title", ""),
            }
    except Exception:
        pass
    return meta

def format_ms(ms: int) -> str:
    return f"{'+' if ms > 0 else ''}{ms} ms"

# ------------------------------ Data ------------------------------ #

@dataclass
class DelayEntry:
    kind: str
    index: int
    start_s: float
    delay_ms: int
    language: str
    codec: str
    title: str

@dataclass
class FileResult:
    file_path: str
    video_start_s: float
    audio: List[DelayEntry]
    subs: List[DelayEntry]

# ------------------------------ Worker ------------------------------ #

class AnalyzeSignals(QObject):
    started = pyqtSignal(str)
    finished = pyqtSignal(str, FileResult)
    failed = pyqtSignal(str, str)

class AnalyzeTask(QRunnable):
    def __init__(self, file_path: str, signals: AnalyzeSignals):
        super().__init__()
        self.file_path = file_path
        self.signals = signals

    def run(self):
        self.signals.started.emit(self.file_path)
        try:
            vstart = ffprobe_video_start(self.file_path)
            if vstart is None:
                vstart = 0.0

            audio_rows = ffprobe_list_starts(self.file_path, "a")
            sub_rows = ffprobe_list_starts(self.file_path, "s")
            meta = ffprobe_stream_meta(self.file_path)

            audio_entries: List[DelayEntry] = []
            for r in audio_rows:
                idx = int(r["index"])
                start = float(r["start"])
                delay_ms = int(round((start - vstart) * 1000))
                m = meta.get(idx, {})
                audio_entries.append(
                    DelayEntry(
                        kind="audio", index=idx, start_s=start, delay_ms=delay_ms,
                        language=m.get("language", "und"), codec=m.get("codec_name", "?"),
                        title=m.get("title", ""),
                    )
                )

            sub_entries: List[DelayEntry] = []
            for r in sub_rows:
                idx = int(r["index"])
                start = float(r["start"])
                delay_ms = int(round((start - vstart) * 1000))
                m = meta.get(idx, {})
                sub_entries.append(
                    DelayEntry(
                        kind="subtitle", index=idx, start_s=start, delay_ms=delay_ms,
                        language=m.get("language", "und"), codec=m.get("codec_name", "?"),
                        title=m.get("title", ""),
                    )
                )

            res = FileResult(
                file_path=self.file_path, video_start_s=vstart,
                audio=sorted(audio_entries, key=lambda e: e.index),
                subs=sorted(sub_entries, key=lambda e: e.index),
            )
            self.signals.finished.emit(self.file_path, res)

        except Exception as e:
            self.signals.failed.emit(self.file_path, str(e))
