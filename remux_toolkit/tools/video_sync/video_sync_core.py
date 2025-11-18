"""
Core logic for Video Sync Tool
Aligns videos using audio correlation to detect and fix timing issues
"""

import os
import subprocess
import shlex
import json
import numpy as np
from pathlib import Path
from PyQt6 import QtCore
from scipy.signal import correlate
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass


@dataclass
class SegmentMatch:
    """Represents a matched segment between reference and target"""
    reference_start_ms: int
    reference_end_ms: int
    target_start_ms: int
    target_end_ms: int
    confidence: float  # 0-1 correlation score
    offset_ms: int  # detected timing offset
    target_file_index: int  # which target file this belongs to


@dataclass
class SegmentMap:
    """Complete mapping of segments for alignment"""
    segments: List[SegmentMatch]
    total_duration_ms: int
    global_offset_ms: int


def validate_mkv_file(file_path):
    """
    Validates if a file exists and is an MKV file.
    Returns: (is_valid, error_message)
    """
    if not file_path:
        return False, "Empty file path"

    path = Path(file_path)

    if not path.exists():
        return False, f"File does not exist: {file_path}"

    if not path.is_file():
        return False, f"Not a file: {file_path}"

    if path.suffix.lower() != '.mkv':
        return False, f"Not an MKV file: {file_path}"

    return True, ""


def get_mkv_info(file_path):
    """
    Gets MKV file information using mkvmerge -J.
    Returns: (info_dict, error_message)
    """
    try:
        result = subprocess.run(
            ['mkvmerge', '-J', file_path],
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=30
        )

        if result.returncode != 0:
            return None, f"mkvmerge error: {result.stderr}"

        info = json.loads(result.stdout)
        return info, ""

    except subprocess.TimeoutExpired:
        return None, "mkvmerge timed out"
    except json.JSONDecodeError as e:
        return None, f"Failed to parse mkvmerge output: {e}"
    except FileNotFoundError:
        return None, "mkvmerge not found. Please ensure MKVToolNix is installed."
    except Exception as e:
        return None, f"Error getting MKV info: {e}"


def get_audio_stream_index(file_path, language_code=None):
    """
    Get the audio stream index for a specific language.
    Returns: (stream_index, error_message)
    """
    info, error = get_mkv_info(file_path)
    if error:
        return None, error

    audio_tracks = [t for t in info.get('tracks', []) if t.get('type') == 'audio']

    if not audio_tracks:
        return None, "No audio tracks found"

    # If language specified, find matching track
    if language_code:
        for track in audio_tracks:
            props = track.get('properties', {})
            if props.get('language') == language_code:
                return track['id'], ""

    # Return first audio track if no language match or no language specified
    return audio_tracks[0]['id'], ""


def extract_audio_pcm(file_path, stream_index, duration_sec=None, start_sec=0):
    """
    Extract audio from MKV as raw PCM data using ffmpeg.
    Returns: (numpy array of audio samples, sample_rate, error_message)
    """
    try:
        cmd = [
            'ffmpeg', '-v', 'error',
            '-i', file_path,
            '-ss', str(start_sec),
            '-map', f'0:{stream_index}',
            '-ac', '1',  # Mono
            '-ar', '48000',  # 48kHz sample rate
            '-f', 's16le',  # 16-bit PCM
            '-'
        ]

        if duration_sec:
            cmd.insert(4, '-t')
            cmd.insert(5, str(duration_sec))

        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=300
        )

        if result.returncode != 0:
            return None, 48000, f"ffmpeg error: {result.stderr.decode('utf-8', errors='ignore')}"

        # Convert bytes to numpy array
        audio_data = np.frombuffer(result.stdout, dtype=np.int16)
        audio_float = audio_data.astype(np.float32) / 32768.0  # Normalize to -1.0 to 1.0

        return audio_float, 48000, ""

    except subprocess.TimeoutExpired:
        return None, 48000, "Audio extraction timed out"
    except Exception as e:
        return None, 48000, f"Error extracting audio: {e}"


def get_video_duration(file_path):
    """
    Get video duration in seconds using ffprobe.
    Returns: (duration_seconds, error_message)
    """
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', file_path],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            return None, f"ffprobe error: {result.stderr}"

        duration = float(result.stdout.strip())
        return duration, ""

    except Exception as e:
        return None, f"Error getting duration: {e}"


