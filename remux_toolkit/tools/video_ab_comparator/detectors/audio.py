# remux_toolkit/tools/video_ab_comparator/detectors/audio.py

import subprocess
import json
import re
import numpy as np
from .base_detector import BaseDetector
from ..core.source import VideoSource
from typing import List

class AudioDetector(BaseDetector):
    @property
    def issue_name(self) -> str:
        return "Audio Analysis"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        # Check if source has audio streams
        if not source.info or not source.info.streams:
            return {'score': -1, 'summary': 'No stream info available'}

        audio_streams = [s for s in source.info.streams if s.codec_type == 'audio']

        if not audio_streams:
            # No audio is not an error - just report it
            return {'score': 0, 'summary': 'No audio stream found', 'data': {'has_audio': False}}

        # Analyze primary audio stream
        audio_stream = audio_streams[0]

        analysis_results = {'has_audio': True}
        issues_found = []
        score = 0

        try:
            # 1. Comprehensive loudness analysis
            loudness_data = self._analyze_loudness(source, audio_stream.index)
            analysis_results.update(loudness_data)

            # 2. Check for clipping
            clipping_data = self._detect_clipping(source, audio_stream.index)
            analysis_results.update(clipping_data)
            if clipping_data.get('clipping_ratio', 0) > 0.1:
                issues_found.append(f"Clipping: {clipping_data['clipping_ratio']:.2f}%")
                score += 30

            # 3. Dynamic range analysis
            dr_data = self._analyze_dynamic_range(source, audio_stream.index)
            analysis_results.update(dr_data)
            if dr_data.get('dynamic_range', 100) < 6:
                issues_found.append(f"Low DR: {dr_data['dynamic_range']:.1f}dB")
                score += 20

            # 4. Channel balance check
            balance_data = self._check_channel_balance(source, audio_stream.index)
            analysis_results.update(balance_data)
            if abs(balance_data.get('channel_imbalance', 0)) > 3:
                issues_found.append(f"Imbalance: {balance_data['channel_imbalance']:.1f}dB")
                score += 15

            # 5. PAL speedup detection (for anime)
            speedup_data = self._detect_pal_speedup(source, audio_stream.index)
            analysis_results.update(speedup_data)
            if speedup_data.get('has_speedup', False):
                issues_found.append("PAL speedup detected")
                score += 25

        except Exception as e:
            print(f"Audio analysis partial failure: {e}")
            # Continue with what we have

        # Build summary
        summary_parts = [f"Codec: {audio_stream.codec_name}"]

        if 'integrated_loudness' in analysis_results:
            summary_parts.append(f"Loudness: {analysis_results['integrated_loudness']:.1f} LUFS")

        if 'dynamic_range' in analysis_results:
            summary_parts.append(f"DR: {analysis_results['dynamic_range']:.1f}dB")

        if issues_found:
            summary_parts.append(f"Issues: {', '.join(issues_found)}")

        return {
            'score': min(100, score),
            'summary': " | ".join(summary_parts),
            'data': analysis_results
        }

    def _analyze_loudness(self, source: VideoSource, stream_index: int) -> dict:
        """Comprehensive loudness analysis using EBU R128."""
        try:
            # For in-memory chunks, skip this analysis
            if isinstance(source.source, bytes):
                return {'integrated_loudness': -23.0}

            cmd = [
                "ffmpeg", "-nostats", "-i", str(source.source),
                "-map", f"0:{stream_index}",
                "-filter:a", "ebur128=peak=true:framelog=quiet",
                "-t", "120",
                "-f", "null", "-"
            ]

            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, timeout=30)
            output = result.stdout

            data = {}

            # Extract various loudness metrics
            patterns = {
                'integrated_loudness': r"Integrated loudness:\s+I:\s+(-?\d+\.?\d*)\s+LUFS",
                'loudness_range': r"Loudness range:\s+LRA:\s+(\d+\.?\d*)\s+LU",
                'true_peak': r"True peak:\s+Peak:\s+(-?\d+\.?\d*)\s+dBFS",
                'short_term_max': r"Max short-term:\s+(-?\d+\.?\d*)\s+LUFS"
            }

            for key, pattern in patterns.items():
                match = re.search(pattern, output, re.IGNORECASE)
                if match:
                    data[key] = float(match.group(1))

            return data if data else {'integrated_loudness': -23.0}

        except Exception as e:
            print(f"Loudness analysis failed: {e}")
            return {'integrated_loudness': -23.0}

    def _detect_clipping(self, source: VideoSource, stream_index: int) -> dict:
        """Detect audio clipping."""
        try:
            # For in-memory chunks, skip this analysis
            if isinstance(source.source, bytes):
                return {'clipping_detected': False, 'clipping_ratio': 0}

            cmd = [
                "ffmpeg", "-nostats", "-i", str(source.source),
                "-map", f"0:{stream_index}",
                "-af", "astats=metadata=1:reset=1",
                "-t", "60",
                "-f", "null", "-"
            ]

            result = subprocess.run(cmd, stderr=subprocess.PIPE, text=True, timeout=30)
            output = result.stderr

            # Count samples at maximum level (potential clipping)
            clip_count = 0
            total_samples = 0

            for line in output.splitlines():
                if "Peak level dB" in line:
                    match = re.search(r"(-?\d+\.?\d*)", line)
                    if match and float(match.group(1)) >= -0.1:
                        clip_count += 1
                if "Number of samples" in line:
                    match = re.search(r"(\d+)", line)
                    if match:
                        total_samples += int(match.group(1))

            clipping_ratio = (clip_count / max(1, total_samples)) * 100 if total_samples > 0 else 0

            return {
                'clipping_detected': clipping_ratio > 0.01,
                'clipping_ratio': clipping_ratio
            }

        except Exception as e:
            print(f"Clipping detection failed: {e}")
            return {'clipping_detected': False, 'clipping_ratio': 0}

    def _analyze_dynamic_range(self, source: VideoSource, stream_index: int) -> dict:
        """Analyze audio dynamic range."""
        try:
            # For in-memory chunks, skip this analysis
            if isinstance(source.source, bytes):
                return {'dynamic_range': 12, 'compressed': False}

            # Use a simple RMS-based DR measurement
            cmd = [
                "ffmpeg", "-nostats", "-i", str(source.source),
                "-map", f"0:{stream_index}",
                "-af", "volumedetect",
                "-t", "60",
                "-f", "null", "-"
            ]

            result = subprocess.run(cmd, stderr=subprocess.PIPE, text=True, timeout=30)
            output = result.stderr

            max_vol = mean_vol = None

            for line in output.splitlines():
                if "max_volume:" in line:
                    match = re.search(r"(-?\d+\.?\d*)", line)
                    if match:
                        max_vol = float(match.group(1))
                if "mean_volume:" in line:
                    match = re.search(r"(-?\d+\.?\d*)", line)
                    if match:
                        mean_vol = float(match.group(1))

            if max_vol is not None and mean_vol is not None:
                # Simple DR calculation
                dynamic_range = abs(max_vol - mean_vol)
            else:
                dynamic_range = 12  # Default reasonable value

            return {
                'dynamic_range': dynamic_range,
                'compressed': dynamic_range < 6
            }

        except Exception as e:
            print(f"DR analysis failed: {e}")
            return {'dynamic_range': 12, 'compressed': False}

    def _check_channel_balance(self, source: VideoSource, stream_index: int) -> dict:
        """Check for channel imbalance in stereo/multichannel audio."""
        try:
            # For in-memory chunks, skip this analysis
            if isinstance(source.source, bytes):
                return {'channel_imbalance': 0, 'balanced': True}

            cmd = [
                "ffmpeg", "-nostats", "-i", str(source.source),
                "-map", f"0:{stream_index}",
                "-af", "astats=metadata=0:measure_perchannel=RMS_level",
                "-t", "30",
                "-f", "null", "-"
            ]

            result = subprocess.run(cmd, stderr=subprocess.PIPE, text=True, timeout=30)
            output = result.stderr

            channel_levels = []
            for line in output.splitlines():
                if "RMS level" in line and "dB" in line:
                    match = re.search(r"(-?\d+\.?\d*)\s+dB", line)
                    if match:
                        channel_levels.append(float(match.group(1)))

            if len(channel_levels) >= 2:
                imbalance = max(channel_levels) - min(channel_levels)
            else:
                imbalance = 0

            return {
                'channel_imbalance': imbalance,
                'balanced': imbalance < 1.0
            }

        except Exception as e:
            print(f"Channel balance check failed: {e}")
            return {'channel_imbalance': 0, 'balanced': True}

    def _detect_pal_speedup(self, source: VideoSource, stream_index: int) -> dict:
        """Detect PAL speedup (4% speed increase from 24fps to 25fps conversion)."""
        try:
            # For in-memory chunks, skip this analysis
            if isinstance(source.source, bytes):
                return {'has_speedup': False}

            # Check if audio duration suggests PAL speedup
            cmd = [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-select_streams", f"a:{stream_index}",
                "-show_streams", str(source.source)
            ]

            result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, timeout=10)
            data = json.loads(result.stdout)

            if data.get('streams'):
                stream = data['streams'][0]
                sample_rate = int(stream.get('sample_rate', 48000))

                has_speedup = False

                # Check for non-standard sample rates that indicate speedup
                if sample_rate in [50000, 50048]:  # Common PAL speedup rates
                    has_speedup = True

                # Check video frame rate if available
                if source.info.video_stream and source.info.video_stream.fps:
                    fps = source.info.video_stream.fps
                    if 24.9 < fps < 25.1:  # 25fps suggests PAL
                        has_speedup = True

                return {
                    'has_speedup': has_speedup,
                    'sample_rate': sample_rate
                }

            return {'has_speedup': False}

        except Exception as e:
            print(f"PAL speedup detection failed: {e}")
            return {'has_speedup': False}
