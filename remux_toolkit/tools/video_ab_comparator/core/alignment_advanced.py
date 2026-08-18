# remux_toolkit/tools/video_ab_comparator/core/alignment_advanced.py
"""
Advanced audio-based alignment using GPU-accelerated SCC correlation
with GPU pHash sliding for frame-perfect accuracy.

Based on Video-Sync-GUI methodology:
- GPU PyTorch FFT for audio cross-correlation (with parabolic peak fit)
- GPU pHash (DCT-II) sliding-window matching for frame-level alignment
  verification (replaces the earlier ISC neural matcher — same search
  geometry, no model weights, ~3x faster, sharper peaks)
"""

from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import subprocess
import json
from typing import Optional, List, Tuple, Dict
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

    # Sliding pHash frame matching (GPU DCT-II, no weights)
    use_sliding: bool = True  # Fine-tune audio offset with sliding pHash matching
    sliding_num_positions: int = 9  # Test positions across video (evenly, 10-90%)
    sliding_window_seconds: int = 10  # Duration of frame window per position
    sliding_slide_range_seconds: int = 5  # ±N seconds sliding range
    sliding_batch_size: int = 32  # GPU batch size for pHash extraction
    sliding_hash_size: int = 32  # pHash size (32 → 1024-bit descriptor)
    # Minimum consensus confidence to accept the sliding result over
    # audio ("HIGH"/"MEDIUM"/"LOW"). Below this the audio offset is
    # kept and the rejection is recorded in details.
    sliding_min_confidence: str = "MEDIUM"
    sliding_debug_report: bool = True  # Write score-landscape report to temp dir
    # Directory for ffindex cache + debug report (None = system temp)
    temp_dir: Optional[str] = None


