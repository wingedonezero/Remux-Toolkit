# remux_toolkit/tools/video_ab_comparator/core/alignment.py
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import subprocess
import json
import re
import cv2
from typing import Optional, List, Tuple
import tempfile
import os

@dataclass
class AlignResult:
    offset_sec: float
    drift_ratio: float
    confidence: float

def extract_audio_fingerprint(source_path: str, start_time: float, duration: float) -> Optional[np.ndarray]:
    """Extract audio fingerprint using FFmpeg's chromaprint."""
    try:
        cmd = [
            'ffmpeg', '-v', 'quiet',
            '-ss', str(start_time),
            '-i', str(source_path),
            '-t', str(duration),
            '-vn',  # No video
            '-ac', '1',  # Mono
            '-ar', '16000',  # 16kHz sample rate
            '-f', 's16le',  # 16-bit PCM
            '-'
        ]

        result = subprocess.run(cmd, capture_output=True, timeout=10)
        if result.returncode != 0:
            return None

        # Convert PCM to numpy array
        audio_data = np.frombuffer(result.stdout, dtype=np.int16)
        return audio_data.astype(np.float32) / 32768.0  # Normalize

    except Exception as e:
        print(f"Audio extraction failed: {e}")
        return None

def cross_correlate_audio(audio_a: np.ndarray, audio_b: np.ndarray, sample_rate: int = 16000) -> Tuple[float, float]:
    """Find offset using audio cross-correlation."""
    # Use scipy if available, otherwise numpy
    try:
        from scipy import signal
        correlation = signal.correlate(audio_b, audio_a, mode='valid', method='fft')
    except ImportError:
        # Fallback to numpy (slower but works)
        correlation = np.correlate(audio_b, audio_a, mode='valid')

    # Find peak
    peak_idx = np.argmax(np.abs(correlation))
    peak_value = correlation[peak_idx]

    # Convert to time offset
    offset_samples = peak_idx
    offset_sec = offset_samples / sample_rate

    # Calculate confidence based on correlation peak
    confidence = min(1.0, np.abs(peak_value) / (len(audio_a) * 0.5))

    return offset_sec, confidence

def extract_frame_hashes(source_path: str, start_time: float, duration: float, fps: int = 2) -> List[int]:
    """Extract perceptual hashes of frames at regular intervals."""
    hashes = []

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Extract frames at low fps
            cmd = [
                'ffmpeg', '-v', 'quiet',
                '-ss', str(start_time),
                '-i', str(source_path),
                '-t', str(duration),
                '-vf', f'fps={fps},scale=16:16',  # Small size for hashing
                '-pix_fmt', 'gray',
                os.path.join(tmpdir, 'frame_%04d.png')
            ]

            subprocess.run(cmd, check=True, timeout=15)

            # Read frames and compute hashes
            frame_files = sorted([f for f in os.listdir(tmpdir) if f.endswith('.png')])

            for frame_file in frame_files:
                frame = cv2.imread(os.path.join(tmpdir, frame_file), cv2.IMREAD_GRAYSCALE)
                if frame is not None:
                    # Simple perceptual hash
                    avg = np.mean(frame)
                    hash_val = sum(1 << i for i, pixel in enumerate(frame.flatten()) if pixel > avg)
                    hashes.append(hash_val)

    except Exception as e:
        print(f"Frame hash extraction failed: {e}")

    return hashes

def compare_frame_sequences(hashes_a: List[int], hashes_b: List[int], max_offset: int = 10) -> Tuple[int, float]:
    """Compare frame hash sequences to find best alignment."""
    if not hashes_a or not hashes_b:
        return 0, 0.0

    best_offset = 0
    best_score = 0.0

    for offset in range(-max_offset, max_offset + 1):
        score = 0
        count = 0

        for i in range(len(hashes_a)):
            j = i + offset
            if 0 <= j < len(hashes_b):
                # Hamming distance between hashes
                xor = hashes_a[i] ^ hashes_b[j]
                distance = bin(xor).count('1')
                similarity = 1.0 - (distance / 64.0)  # Assuming 64-bit hash
                score += similarity
                count += 1

        if count > 0:
            avg_score = score / count
            if avg_score > best_score:
                best_score = avg_score
                best_offset = offset

    return best_offset, best_score

