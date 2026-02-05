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

    # Video-verified frame sync (consecutive frame matching)
    visual_verification: bool = True  # Fine-tune with visual frame matching
    visual_num_checkpoints: int = 5  # Checkpoints at 15%, 30%, 50%, 70%, 85%
    visual_sequence_length: int = 10  # Consecutive frames to verify per checkpoint
    visual_candidate_range: int = 3  # Search ±N frames around audio correlation
    visual_comparison_method: str = "hash"  # Comparison method ('hash' or 'ssim')
    visual_hash_algorithm: str = "dhash"  # Hash method ('dhash', 'phash', 'average_hash', 'whash')
    visual_hash_size: int = 8  # Hash size (8x8 = 64 bits)
    visual_hash_threshold: int = 5  # Max hamming distance per frame
    visual_match_threshold_pct: float = 70.0  # % of sequence that must match


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


# Note: Frame extraction and verification functions have been moved to
# audio_correlation_frame_sync.py module, which provides comprehensive
# 3-checkpoint verification with sliding window matching.


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

    # Video-verified frame sync for frame-perfect accuracy
    # Uses consecutive frame matching at multiple checkpoints (no sliding window)
    if config.visual_verification and final_offset_sec != 0.0:
        if progress_callback:
            progress_callback("Video-verified frame sync...", 92)

        try:
            from .video_verified_frame_sync import video_verified_frame_sync

            # Use a temp directory for FFMS2 index caching
            import tempfile
            temp_dir = Path(tempfile.gettempdir()) / "remux_toolkit_frame_sync"
            temp_dir.mkdir(parents=True, exist_ok=True)

            # Convert audio offset to milliseconds for the frame sync
            audio_offset_ms = final_offset_sec * 1000.0

            frame_sync_result = video_verified_frame_sync(
                source_a_path,
                source_b_path,
                audio_offset_ms,
                fps_a,
                fps_b,
                num_checkpoints=config.visual_num_checkpoints,
                sequence_length=config.visual_sequence_length,
                candidate_range=config.visual_candidate_range,
                comparison_method=config.visual_comparison_method,
                hash_algorithm=config.visual_hash_algorithm,
                hash_size=config.visual_hash_size,
                hash_threshold=config.visual_hash_threshold,
                match_threshold_pct=config.visual_match_threshold_pct,
                temp_dir=temp_dir,
                progress_callback=progress_callback
            )

            if frame_sync_result.success:
                # Frame verification passed - use verified offset
                frame_corrected_offset_sec = frame_sync_result.offset_ms / 1000.0

                print(f"\n✓ Video-verified frame sync successful!")
                print(f"  Audio offset: {final_offset_sec:.6f}s ({final_offset_sec*1000:.3f}ms)")
                print(f"  Frame-verified offset: {frame_corrected_offset_sec:.6f}s ({frame_sync_result.offset_ms:.3f}ms)")
                print(f"  Correction: {frame_sync_result.offset_ms - audio_offset_ms:+.3f}ms ({frame_sync_result.offset_frames:+d} frames)")
                print(f"  Verified: {frame_sync_result.best_candidate.checkpoints_verified}/{frame_sync_result.best_candidate.checkpoints_total} checkpoints")
                print(f"  Confidence: {frame_sync_result.confidence:.1%}")

                # Use frame-verified offset
                final_offset_sec = frame_corrected_offset_sec

                # Combine confidences (weighted average: audio 40%, visual 60%)
                final_confidence = (final_confidence * 0.4) + (frame_sync_result.confidence * 0.6)

            else:
                # Frame verification failed - keep audio offset
                print(f"\n⚠ Video-verified frame sync: {frame_sync_result.method}")
                if frame_sync_result.error:
                    print(f"  Reason: {frame_sync_result.error}")
                print(f"  Keeping audio offset: {final_offset_sec:.6f}s")

        except ImportError as e:
            print(f"Video-verified frame sync unavailable: {e}")
            print(f"Keeping audio-only offset: {final_offset_sec:.6f}s")
        except Exception as e:
            print(f"Video-verified frame sync error: {e}")
            import traceback
            traceback.print_exc()
            print(f"Keeping audio-only offset: {final_offset_sec:.6f}s")

    if progress_callback:
        progress_callback("Alignment complete", 98)

    # Determine method string
    if config.visual_verification:
        method_str = "SCC+FrameSync"  # Audio correlation + frame-perfect sync
    else:
        method_str = "SCC"  # Audio-only

    return AlignResult(
        offset_sec=final_offset_sec,
        drift_ratio=0.0,  # Will be calculated from FPS if needed
        confidence=final_confidence,
        chunk_results=chunk_results,
        accepted_count=len(accepted_chunks),
        method=method_str
    )
