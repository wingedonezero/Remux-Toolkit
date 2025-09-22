# remux_toolkit/tools/silence_checker/silence_checker_core.py

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import List, Optional

from PyQt6 import QtCore

FFPROBE_BIN = os.environ.get("FFPROBE_BIN", "ffprobe")
FFMPEG_BIN  = os.environ.get("FFMPEG_BIN",  "ffmpeg")

# --- Data Structures & Exceptions ---

@dataclass
class AudioStream:
    index: int
    codec_name: str
    channels: int
    sample_rate: Optional[int]
    language: Optional[str]
    title: Optional[str]

@dataclass
class SilenceResult:
    ok: bool
    leading_silence_ms: float
    details: str

class ProbeError(Exception):
    pass

# --- FFmpeg/FFprobe Logic ---

_SILENCE_START_RE = re.compile(r"silence_start:\s+([-+]?\d+(?:\.\d+)?)")
_SILENCE_END_RE   = re.compile(r"silence_end:\s+([-+]?\d+(?:\.\d+)?)\s+\|\s+silence_duration:\s+([-+]?\d+(?:\.\d+)?)")

def run_cmd(cmd: List[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )

def ffprobe_audio_streams(path: str) -> List[AudioStream]:
    if not os.path.isfile(path):
        raise ProbeError(f"File not found: {path}")
    cmd = [
        FFPROBE_BIN, "-v", "error",
        "-show_streams", "-select_streams", "a",
        "-print_format", "json",
        path,
    ]
    p = run_cmd(cmd)
    if p.returncode != 0:
        raise ProbeError(p.stderr.strip() or "ffprobe failed")
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise ProbeError(f"ffprobe JSON parse error: {e}")

    out: List[AudioStream] = []
    for st in data.get("streams", []):
        tags = st.get("tags", {}) or {}
        out.append(AudioStream(
            index=int(st.get("index")),
            codec_name=st.get("codec_name", "") or "",
            channels=int(st.get("channels", 0) or 0),
            sample_rate=int(st["sample_rate"]) if st.get("sample_rate") else None,
            language=tags.get("language"),
            title=tags.get("title"),
        ))
    return out

def scan_leading_silence(
    path: str,
    stream_index: int,
    window_ms: int,
    noise_db: int,
    min_silence_ms: int,
) -> SilenceResult:
    window_sec = max(0.05, window_ms / 1000.0)
    min_silence_sec = max(0.0, min_silence_ms / 1000.0)

    cmd = [
        FFMPEG_BIN, "-hide_banner", "-nostats", "-v", "info",
        "-t", f"{window_sec}",
        "-i", path,
        "-map", f"0:{stream_index}",
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence_sec}",
        "-f", "null", "-"
    ]
    p = run_cmd(cmd)
    stderr = p.stderr

    leading_start = None
    leading_duration = 0.0

    for line in stderr.splitlines():
        m_start = _SILENCE_START_RE.search(line)
        if m_start:
            t = float(m_start.group(1))
            if leading_start is None and abs(t - 0.0) <= 0.02:
                leading_start = t
        m_end = _SILENCE_END_RE.search(line)
        if m_end and leading_start is not None:
            end = float(m_end.group(1))
            dur = float(m_end.group(2))
            if abs((leading_start + dur) - end) < 0.05:
                leading_duration = max(leading_duration, dur)
                break

    if leading_start is not None and leading_duration == 0.0:
        leading_duration = window_sec

    return SilenceResult(ok=True, leading_silence_ms=leading_duration * 1000.0, details=stderr)

# --- Qt Background Worker ---

class Worker(QtCore.QObject):
    resultReady = QtCore.pyqtSignal(int, SilenceResult)
    error       = QtCore.pyqtSignal(str)

    @QtCore.pyqtSlot(int, str, int, int, int, int)
    def run(self, row: int, path: str, stream_index: int, window_ms: int, noise_db: int, min_sil_ms: int):
        try:
            res = scan_leading_silence(path, stream_index, window_ms, noise_db, min_sil_ms)
            self.resultReady.emit(row, res)
        except Exception as e:
            self.error.emit(str(e))