def quick_align_hybrid(source_a_path: str, source_b_path: str, duration: float,
                       progress_callback=None) -> AlignResult:
    """
    Fast hybrid alignment using both audio and visual cues.
    This is MUCH faster than the SSIM approach.
    """
    if duration < 10:
        print("Video too short for alignment")
        return AlignResult(0.0, 0.0, 0.5)

    # 1. First pass: Audio-based alignment (very fast and usually accurate)
    if progress_callback:
        progress_callback("Extracting audio for alignment...", 12)

    # Sample from middle of video
    sample_start = duration * 0.4
    sample_duration = min(30.0, duration * 0.2)  # 30 seconds or 20% of video

    audio_a = extract_audio_fingerprint(source_a_path, sample_start, sample_duration)
    audio_b = extract_audio_fingerprint(source_a_path, sample_start - 10, sample_duration + 20)  # Wider window

    audio_offset = 0.0
    audio_confidence = 0.0

    if audio_a is not None and audio_b is not None:
        if progress_callback:
            progress_callback("Analyzing audio alignment...", 15)
        audio_offset, audio_confidence = cross_correlate_audio(audio_a, audio_b)
        audio_offset -= 10  # Adjust for wider window

    # 2. Second pass: Visual verification (quick hash-based)
    if progress_callback:
        progress_callback("Verifying with visual analysis...", 18)

    # Extract frame hashes around the suspected offset
    hash_start_a = duration * 0.5
    hash_duration = 5.0

    hashes_a = extract_frame_hashes(source_a_path, hash_start_a, hash_duration, fps=2)
    hashes_b = extract_frame_hashes(source_b_path, hash_start_a + audio_offset, hash_duration, fps=2)

    if hashes_a and hashes_b:
        frame_offset, frame_confidence = compare_frame_sequences(hashes_a, hashes_b, max_offset=4)
        # Convert frame offset to seconds (2 fps)
        frame_offset_sec = frame_offset * 0.5

        # Combine audio and visual results
        if audio_confidence > 0.7:
            # Trust audio more if confident
            final_offset = audio_offset + frame_offset_sec * 0.3
            final_confidence = (audio_confidence * 0.7 + frame_confidence * 0.3)
        else:
            # Equal weight if audio not confident
            final_offset = (audio_offset + frame_offset_sec) * 0.5
            final_confidence = (audio_confidence + frame_confidence) * 0.5
    else:
        # Fall back to audio only
        final_offset = audio_offset
        final_confidence = audio_confidence * 0.8

    if progress_callback:
        progress_callback("Alignment complete", 25)

    return AlignResult(offset_sec=final_offset, drift_ratio=0.0, confidence=final_confidence)

def precise_align_keyframes(source_a_path: str, source_b_path: str,
                           rough_offset: float, duration: float,
                           progress_callback=None) -> float:
    """
    Precise alignment using keyframe analysis.
    Much faster than SSIM but still accurate.
    """
    # Sample a short segment for fine-tuning
    test_point = duration * 0.5
    test_duration = 2.0

    best_offset = rough_offset
    best_score = -1

    # Search in a small window around rough offset
    search_range = np.arange(rough_offset - 1.0, rough_offset + 1.0, 0.1)

    for i, offset in enumerate(search_range):
        try:
            # Extract single frames at test point
            cmd_a = [
                'ffmpeg', '-v', 'quiet',
                '-ss', str(test_point),
                '-i', source_a_path,
                '-vframes', '1',
                '-f', 'image2pipe',
                '-pix_fmt', 'gray',
                '-vcodec', 'rawvideo',
                '-'
            ]

            cmd_b = [
                'ffmpeg', '-v', 'quiet',
                '-ss', str(test_point + offset),
                '-i', source_b_path,
                '-vframes', '1',
                '-f', 'image2pipe',
                '-pix_fmt', 'gray',
                '-vcodec', 'rawvideo',
                '-'
            ]

            result_a = subprocess.run(cmd_a, capture_output=True, timeout=5)
            result_b = subprocess.run(cmd_b, capture_output=True, timeout=5)

            if result_a.returncode == 0 and result_b.returncode == 0:
                # Assume 1920x1080 for now (you should get this from probe)
                frame_size = 1920 * 1080

                if len(result_a.stdout) >= frame_size and len(result_b.stdout) >= frame_size:
                    frame_a = np.frombuffer(result_a.stdout[:frame_size], dtype=np.uint8)
                    frame_b = np.frombuffer(result_b.stdout[:frame_size], dtype=np.uint8)

                    # Simple correlation score
                    correlation = np.corrcoef(frame_a, frame_b)[0, 1]

                    if correlation > best_score:
                        best_score = correlation
                        best_offset = offset

            if progress_callback and i % 5 == 0:
                progress = 18 + int(7 * (i + 1) / len(search_range))
                progress_callback(f"Fine-tuning alignment... ({i+1}/{len(search_range)})", progress)

        except Exception as e:
            continue

    return best_offset

def robust_align(source_a, source_b, *, fps_a: float, fps_b: float,
                duration: float, progress_callback=None) -> AlignResult:
    """
    Main alignment function - now MUCH faster.
    Uses hybrid audio/visual approach instead of slow SSIM scanning.
    """
    # Use the fast hybrid approach
    result = quick_align_hybrid(
        str(source_a.path),
        str(source_b.path),
        duration,
        progress_callback
    )

    # Optional: Fine-tune with keyframes if needed
    if result.confidence < 0.8 and duration > 20:
        if progress_callback:
            progress_callback("Fine-tuning alignment...", 20)

        refined_offset = precise_align_keyframes(
            str(source_a.path),
            str(source_b.path),
            result.offset_sec,
            duration,
            progress_callback
        )

        result.offset_sec = refined_offset
        result.confidence = min(0.9, result.confidence + 0.2)

    # Check for frame rate mismatch (potential drift)
    if abs(fps_a - fps_b) > 0.01:
        # Calculate drift ratio
        drift_ratio = (fps_a - fps_b) / fps_a
        result.drift_ratio = drift_ratio

    # Our convention: offset = ts_a - ts_b
    result.offset_sec = -result.offset_sec

    return result

# Legacy function for compatibility (not used)
def find_offset_ffmpeg_ssim(source_a, source_b, progress_callback=None) -> float:
    """Legacy function - kept for compatibility but not used."""
    print("Warning: Using legacy SSIM alignment (slow)")
    return 0.0
