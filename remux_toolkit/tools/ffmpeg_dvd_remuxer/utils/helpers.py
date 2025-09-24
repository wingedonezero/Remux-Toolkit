# remux_toolkit/tools/ffmpeg_dvd_remuxer/utils/helpers.py
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Generator

def run_stream(cmd: list[str], stop_event=None) -> Generator[str, None, int]:
    """
    Runs a command, yielding its output line-by-line in an unbuffered way
    to handle real-time progress from tools like ffmpeg.
    """
    cmd_str = shlex.join(cmd)
    yield f">>> Executing: {cmd_str}"
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        )

        line_buffer = ''
        for char in iter(lambda: proc.stdout.read(1), ''):
            if stop_event and stop_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                yield "## PROCESS TERMINATED BY USER ##"
                return -1

            if char in ('\n', '\r'):
                if line_buffer:
                    yield line_buffer
                    line_buffer = ''
            else:
                line_buffer += char

        if line_buffer:
            yield line_buffer

        return proc.wait()
    except Exception as e:
        yield f"!! Failed to execute command: {e}"
        return -1

def get_base_name(path: Path) -> str:
    """Generates a clean base name from the input path."""
    if path.is_dir() and path.name.lower() in ("video_ts", "bmdv"):
        return path.parent.name
    return path.stem

def time_str_to_seconds(time_str: str) -> int:
    """Converts an HH:MM:SS.ss string to total seconds."""
    parts = time_str.split(':')
    seconds = int(parts[0]) * 3600 + int(parts[1]) * 60
    if '.' in parts[2]:
        seconds += int(parts[2].split('.')[0])
    else:
        seconds += int(parts[2])
    return seconds