def cross_correlate_audio(reference_audio, target_audio, sample_rate=48000):
    """
    Cross-correlate two audio signals to find offset and confidence.
    Returns: (offset_ms, confidence_score)
    """
    if len(reference_audio) == 0 or len(target_audio) == 0:
        return 0, 0.0

    # Normalize signals
    ref_norm = (reference_audio - np.mean(reference_audio)) / (np.std(reference_audio) + 1e-9)
    target_norm = (target_audio - np.mean(target_audio)) / (np.std(target_audio) + 1e-9)

    # Compute cross-correlation
    correlation = correlate(target_norm, ref_norm, mode='full', method='fft')

    # Find peak
    peak_index = np.argmax(np.abs(correlation))
    confidence = np.abs(correlation[peak_index]) / (np.sqrt(np.sum(ref_norm**2) * np.sum(target_norm**2)) + 1e-9)

    # Calculate offset in samples
    lag_samples = peak_index - (len(ref_norm) - 1)
    offset_ms = int((lag_samples / sample_rate) * 1000)

    return offset_ms, float(confidence)


def find_matching_segments(reference_path, target_paths, audio_language, chunk_duration_sec=5.0,
                          correlation_threshold=0.7, progress_callback=None):
    """
    Find matching segments between reference and target files using audio correlation.
    Processes target files sequentially to build a complete segment map.

    Returns: (SegmentMap, error_message)
    """
    # Get reference audio stream
    ref_stream_idx, error = get_audio_stream_index(reference_path, audio_language)
    if error:
        return None, f"Reference file: {error}"

    # Get reference duration
    ref_duration, error = get_video_duration(reference_path)
    if error:
        return None, f"Reference duration: {error}"

    ref_duration_ms = int(ref_duration * 1000)

    # Extract full reference audio
    if progress_callback:
        progress_callback("Extracting reference audio...")

    ref_audio, sample_rate, error = extract_audio_pcm(reference_path, ref_stream_idx)
    if error:
        return None, f"Reference audio extraction: {error}"

    segments = []
    current_ref_position_ms = 0  # Track where we are in the reference timeline

    # Process each target file in sequence
    for target_idx, target_path in enumerate(target_paths):
        if progress_callback:
            progress_callback(f"Processing target file {target_idx + 1}/{len(target_paths)}: {Path(target_path).name}")

        # Get target audio stream
        target_stream_idx, error = get_audio_stream_index(target_path, audio_language)
        if error:
            return None, f"Target file {target_idx + 1}: {error}"

        # Get target duration
        target_duration, error = get_video_duration(target_path)
        if error:
            return None, f"Target duration {target_idx + 1}: {error}"

        target_duration_ms = int(target_duration * 1000)

        # Extract target audio
        target_audio, _, error = extract_audio_pcm(target_path, target_stream_idx)
        if error:
            return None, f"Target audio extraction {target_idx + 1}: {error}"

        # Find where this target file's content matches in the reference
        # using a sliding window approach
        segment, error = _find_best_alignment(
            ref_audio, target_audio, sample_rate,
            current_ref_position_ms, ref_duration_ms, target_duration_ms,
            chunk_duration_sec, correlation_threshold, target_idx, progress_callback
        )

        if error:
            return None, error

        if segment:
            segments.append(segment)
            # Update reference position for next file
            current_ref_position_ms = segment.reference_end_ms
        else:
            return None, f"Could not find matching content in target file {target_idx + 1}"

    # Calculate global offset (average of all segment offsets)
    if segments:
        global_offset = int(np.mean([s.offset_ms for s in segments]))
    else:
        global_offset = 0

    segment_map = SegmentMap(
        segments=segments,
        total_duration_ms=ref_duration_ms,
        global_offset_ms=global_offset
    )

    return segment_map, ""


