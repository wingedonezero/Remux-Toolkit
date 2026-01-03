# remux_toolkit/tools/video_ab_comparator/core/alignment_advanced.py
"""
Advanced audio-based alignment using SCC correlation with configurable parameters.
Based on Video-Sync-GUI audio_corr.py methodology.
"""

from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import subprocess
import json
from typing import Optional, List, Tuple, Dict
from scipy.signal import correlate
from pathlib import Path
import imagehash
from PIL import Image
import io

# Language normalization mapping (2-letter to 3-letter ISO codes)
_LANG2TO3 = {
    'en': 'eng', 'ja': 'jpn', 'jp': 'jpn', 'zh': 'zho', 'cn': 'zho', 'es': 'spa', 'de': 'deu', 'fr': 'fra',
    'it': 'ita', 'pt': 'por', 'ru': 'rus', 'ko': 'kor', 'ar': 'ara', 'tr': 'tur', 'pl': 'pol', 'nl': 'nld',
    'sv': 'swe', 'no': 'nor', 'fi': 'fin', 'da': 'dan', 'cs': 'ces', 'sk': 'slk', 'sl': 'slv', 'hu': 'hun',
    'el': 'ell', 'he': 'heb', 'id': 'ind', 'vi': 'vie', 'th': 'tha', 'hi': 'hin', 'ur': 'urd', 'fa': 'fas',
    'uk': 'ukr', 'ro': 'ron', 'bg': 'bul', 'sr': 'srp', 'hr': 'hrv', 'ms': 'msa', 'bn': 'ben', 'ta': 'tam',
    'te': 'tel'
}

@dataclass
class AlignmentConfig:
    """Configuration for audio-based alignment."""
    # Chunk analysis settings
    chunk_count: int = 30
    chunk_duration: float = 30.0

    # Scan range (percentage of video to analyze)
    scan_start_pct: float = 5.0
    scan_end_pct: float = 95.0

    # Confidence thresholds
    min_match_pct: float = 20.0  # Minimum acceptable match percentage
    target_confidence_pct: float = 70.0  # Target confidence for good alignment

    # Audio processing
    sample_rate: int = 48000
    use_soxr: bool = True  # High-quality SOXR resampling
    peak_fit: bool = True  # Sub-sample peak fitting for accuracy

    # Delay selection strategy
    delay_selection: str = "first"  # Options: "first", "median", "mean"

    # Language selection (None = use first audio track)
    audio_lang: Optional[str] = None

    # Visual verification
    visual_verification: bool = True  # Fine-tune with visual frame matching
    visual_search_range_frames: int = 20  # Search ±N frames around audio offset


@dataclass
class AlignResult:
    """Result of alignment analysis."""
    offset_sec: float  # Final offset in seconds
    drift_ratio: float  # FPS drift ratio
    confidence: float  # Overall confidence (0-1)
    chunk_results: List[Dict]  # Individual chunk results
    accepted_count: int  # Number of accepted chunks
    method: str = "SCC"  # Correlation method used


def _normalize_lang(lang: Optional[str]) -> Optional[str]:
    """Normalize language code to 3-letter ISO format."""
    if not lang:
        return None
    s = lang.strip().lower()
    if not s or s == 'und':
        return None
    return _LANG2TO3.get(s, s) if len(s) == 2 else s