@dataclass
class AlignResult:
    """Result of alignment analysis."""
    offset_sec: float  # Final offset in seconds
    drift_ratio: float  # FPS drift ratio
    confidence: float  # Overall confidence (0-1)
    chunk_results: List[Dict]  # Individual chunk results
    accepted_count: int  # Number of accepted chunks
    method: str = "SCC"  # Correlation method used
    offset_frames: Optional[int] = None  # Content-index frame offset from video-verified sync
    # Rich alignment metadata: content types, frame-match reason,
    # confidence label, sliding consensus / PTS / timeline details.
    # JSON-serializable (survives the subprocess round trip).
    details: Optional[Dict] = None


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
    Calculate delay using GPU-accelerated Standard Cross-Correlation (SCC).

    Uses PyTorch FFT for GPU acceleration with automatic CPU fallback.

    Args:
        ref_chunk: Reference audio chunk
        tgt_chunk: Target audio chunk
        sample_rate: Audio sample rate
        peak_fit: Enable sub-sample peak fitting for accuracy

    Returns:
        (delay_ms, match_pct): Delay in milliseconds and match percentage (0-100)
    """
    try:
        import torch
        from .gpu_backend import get_device, to_torch
        from .gpu_correlation import extract_peak, scc_confidence

        device = get_device()
        ref = to_torch(ref_chunk, device)
        tgt = to_torch(tgt_chunk, device)

        # Normalize (zero-mean, unit-variance)
        ref_n = (ref - torch.mean(ref)) / (torch.std(ref) + 1e-9)
        tgt_n = (tgt - torch.mean(tgt)) / (torch.std(tgt) + 1e-9)

        # Cross-correlation via FFT
        n = ref_n.shape[0] + tgt_n.shape[0] - 1
        n_fft = 1 << (n - 1).bit_length()

        R = torch.fft.rfft(ref_n, n=n_fft)
        T = torch.fft.rfft(tgt_n, n=n_fft)
        G = R * torch.conj(T)
        corr = torch.fft.irfft(G, n=n_fft)

        delay_ms, peak_idx = extract_peak(corr, n_fft, sample_rate, peak_fit=peak_fit)
        match_pct = scc_confidence(corr, peak_idx, ref_n, tgt_n)

        return delay_ms, match_pct

    except ImportError:
        # Fallback to CPU scipy if torch not available
        from scipy.signal import correlate

        ref_norm = (ref_chunk - np.mean(ref_chunk)) / (np.std(ref_chunk) + 1e-9)
        tgt_norm = (tgt_chunk - np.mean(tgt_chunk)) / (np.std(tgt_chunk) + 1e-9)

        corr = correlate(ref_norm, tgt_norm, mode='full', method='fft')

        peak_idx = np.argmax(np.abs(corr))
        lag_samples = float(peak_idx - (len(tgt_norm) - 1))

        if peak_fit and 0 < peak_idx < len(corr) - 1:
            y1, y2, y3 = np.abs(corr[peak_idx-1:peak_idx+2])
            delta = 0.5 * (y1 - y3) / (y1 - 2*y2 + y3 + 1e-9)
            if -1 < delta < 1:
                lag_samples += delta

        delay_ms = (lag_samples / float(sample_rate)) * 1000.0
        match_pct = (np.abs(corr[peak_idx]) / (np.sqrt(np.sum(ref_norm**2) * np.sum(tgt_norm**2)) + 1e-9)) * 100.0

        return delay_ms, match_pct


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
        return AlignResult(0.0, 0.0, 0.0, [], 0, "SCC",
                           details={"reason": "audio-streams-not-found"})

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
        return AlignResult(0.0, 0.0, 0.0, [], 0, "SCC",
                           details={"reason": "audio-decode-failed"})

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

    print(f"\nAudio alignment: delay={final_offset_sec:.6f}s ({final_offset_sec*1000:.3f}ms), "
          f"confidence={final_confidence:.2%}, accepted={len(accepted_chunks)}/{chunk_count_int}")

    # Domain conversion: SCC returns a DELAY ("how much to delay B so it
    # matches A", i.e. src_time - tgt_time). The sliding matcher and
    # every downstream consumer work in the opposite domain
    # (tgt_time - src_time: positive = B's matching content is LATER).
    # Convert here so the whole advanced path speaks one domain; without
    # this, audio-only fallbacks came back sign-flipped and every mapped
    # timestamp was off by 2x the true offset.
    final_offset_sec = -final_offset_sec

    # Track verified frame offset for frame-to-frame mapping
    verified_frame_offset = None
    details: Dict = {
        "audio_offset_sec": final_offset_sec,
        "audio_confidence": final_confidence,
    }

    # Content-type probes. Frames are always analyzed AS-IS in both
    # sources — content type only decides whether sliding frame
    # matching is attempted, and is reported so the results say
    # exactly what alignment method was used and why.
    eligible, gate_reason = True, ""
    try:
        from .content_probe import detect_video_properties, frame_match_eligibility

        if progress_callback:
            progress_callback("Probing content types...", 90)

        props_a = detect_video_properties(source_a_path)
        props_b = detect_video_properties(source_b_path)
        eligible, gate_reason = frame_match_eligibility(props_a, props_b)
        details["content_a"] = {k: v for k, v in props_a.items() if k != "mediainfo"}
        details["content_b"] = {k: v for k, v in props_b.items() if k != "mediainfo"}
    except Exception as e:
        print(f"[ContentProbe] Probe failed: {e} — attempting frame matching anyway")

    sliding_attempted = False
    sliding_accepted = False

    # Sliding pHash frame matching for frame-perfect accuracy.
    # GPU DCT-II perceptual hash, no model weights. Slides a source
    # window across the target, votes across N positions, and reports
    # both a wall-clock offset (real container timestamps of the
    # matched pair) and a content-index frame offset for FrameMapper.
    # Runs whenever enabled and eligible — including when the audio
    # offset is exactly 0.0 (audio-identical files can still be a
    # frame off in the video domain).
    if config.use_sliding and not eligible:
        print(f"\n⚠ Frame matching skipped: {gate_reason}")
        print("  Using audio-correlation alignment (frames still analyzed as-is).")
        details["frame_match_reason"] = gate_reason
    elif config.use_sliding:
        sliding_attempted = True
        if progress_callback:
            progress_callback("Sliding pHash frame matching...", 92)

        try:
            from .sliding_matcher import calculate_sliding_offset

            if config.temp_dir:
                temp_dir = Path(config.temp_dir)
            else:
                import tempfile
                temp_dir = Path(tempfile.gettempdir()) / "remux_toolkit_frame_sync"
            temp_dir.mkdir(parents=True, exist_ok=True)
            debug_dir = temp_dir if config.sliding_debug_report else None

            audio_offset_ms = final_offset_sec * 1000.0

            sliding_result = calculate_sliding_offset(
                source_a_path,
                source_b_path,
                audio_offset_ms,
                fps_a,
                fps_b,
                duration,
                num_positions=config.sliding_num_positions,
                window_seconds=config.sliding_window_seconds,
                slide_range_seconds=config.sliding_slide_range_seconds,
                batch_size=config.sliding_batch_size,
                hash_size=config.sliding_hash_size,
                temp_dir=temp_dir,
                debug_output_dir=debug_dir,
                progress_callback=progress_callback,
            )

            details["sliding"] = sliding_result

            if sliding_result["success"]:
                label = sliding_result.get("confidence_label", "LOW")
                rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
                min_label = str(config.sliding_min_confidence or "MEDIUM").upper()
                required = rank.get(min_label, 1)

                if rank.get(label, 0) >= required:
                    sliding_accepted = True
                    frame_corrected_offset_sec = sliding_result["offset_ms"] / 1000.0
                    verified_frame_offset = sliding_result["offset_frames"]

                    print(f"\n✓ Sliding pHash matching successful!")
                    print(f"  Audio offset:      {final_offset_sec:.6f}s ({final_offset_sec*1000:.3f}ms)")
                    print(f"  Wall-clock offset: {frame_corrected_offset_sec:.6f}s ({sliding_result['offset_ms']:.3f}ms)")
                    print(f"  Frame offset:      {sliding_result['offset_frames']:+d} frames (content-index)")
                    print(f"  Confidence:        {label} ({sliding_result['confidence']:.1%})")
                    print(f"  Consensus:         {sliding_result.get('consensus_count', 0)}/{sliding_result.get('num_positions', 0)} positions")
                    if sliding_result.get("pts_correction_applied"):
                        print(
                            f"  PTS delta:         {sliding_result['pts_delta_frames']:+d} frames "
                            f"({sliding_result['pts_delta_s']:+.3f}s)"
                        )
                    if sliding_result.get("timeline_correction_applied"):
                        print(
                            f"  Timeline gap corr: {sliding_result['timeline_correction_frames']:+d} frames "
                            f"(wall-clock is authoritative)"
                        )

                    final_offset_sec = frame_corrected_offset_sec
                    final_confidence = (final_confidence * 0.4) + (sliding_result["confidence"] * 0.6)
                    details["frame_match_reason"] = "sliding-matched"
                else:
                    print(f"\n⚠ Sliding result REJECTED: confidence {label} below required {min_label}")
                    print(f"  Consensus was {sliding_result.get('consensus_count', 0)}/"
                          f"{sliding_result.get('num_positions', 0)} positions, "
                          f"mean score {sliding_result.get('mean_score', 0):.4f}")
                    print(f"  Keeping audio offset: {final_offset_sec:.6f}s")
                    details["frame_match_reason"] = f"rejected-low-confidence ({label})"
            else:
                print(f"\n⚠ Sliding pHash matching: {sliding_result['method']}")
                if sliding_result.get("error"):
                    print(f"  Reason: {sliding_result['error']}")
                print(f"  Keeping audio offset: {final_offset_sec:.6f}s")
                details["frame_match_reason"] = sliding_result.get("reason", "fallback")

        except ImportError as e:
            print(f"Sliding pHash matching unavailable: {e}")
            print(f"Keeping audio-only offset: {final_offset_sec:.6f}s")
            details["frame_match_reason"] = f"fallback-import-error: {e}"
        except Exception as e:
            print(f"Sliding pHash matching error: {e}")
            import traceback
            traceback.print_exc()
            print(f"Keeping audio-only offset: {final_offset_sec:.6f}s")
            details["frame_match_reason"] = f"fallback-error: {e}"
    else:
        details["frame_match_reason"] = "sliding-disabled"

    # Clean up GPU resources after correlation
    try:
        from .gpu_backend import cleanup_gpu
        cleanup_gpu()
    except ImportError:
        pass

    if progress_callback:
        progress_callback("Alignment complete", 98)

    # Determine method string
    if not config.use_sliding:
        method_str = "SCC"  # Audio-only (frame matching disabled)
    elif sliding_accepted:
        method_str = "SCC+pHash"  # Audio correlation + GPU pHash sliding
    elif not eligible:
        method_str = "SCC (frame match skipped)"  # Content type ineligible
    elif sliding_attempted:
        method_str = "SCC+pHashFallback"  # Sliding attempted but fell back/rejected
    else:
        method_str = "SCC"

    details["method"] = method_str
    details["final_offset_sec"] = final_offset_sec

    return AlignResult(
        offset_sec=final_offset_sec,
        drift_ratio=0.0,  # Will be calculated from FPS if needed
        confidence=final_confidence,
        chunk_results=chunk_results,
        accepted_count=len(accepted_chunks),
        method=method_str,
        offset_frames=verified_frame_offset,  # Content-index frame offset for FrameMapper
        details=details,
    )