def _find_best_alignment(ref_audio, target_audio, sample_rate, ref_start_ms, ref_duration_ms,
                        target_duration_ms, chunk_duration_sec, threshold, target_idx, progress_callback):
    """
    Find the best alignment between reference and target audio.
    Returns: (SegmentMatch, error_message)
    """
    chunk_samples = int(chunk_duration_sec * sample_rate)
    ref_start_sample = int((ref_start_ms / 1000.0) * sample_rate)

    # Scan target audio to find matching content
    # Start by checking the beginning, middle, and end for potential intro/outro credits
    target_samples = len(target_audio)

    best_match = None
    best_confidence = 0.0

    # Try different starting points in the target (to handle intro credits)
    test_positions = [0]  # Start from beginning

    # Add more test positions if target is long enough
    if target_duration_ms > 120000:  # > 2 minutes
        test_positions.extend([
            int(0.02 * target_samples),  # 2% in
            int(0.05 * target_samples),  # 5% in
            int(0.1 * target_samples),   # 10% in
        ])

    for target_start_sample in test_positions:
        if progress_callback:
            progress_callback(f"  Testing alignment at {int((target_start_sample/sample_rate)*1000)}ms...")

        # Extract a chunk from target starting at this position
        test_chunk_end = min(target_start_sample + chunk_samples * 10, target_samples)
        target_chunk = target_audio[target_start_sample:test_chunk_end]

        if len(target_chunk) < chunk_samples:
            continue

        # Extract corresponding chunk from reference
        ref_end_sample = min(ref_start_sample + len(target_chunk), len(ref_audio))
        ref_chunk = ref_audio[ref_start_sample:ref_end_sample]

        if len(ref_chunk) < chunk_samples:
            continue

        # Correlate
        offset_ms, confidence = cross_correlate_audio(ref_chunk, target_chunk, sample_rate)

        if confidence > best_confidence and confidence >= threshold:
            best_confidence = confidence

            # Found a good match - now find the exact boundaries
            # Determine target content start (skip intro credits)
            target_content_start_ms = int((target_start_sample / sample_rate) * 1000)

            # Determine target content end (find where it stops matching reference)
            target_content_end_ms = _find_content_end(
                ref_audio, target_audio, sample_rate,
                ref_start_sample, target_start_sample,
                chunk_duration_sec, threshold
            )

            # Calculate reference mapping
            content_duration_ms = target_content_end_ms - target_content_start_ms
            ref_end_ms = min(ref_start_ms + content_duration_ms, ref_duration_ms)

            best_match = SegmentMatch(
                reference_start_ms=ref_start_ms,
                reference_end_ms=ref_end_ms,
                target_start_ms=target_content_start_ms,
                target_end_ms=target_content_end_ms,
                confidence=confidence,
                offset_ms=offset_ms,
                target_file_index=target_idx
            )

    if best_match and best_confidence >= threshold:
        return best_match, ""
    else:
        return None, f"No matching content found (best confidence: {best_confidence:.2f}, threshold: {threshold})"


def _find_content_end(ref_audio, target_audio, sample_rate, ref_start_sample,
                     target_start_sample, chunk_duration_sec, threshold):
    """
    Find where the content ends in the target (before ending credits).
    Returns: end_time_ms
    """
    chunk_samples = int(chunk_duration_sec * sample_rate)

    # Start from the end and work backwards
    target_samples = len(target_audio)
    ref_samples = len(ref_audio)

    # Check from the end backwards in steps
    step_samples = chunk_samples

    for test_pos in range(target_samples - chunk_samples, target_start_sample, -step_samples):
        target_chunk = target_audio[test_pos:test_pos + chunk_samples]

        # Find corresponding position in reference
        offset_from_start = test_pos - target_start_sample
        ref_test_pos = ref_start_sample + offset_from_start

        if ref_test_pos + chunk_samples > ref_samples:
            # We've gone past the reference end
            return int((test_pos / sample_rate) * 1000)

        ref_chunk = ref_audio[ref_test_pos:ref_test_pos + chunk_samples]

        _, confidence = cross_correlate_audio(ref_chunk, target_chunk, sample_rate)

        if confidence < threshold * 0.8:  # Use slightly lower threshold
            # Found where content stops matching - this is likely where credits start
            return int((test_pos / sample_rate) * 1000)

    # If we get here, all content matches - return target end
    return int((target_samples / sample_rate) * 1000)


def generate_alignment_commands(segment_map: SegmentMap, target_paths: List[str], output_path: str):
    """
    Generate mkvmerge commands to create the aligned video.
    Returns: List of command strings
    """
    commands = []
    temp_files = []

    # Group segments by target file
    segments_by_file = {}
    for segment in segment_map.segments:
        file_idx = segment.target_file_index
        if file_idx not in segments_by_file:
            segments_by_file[file_idx] = []
        segments_by_file[file_idx].append(segment)

    # Generate split commands for each target file
    for file_idx, segments in segments_by_file.items():
        target_path = target_paths[file_idx]

        for seg_idx, segment in enumerate(segments):
            # Create temporary file name
            temp_file = f"temp_f{file_idx}_s{seg_idx}.mkv"
            temp_files.append(temp_file)

            # Convert ms to timestamps
            start_time = _ms_to_timestamp(segment.target_start_ms)
            end_time = _ms_to_timestamp(segment.target_end_ms)

            # mkvmerge command to extract this segment
            cmd = [
                'mkvmerge',
                '-o', f'"{temp_file}"',
                '--split', f'parts:{start_time}-{end_time}',
                f'"{target_path}"'
            ]

            commands.append(' '.join(cmd))

    # Generate final merge command
    if temp_files:
        merge_cmd = ['mkvmerge', '-o', f'"{output_path}"']

        # First file
        merge_cmd.append(f'"{temp_files[0]}"')

        # Subsequent files with + operator
        for temp_file in temp_files[1:]:
            # Apply offset correction if needed
            avg_offset = segment_map.global_offset_ms
            if abs(avg_offset) > 10:  # Only apply if offset > 10ms
                merge_cmd.append(f'[ --sync 0:{-avg_offset} +"{temp_file}" ]')
            else:
                merge_cmd.append(f'+"{temp_file}"')

        commands.append(' '.join(merge_cmd))

    return commands, temp_files