def get_audio_stream_info(file_path: str, lang: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    """
    Find audio stream by language preference.
    Returns: (stream_index, track_id) or (None, None)
    """
    try:
        cmd = ['mkvmerge', '-J', str(file_path)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

        if result.returncode != 0:
            return None, None

        info = json.loads(result.stdout)
        audio_tracks = [t for t in info.get('tracks', []) if t.get('type') == 'audio']

        if not audio_tracks:
            return None, None

        # Try to match language if specified
        if lang:
            for i, track in enumerate(audio_tracks):
                props = track.get('properties', {})
                track_lang = (props.get('language') or '').strip().lower()
                if track_lang == lang:
                    return i, track.get('id')

        # Fallback to first audio track
        return 0, audio_tracks[0].get('id')

    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        print(f"Failed to get audio stream info: {e}")
        return None, None


def decode_audio_to_memory(file_path: str, stream_index: int, sample_rate: int,
                          use_soxr: bool) -> Optional[np.ndarray]:
    """
    Decode audio stream to mono float32 NumPy array.
    Uses SOXR for high-quality resampling if enabled.
    """
    try:
        cmd = [
            'ffmpeg', '-nostdin', '-v', 'error',
            '-i', str(file_path),
            '-map', f'0:a:{stream_index}'
        ]

        if use_soxr:
            cmd.extend(['-resampler', 'soxr'])

        cmd.extend([
            '-ac', '1',  # Mono
            '-ar', str(sample_rate),  # Sample rate
            '-f', 'f32le',  # Float32 little-endian
            '-'
        ])

        result = subprocess.run(cmd, capture_output=True, timeout=60)

        if result.returncode != 0:
            print(f"FFmpeg decode failed: {result.stderr.decode()}")
            return None

        pcm_bytes = result.stdout

        # Buffer alignment (handles Opus and other codecs)
        element_size = np.dtype(np.float32).itemsize
        aligned_size = (len(pcm_bytes) // element_size) * element_size

        if aligned_size != len(pcm_bytes):
            trimmed = len(pcm_bytes) - aligned_size
            print(f"Buffer alignment: trimmed {trimmed} bytes from {Path(file_path).name}")
            pcm_bytes = pcm_bytes[:aligned_size]

        return np.frombuffer(pcm_bytes, dtype=np.float32)

    except Exception as e:
        print(f"Audio decode failed: {e}")
        return None


def find_delay_scc(ref_chunk: np.ndarray, tgt_chunk: np.ndarray,
                   sample_rate: int, peak_fit: bool = True) -> Tuple[float, float]:
    """
    Calculate delay using Standard Cross-Correlation (SCC).

    Args:
        ref_chunk: Reference audio chunk
        tgt_chunk: Target audio chunk
        sample_rate: Audio sample rate
        peak_fit: Enable sub-sample peak fitting for accuracy

    Returns:
        (delay_ms, match_pct): Delay in milliseconds and match percentage (0-100)
    """
    # Normalize chunks (zero mean, unit variance)
    ref_norm = (ref_chunk - np.mean(ref_chunk)) / (np.std(ref_chunk) + 1e-9)
    tgt_norm = (tgt_chunk - np.mean(tgt_chunk)) / (np.std(tgt_chunk) + 1e-9)

    # Compute cross-correlation using FFT method
    corr = correlate(ref_norm, tgt_norm, mode='full', method='fft')

    # Find peak
    peak_idx = np.argmax(np.abs(corr))
    lag_samples = float(peak_idx - (len(tgt_norm) - 1))

    # Sub-sample peak fitting using parabolic interpolation
    if peak_fit and 0 < peak_idx < len(corr) - 1:
        y1, y2, y3 = np.abs(corr[peak_idx-1:peak_idx+2])
        # Parabolic interpolation formula
        delta = 0.5 * (y1 - y3) / (y1 - 2*y2 + y3 + 1e-9)
        if -1 < delta < 1:
            lag_samples += delta

    # Convert to milliseconds
    delay_ms = (lag_samples / float(sample_rate)) * 1000.0

    # Calculate match percentage (normalized correlation coefficient)
    match_pct = (np.abs(corr[peak_idx]) / (np.sqrt(np.sum(ref_norm**2) * np.sum(tgt_norm**2)) + 1e-9)) * 100.0

    return delay_ms, match_pct


def extract_frame_at_time(file_path: str, timestamp: float, accurate: bool = True) -> Optional[np.ndarray]:
    """
    Extract a single frame at specific timestamp.

    Args:
        file_path: Path to video file
        timestamp: Timestamp in seconds
        accurate: Use accurate seeking (slower but frame-perfect)

    Returns:
        Frame as numpy array (RGB) or None
    """
    try:
        cmd = ['ffmpeg', '-v', 'error']

        if accurate:
            # Accurate seeking: decode from previous keyframe
            cmd.extend(['-ss', str(timestamp)])
        else:
            # Fast seeking: jump to nearest keyframe (less accurate)
            cmd.extend(['-ss', str(timestamp)])

        cmd.extend([
            '-i', str(file_path),
            '-vframes', '1',
            '-f', 'image2pipe',
            '-pix_fmt', 'rgb24',
            '-vcodec', 'rawvideo',
            '-'
        ])

        result = subprocess.run(cmd, capture_output=True, timeout=10)

        if result.returncode != 0 or not result.stdout:
            return None

        # Convert to numpy array
        # Need to know frame dimensions - try to get from a test extraction
        # For now, return raw bytes and let caller handle it
        return np.frombuffer(result.stdout, dtype=np.uint8)

    except Exception as e:
        print(f"Failed to extract frame at {timestamp}s: {e}")
        return None


def compute_frame_hash(frame_data: np.ndarray, size: int = 16) -> Optional[imagehash.ImageHash]:
    """
    Compute perceptual hash of frame.

    Args:
        frame_data: Raw frame data as numpy array
        size: Hash size (default 16x16 = 256 bits)

    Returns:
        ImageHash or None
    """
    try:
        # Try to interpret as image
        # Frame data should be RGB, but we need dimensions
        # Use a rough estimate based on common resolutions
        possible_heights = [1080, 720, 480, 2160, 576]

        for height in possible_heights:
            total_pixels = len(frame_data) // 3  # RGB = 3 bytes per pixel
            width = total_pixels // height

            if width * height * 3 == len(frame_data):
                # Found valid dimensions
                frame_rgb = frame_data.reshape((height, width, 3))
                img = Image.fromarray(frame_rgb, 'RGB')
                return imagehash.average_hash(img, hash_size=size)

        # If no valid dimensions found, just use raw comparison
        return None

    except Exception as e:
        return None


def compare_frames_correlation(frame_a: np.ndarray, frame_b: np.ndarray) -> float:
    """
    Compare two frames using normalized cross-correlation.

    Returns:
        Correlation coefficient (0-1, higher is better)
    """
    try:
        # Ensure same size
        min_size = min(len(frame_a), len(frame_b))
        a = frame_a[:min_size].astype(np.float32)
        b = frame_b[:min_size].astype(np.float32)

        # Normalize
        a_norm = (a - np.mean(a)) / (np.std(a) + 1e-9)
        b_norm = (b - np.mean(b)) / (np.std(b) + 1e-9)

        # Correlation
        corr = np.mean(a_norm * b_norm)
        return float(max(0.0, min(1.0, corr)))

    except Exception:
        return 0.0


def visual_frame_verification(source_a_path: str, source_b_path: str,
                              audio_offset_sec: float, fps_a: float, fps_b: float,
                              search_range_frames: int = 20,
                              progress_callback=None) -> Tuple[float, float]:
    """
    Fine-tune audio offset using visual frame matching.

    Extracts frames around the audio offset and finds the best visual match
    to achieve frame-perfect alignment.

    Args:
        source_a_path: Path to source A
        source_b_path: Path to source B
        audio_offset_sec: Rough offset from audio correlation
        fps_a: Frame rate of source A
        fps_b: Frame rate of source B
        search_range_frames: Search ±N frames around audio offset
        progress_callback: Optional progress callback

    Returns:
        (refined_offset_sec, confidence): Refined offset and match confidence
    """
    # Get video duration from source A
    try:
        probe_cmd = [
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'format=duration',
            '-of', 'json',
            source_a_path
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=5)
        duration_a = float(json.loads(result.stdout)['format']['duration'])
    except Exception:
        print("Failed to get duration, using 600s default")
        duration_a = 600.0

    # Test at 50% into video
    test_time_a = duration_a * 0.5
    test_time_b_center = test_time_a - audio_offset_sec

    if progress_callback:
        progress_callback("Visual frame verification...", 92)

    print(f"\nVisual verification: testing at {test_time_a:.2f}s in source A")

    # Extract reference frame from source A
    frame_a = extract_frame_at_time(source_a_path, test_time_a, accurate=True)
    if frame_a is None:
        print("Failed to extract reference frame, skipping visual verification")
        return audio_offset_sec, 0.5

    # Search for best match in source B
    # Convert frame range to time range
    frame_period_b = 1.0 / fps_b
    search_range_sec = search_range_frames * frame_period_b

    best_offset = audio_offset_sec
    best_score = 0.0
    best_frame_offset = 0

    # Search from -range to +range frames
    search_points = []
    for frame_offset in range(-search_range_frames, search_range_frames + 1):
        time_offset = frame_offset * frame_period_b
        test_time_b = test_time_b_center + time_offset
        search_points.append((frame_offset, test_time_b))

    print(f"Searching {len(search_points)} frames (±{search_range_frames} frames = ±{search_range_sec:.3f}s)")

    for i, (frame_offset, test_time_b) in enumerate(search_points):
        if test_time_b < 0:
            continue

        frame_b = extract_frame_at_time(source_b_path, test_time_b, accurate=True)
        if frame_b is None:
            continue

        # Compare frames
        score = compare_frames_correlation(frame_a, frame_b)

        if score > best_score:
            best_score = score
            best_frame_offset = frame_offset
            best_offset = audio_offset_sec - (frame_offset * frame_period_b)

        # Progress update every 5 frames
        if progress_callback and i % 5 == 0:
            pct = 92 + int(6 * (i + 1) / len(search_points))
            progress_callback(f"Verifying frames ({i+1}/{len(search_points)})...", pct)

    frame_adjustment_ms = best_frame_offset * frame_period_b * 1000
    print(f"Visual match: best at frame offset {best_frame_offset:+d} ({frame_adjustment_ms:+.1f}ms), "
          f"correlation={best_score:.3f}")
    print(f"Refined offset: {best_offset:.6f}s (was {audio_offset_sec:.6f}s)")

    return best_offset, best_score


def advanced_align(source_a_path: str, source_b_path: str,
                   config: AlignmentConfig,
                   fps_a: float = 23.976,
                   fps_b: float = 23.976,
                   progress_callback=None) -> AlignResult:
    """
    Advanced audio alignment using SCC correlation with configurable parameters.

    This implementation follows Video-Sync-GUI methodology:
    - Multi-chunk analysis across video (default 30 chunks @ 30s each)
    - Scans 5-95% of video by default
    - Uses SOXR high-quality resampling
    - Sub-sample peak fitting for accuracy
    - Configurable delay selection strategy

    Args:
        source_a_path: Path to source A video
        source_b_path: Path to source B video
        config: AlignmentConfig with analysis parameters
        progress_callback: Optional callback(message, progress_pct)

    Returns:
        AlignResult with offset, confidence, and detailed chunk results
    """
    if progress_callback:
        progress_callback("Selecting audio streams...", 5)

    # Normalize language code
    lang_norm = _normalize_lang(config.audio_lang)

    # Get audio stream info
    idx_a, track_a = get_audio_stream_info(source_a_path, lang_norm)
    idx_b, track_b = get_audio_stream_info(source_b_path, lang_norm)

    if idx_a is None or idx_b is None:
        print("Failed to locate audio streams")
        return AlignResult(0.0, 0.0, 0.0, [], 0, "SCC")

    lang_desc = f"'{lang_norm}'" if lang_norm else "'first available'"
    print(f"Selected audio streams: A (lang={lang_desc}, index={idx_a}), B (lang={lang_desc}, index={idx_b})")

    if progress_callback:
        progress_callback("Decoding audio from source A...", 10)

    # Decode audio to memory
    audio_a = decode_audio_to_memory(source_a_path, idx_a, config.sample_rate, config.use_soxr)

    if progress_callback:
        progress_callback("Decoding audio from source B...", 20)

    audio_b = decode_audio_to_memory(source_b_path, idx_b, config.sample_rate, config.use_soxr)

    if audio_a is None or audio_b is None:
        print("Failed to decode audio")
        return AlignResult(0.0, 0.0, 0.0, [], 0, "SCC")

    # Calculate video duration from audio length
    duration_a = len(audio_a) / float(config.sample_rate)
    duration_b = len(audio_b) / float(config.sample_rate)
    duration = min(duration_a, duration_b)

    print(f"Audio decoded: A={duration_a:.1f}s, B={duration_b:.1f}s")

    if progress_callback:
        progress_callback("Analyzing audio chunks...", 30)

    # Calculate scan range
    scan_start_s = duration * (config.scan_start_pct / 100.0)
    scan_end_s = duration * (config.scan_end_pct / 100.0)
    scan_range = max(0.0, (scan_end_s - scan_start_s) - config.chunk_duration)

    # Generate chunk start times
    chunk_count_int = int(config.chunk_count)  # Ensure integer for range()
    chunk_starts = [
        scan_start_s + (scan_range / max(1, chunk_count_int - 1) * i)
        for i in range(chunk_count_int)
    ]

    chunk_samples = int(round(config.chunk_duration * config.sample_rate))
    chunk_results = []
    accepted_chunks = []

    # Analyze each chunk
    for i, start_time in enumerate(chunk_starts):
        start_sample = int(round(start_time * config.sample_rate))
        end_sample = start_sample + chunk_samples

        # Skip if chunk exceeds available audio
        if end_sample > len(audio_a) or end_sample > len(audio_b):
            continue

        ref_chunk = audio_a[start_sample:end_sample]
        tgt_chunk = audio_b[start_sample:end_sample]

        # Compute delay using SCC
        delay_ms, match_pct = find_delay_scc(ref_chunk, tgt_chunk, config.sample_rate, config.peak_fit)

        accepted = match_pct >= config.min_match_pct

        result = {
            'chunk_index': i + 1,
            'start_time': start_time,
            'delay_ms': delay_ms,
            'delay_sec': delay_ms / 1000.0,
            'match_pct': match_pct,
            'accepted': accepted
        }

        chunk_results.append(result)

        if accepted:
            accepted_chunks.append(result)

        status = "ACCEPTED" if accepted else f"REJECTED (< {config.min_match_pct:.1f}%)"
        print(f"  Chunk {i+1}/{chunk_count_int} @{start_time:.1f}s: "
              f"delay={int(round(delay_ms)):+d}ms (raw={delay_ms:+.3f}ms), "
              f"match={match_pct:.2f}% — {status}")

        if progress_callback and i % 5 == 0:
            progress = 30 + int(60 * (i + 1) / chunk_count_int)
            progress_callback(f"Analyzing chunks ({i+1}/{chunk_count_int})...", progress)

    # Select final offset based on strategy
    if not accepted_chunks:
        print(f"WARNING: No chunks met minimum match threshold of {config.min_match_pct}%")
        # Use best chunk even if below threshold
        if chunk_results:
            best_chunk = max(chunk_results, key=lambda x: x['match_pct'])
            final_offset_sec = best_chunk['delay_sec']
            final_confidence = best_chunk['match_pct'] / 100.0
        else:
            final_offset_sec = 0.0
            final_confidence = 0.0
    else:
        if config.delay_selection == "first":
            # Use first accepted chunk
            selected = accepted_chunks[0]
            final_offset_sec = selected['delay_sec']
        elif config.delay_selection == "median":
            # Use median of accepted chunks
            delays = [c['delay_sec'] for c in accepted_chunks]
            final_offset_sec = float(np.median(delays))
        else:  # "mean"
            # Use mean of accepted chunks
            delays = [c['delay_sec'] for c in accepted_chunks]
            final_offset_sec = float(np.mean(delays))

        # Calculate confidence based on accepted chunks
        avg_match = np.mean([c['match_pct'] for c in accepted_chunks])
        final_confidence = min(1.0, avg_match / 100.0)

    print(f"\nAudio alignment: offset={final_offset_sec:.6f}s ({final_offset_sec*1000:.3f}ms), "
          f"confidence={final_confidence:.2%}, accepted={len(accepted_chunks)}/{chunk_count_int}")

    # Visual frame verification for frame-perfect accuracy
    if config.visual_verification and final_offset_sec != 0.0:
        if progress_callback:
            progress_callback("Visual frame verification...", 92)

        refined_offset, visual_confidence = visual_frame_verification(
            source_a_path, source_b_path,
            final_offset_sec, fps_a, fps_b,
            int(config.visual_search_range_frames),  # Ensure integer
            progress_callback
        )

        # Update offset and confidence
        if visual_confidence > 0.7:  # Trust visual if good match
            final_offset_sec = refined_offset
            final_confidence = (final_confidence + visual_confidence) / 2.0
            print(f"Final offset after visual verification: {final_offset_sec:.6f}s")
        else:
            print(f"Visual verification weak (confidence={visual_confidence:.2f}), keeping audio offset")

    if progress_callback:
        progress_callback("Alignment complete", 98)

    return AlignResult(
        offset_sec=final_offset_sec,
        drift_ratio=0.0,  # Will be calculated from FPS if needed
        confidence=final_confidence,
        chunk_results=chunk_results,
        accepted_count=len(accepted_chunks),
        method="SCC+Visual" if config.visual_verification else "SCC"
    )
