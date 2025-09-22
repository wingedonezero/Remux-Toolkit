# ===========================================
# utils/media.py - Media operations
# ===========================================

import subprocess
import json
import numpy as np
from pathlib import Path
from typing import Optional, List, Dict
import tempfile

def get_stream_info(file_path: Path) -> List[Dict]:
    """Get all stream information from media file"""
    try:
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_streams', str(file_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            info = json.loads(result.stdout)
            return info.get('streams', [])
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return []
    return []

def get_media_duration(file_path: Path) -> Optional[float]:
    """Get media file duration in seconds"""
    try:
        cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', str(file_path)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            info = json.loads(result.stdout)
            duration = float(info.get('format', {}).get('duration', 0))
            return duration if duration > 0 else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None
    return None

def extract_audio_segment(file_path: Path, stream_index: int, sample_rate: int,
                      start_time: float = 0, duration_limit: Optional[float] = None) -> Optional[np.ndarray]:
    """Extract audio segment as numpy array"""
    try:
        cmd = [
            'ffmpeg', '-nostdin', '-v', 'error',
            '-ss', str(start_time),
            '-i', str(file_path),
            # --- CORRECTION: Use absolute stream index ---
            '-map', f'0:{stream_index}',
            '-ac', '1',
            '-ar', str(sample_rate),
            '-f', 'f32le',
            '-'
        ]
        if duration_limit:
            cmd.extend(['-t', str(duration_limit)])
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode == 0 and result.stdout:
            audio = np.frombuffer(result.stdout, dtype=np.float32)
            return audio
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None

def extract_audio_to_wav(file_path: Path, stream_index: int, output_path: Path,
                         sample_rate: int = 22050) -> bool:
    """Extract audio stream to WAV file"""
    try:
        cmd = [
            'ffmpeg', '-nostdin', '-y', '-v', 'error',
            '-i', str(file_path),
            # --- CORRECTION: Use absolute stream index ---
            '-map', f'0:{stream_index}',
            '-ac', '1',
            '-ar', str(sample_rate),
            str(output_path)
        ]
        result = subprocess.run(cmd, timeout=60)
        return result.returncode == 0 and output_path.exists()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False

def extract_frames(file_path: Path, timestamps: List[float]) -> Optional[List[np.ndarray]]:
    """Extract video frames at specified timestamps"""
    frames = []
    for ts in timestamps:
        try:
            probe_cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=s=x:p=0', str(file_path)]
            probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=5)
            if probe_result.returncode != 0: continue
            dimensions = probe_result.stdout.strip().split('x')
            if len(dimensions) != 2: continue
            width, height = int(dimensions[0]), int(dimensions[1])
            cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-ss', str(ts), '-i', str(file_path), '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'rawvideo', '-pix_fmt', 'rgb24', '-']
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            if result.returncode == 0 and result.stdout:
                frame = np.frombuffer(result.stdout, dtype=np.uint8)
                frame = frame.reshape((height, width, 3))
                frames.append(frame)
        except (subprocess.TimeoutExpired, ValueError):
            continue
    return frames if frames else None