def _ms_to_timestamp(ms):
    """Convert milliseconds to HH:MM:SS.mmm format"""
    total_seconds = ms / 1000.0
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = int(total_seconds % 60)
    milliseconds = int((total_seconds % 1) * 1000)

    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


class AnalysisWorker(QtCore.QThread):
    """
    Worker thread to analyze videos and find matching segments.
    """
    result = QtCore.pyqtSignal(object, str)  # (SegmentMap, error_message)
    progress = QtCore.pyqtSignal(str)  # Progress messages

    def __init__(self, reference_path, target_paths, settings):
        super().__init__()
        self.reference_path = reference_path
        self.target_paths = target_paths
        self.settings = settings
        self._stop_requested = False

    def run(self):
        try:
            segment_map, error = find_matching_segments(
                self.reference_path,
                self.target_paths,
                self.settings.get('audio_language', 'jpn'),
                self.settings.get('chunk_duration_sec', 5.0),
                self.settings.get('correlation_threshold', 0.7),
                self.progress.emit
            )

            if error:
                self.result.emit(None, error)
            else:
                self.progress.emit(f"Analysis complete! Found {len(segment_map.segments)} segments")
                self.result.emit(segment_map, "")

        except Exception as e:
            self.result.emit(None, f"Unexpected error during analysis: {e}")

    def stop(self):
        """Request the worker to stop"""
        self._stop_requested = True


class AlignmentWorker(QtCore.QThread):
    """
    Worker thread to execute the alignment commands and create output file.
    """
    line_ready = QtCore.pyqtSignal(str)  # Output line
    progress = QtCore.pyqtSignal(int)  # Progress percentage (0-100)
    finished_signal = QtCore.pyqtSignal(int, str)  # (return_code, error_message)

    def __init__(self, commands, temp_files, output_dir):
        super().__init__()
        self.commands = commands
        self.temp_files = temp_files
        self.output_dir = output_dir
        self._stop_requested = False

    def run(self):
        try:
            # Change to output directory for temp files
            original_dir = os.getcwd()
            os.chdir(self.output_dir)

            total_commands = len(self.commands)

            for idx, command in enumerate(self.commands):
                if self._stop_requested:
                    self._cleanup_temp_files()
                    os.chdir(original_dir)
                    self.finished_signal.emit(-1, "Operation cancelled by user")
                    return

                self.line_ready.emit(f"\nExecuting command {idx + 1}/{total_commands}:")
                self.line_ready.emit(f"{command}\n")

                # Execute command
                cmd_list = shlex.split(command)
                proc = subprocess.Popen(
                    cmd_list,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding='utf-8',
                    bufsize=1
                )

                # Read output
                for line in iter(proc.stdout.readline, ''):
                    if self._stop_requested:
                        proc.terminate()
                        proc.wait(timeout=5)
                        self._cleanup_temp_files()
                        os.chdir(original_dir)
                        self.finished_signal.emit(-1, "Operation cancelled by user")
                        return

                    line = line.strip()
                    if line:
                        self.line_ready.emit(line)

                        # Extract progress
                        if "Progress:" in line and "%" in line:
                            try:
                                percent_str = line.split("Progress:")[1].split("%")[0].strip()
                                percent = int(percent_str)
                                # Scale progress based on current command
                                overall_progress = int((idx / total_commands) * 100 + (percent / total_commands))
                                self.progress.emit(overall_progress)
                            except (IndexError, ValueError):
                                pass

                proc.stdout.close()
                return_code = proc.wait()

                if return_code != 0:
                    self._cleanup_temp_files()
                    os.chdir(original_dir)
                    self.finished_signal.emit(return_code, f"Command {idx + 1} failed with code {return_code}")
                    return

            # Clean up temp files
            self._cleanup_temp_files()
            os.chdir(original_dir)

            self.progress.emit(100)
            self.finished_signal.emit(0, "")

        except Exception as e:
            self.line_ready.emit(f"\nError: {e}")
            self._cleanup_temp_files()
            try:
                os.chdir(original_dir)
            except:
                pass
            self.finished_signal.emit(-1, f"Error during alignment: {e}")

    def _cleanup_temp_files(self):
        """Remove temporary files"""
        for temp_file in self.temp_files:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                    self.line_ready.emit(f"Cleaned up: {temp_file}")
            except Exception as e:
                self.line_ready.emit(f"Warning: Could not remove {temp_file}: {e}")

    def stop(self):
        """Request the worker to stop"""
        self._stop_requested = True
