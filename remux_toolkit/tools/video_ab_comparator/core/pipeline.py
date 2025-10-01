# remux_toolkit/tools/video_ab_comparator/core/pipeline.py

from PyQt6.QtCore import QObject, pyqtSignal
from pathlib import Path
import subprocess
import numpy as np
import traceback
from typing import Optional, List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
import threading

from .source import VideoSource
from .alignment import robust_align
from ..detectors.upscale import UpscaleDetector
from ..detectors.interlace import CombingDetector
from ..detectors.compression import BlockingDetector
from ..detectors.artifacts import BandingDetector, RingingDetector, DotCrawlDetector
from ..detectors.color import ChromaShiftDetector, RainbowingDetector, ColorCastDetector
from ..detectors.noise import DNRDetector, SharpeningDetector
from ..detectors.audio import AudioDetector
from ..detectors.telecine import GhostingDetector, CadenceDetector
from ..detectors.geometry import AspectRatioDetector

class ComparisonPipeline(QObject):
    progress = pyqtSignal(str, int)
    finished = pyqtSignal(dict)

    def __init__(self, path_a: str, path_b: str):
        super().__init__()
        self.source_a = VideoSource(Path(path_a))
        self.source_b = VideoSource(Path(path_b))
        self._lock = threading.Lock()
        self._progress_counter = 0

    def _emit(self, msg: str, pc: int):
        try:
            self.progress.emit(msg, pc)
        except Exception as e:
            print(f"Progress emit failed: {e}")

    def _extract_frames_directly(self, source: VideoSource, start_time: float,
                                 duration: float, max_frames: int = 30) -> List[np.ndarray]:
        """Extract frames directly using the iterator, which is more efficient than chunk extraction."""
        frames = []
        frame_count = 0

        try:
            print(f"    Attempting frame extraction from {start_time:.2f}s for {duration}s...")
            for frame in source.get_frame_iterator(start_time, duration):
                if frame is not None:
                    frames.append(frame)
                    frame_count += 1
                    if frame_count >= max_frames:
                        break

            # If iterator failed, try individual frame extraction as fallback
            if not frames and max_frames > 0:
                print(f"    Iterator failed, trying individual frame extraction...")
                fps = source.info.video_stream.fps if source.info and source.info.video_stream else 24.0
                frame_interval = duration / max_frames

                for i in range(max_frames):
                    ts = start_time + (i * frame_interval)
                    frame = source.get_frame(ts, accurate=False)
                    if frame is not None:
                        frames.append(frame)
                        frame_count += 1

                    if frame_count >= 5:  # At least get 5 frames before giving up
                        break

        except Exception as e:
            print(f"Frame extraction failed at {start_time}s: {e}")

        if frames:
            print(f"    Successfully extracted {len(frames)} frames")
        else:
            print(f"    WARNING: No frames extracted!")

        return frames

    def _analyze_chunk(self, chunk_idx: int, num_chunks: int, duration: float,
                       align_offset: float, align_drift: float,
                       detectors: List) -> Tuple[int, Dict]:
        """Analyze a single chunk - designed to be run in parallel."""

        # Calculate timestamps
        ts_a = duration * (chunk_idx + 0.5) / num_chunks
        ts_b = ts_a - (align_offset + align_drift * ts_a)

        if ts_b < 0:
            return chunk_idx, {}

        chunk_results = {}

        try:
            # Extract frames directly (much faster than memory chunks)
            frames_per_chunk = 20  # Reduced from 30 for speed
            chunk_duration = 2.0

            frames_a = self._extract_frames_directly(self.source_a, ts_a, chunk_duration, frames_per_chunk)
            frames_b = self._extract_frames_directly(self.source_b, ts_b, chunk_duration, frames_per_chunk)

            if not frames_a or not frames_b:
                print(f"Chunk {chunk_idx}: No frames extracted")
                return chunk_idx, {}

            # Ensure equal frame counts
            min_frames = min(len(frames_a), len(frames_b))
            frames_a = frames_a[:min_frames]
            frames_b = frames_b[:min_frames]

            # Run detectors on this chunk
            for detector in detectors:
                try:
                    # Skip audio detector for chunk analysis (run once globally)
                    if detector.issue_name == "Audio Analysis":
                        continue

                    a_res = detector.run(self.source_a, frames_a)
                    b_res = detector.run(self.source_b, frames_b)

                    # Adjust timestamps to absolute position
                    if a_res and 'worst_frame_timestamp' in a_res:
                        a_res['worst_frame_timestamp'] += ts_a
                    if b_res and 'worst_frame_timestamp' in b_res:
                        b_res['worst_frame_timestamp'] += ts_b

                    chunk_results[detector.issue_name] = {'a': a_res, 'b': b_res}

                except Exception as e:
                    print(f"Detector {detector.issue_name} failed on chunk {chunk_idx}: {str(e)[:100]}")

        except Exception as e:
            print(f"Chunk {chunk_idx} analysis failed: {str(e)[:100]}")

        return chunk_idx, chunk_results

    def _run_global_detectors(self, detectors: List) -> Dict:
        """Run detectors that need to analyze the whole file (like audio)."""
        global_results = {}

        # Audio detector needs the whole file
        try:
            audio_detector = AudioDetector()

            # For audio, we pass empty frame list since it uses ffmpeg directly
            a_res = audio_detector.run(self.source_a, [])
            b_res = audio_detector.run(self.source_b, [])

            # Only include in results if at least one source has audio or had analysis
            if a_res and b_res:
                global_results[audio_detector.issue_name] = {'a': [a_res], 'b': [b_res]}
        except Exception as e:
            print(f"Audio detector failed: {e}")
            traceback.print_exc()

        return global_results

    def run(self):
        try:
            self._emit("Probing sources…", 5)
            if not self.source_a.probe() or not self.source_b.probe():
                self.finished.emit({"error": "ffprobe failed"})
                return

            duration = min(self.source_a.info.duration, self.source_b.info.duration)

            if duration <= 0:
                self.finished.emit({"error": "Invalid video duration"})
                return

            fps_a = self.source_a.info.video_stream.fps if self.source_a.info.video_stream else 24.0
            fps_b = self.source_b.info.video_stream.fps if self.source_b.info.video_stream else 24.0

            # Alignment phase
            self._emit("Computing alignment…", 10)
            try:
                align = robust_align(
                    self.source_a, self.source_b,
                    fps_a=fps_a, fps_b=fps_b,
                    duration=duration,
                    progress_callback=lambda c, t: self._emit(f"Aligning... ({c}/{t})", 10 + int(20*c/t))
                )
            except Exception as e:
                print(f"Alignment failed: {e}")
                traceback.print_exc()
                # Continue with no alignment
                from .alignment import AlignResult
                align = AlignResult(offset_sec=0.0, drift_ppm=0.0, confidence=0.0)

            # Initialize detectors - only include working ones
            detectors = []

            # Add detectors one by one with error handling
            detector_classes = [
                UpscaleDetector, AspectRatioDetector, BlockingDetector,
                BandingDetector, RingingDetector, DotCrawlDetector,
                ChromaShiftDetector, RainbowingDetector, ColorCastDetector,
                DNRDetector, SharpeningDetector, CombingDetector,
                GhostingDetector, CadenceDetector
            ]

            for detector_class in detector_classes:
                try:
                    detectors.append(detector_class())
                except Exception as e:
                    print(f"Failed to initialize {detector_class.__name__}: {e}")

            if not detectors:
                self.finished.emit({"error": "No detectors could be initialized"})
                return

            # Adaptive chunk count based on video duration
            if duration < 120:  # Less than 2 minutes
                num_chunks = 3  # Reduced for testing
            elif duration < 600:  # Less than 10 minutes
                num_chunks = 5
            else:
                num_chunks = 8

            self._emit(f"Analyzing {num_chunks} segments...", 30)

            # Run global detectors (like audio)
            aggregated_issues = self._run_global_detectors(detectors)

            # Initialize aggregated issues for chunk-based detectors
            for det in detectors:
                if det.issue_name not in aggregated_issues:
                    aggregated_issues[det.issue_name] = {'a': [], 'b': []}

            # Sequential chunk analysis for debugging (instead of parallel)
            for i in range(num_chunks):
                try:
                    progress = 30 + int(60 * ((i + 1) / num_chunks))
                    self._emit(f"Analyzing segment {i+1}/{num_chunks}", progress)

                    chunk_idx, chunk_results = self._analyze_chunk(
                        i, num_chunks, duration,
                        align.offset_sec, align.drift_ppm, detectors
                    )

                    # Aggregate results
                    for issue_name, data in chunk_results.items():
                        if data.get('a') and data['a'].get('score', -1) > -1:
                            aggregated_issues[issue_name]['a'].append(data['a'])
                        if data.get('b') and data['b'].get('score', -1) > -1:
                            aggregated_issues[issue_name]['b'].append(data['b'])

                except Exception as e:
                    print(f"Chunk {i} failed: {e}")
                    continue

            # Finalize results
            self._emit("Finalizing report…", 95)
            final_issues = self._compile_final_issues(aggregated_issues)

            # Calculate verdict
            wins_a = sum(1 for d in final_issues.values() if d.get('winner') == 'A')
            wins_b = sum(1 for d in final_issues.values() if d.get('winner') == 'B')
            total_cats = len(final_issues)

            if total_cats == 0:
                verdict = "Analysis completed but no issues were detected."
            elif wins_a == wins_b:
                verdict = f"Sources are equivalent ({wins_a} categories each)."
            elif wins_a > wins_b:
                verdict = f"Source A is recommended ({wins_a}/{total_cats} categories)."
            else:
                verdict = f"Source B is recommended ({wins_b}/{total_cats} categories)."

            self._emit("Complete", 100)

            payload = {
                "source_a": self.source_a.info,
                "source_b": self.source_b.info,
                "alignment_offset_secs": align.offset_sec,
                "alignment_drift_ppm": align.drift_ppm,
                "verdict": verdict,
                "issues": final_issues
            }

            self.finished.emit(payload)

        except Exception as e:
            print(f"Pipeline failed: {e}")
            traceback.print_exc()
            self.finished.emit({"error": f"Pipeline failed: {str(e)[:200]}"})

    def _compile_final_issues(self, aggregated_issues: Dict) -> Dict:
        """Compile final issues from aggregated chunk results."""
        final_issues = {}

        for issue_name, data in aggregated_issues.items():
            try:
                if not data['a'] and not data['b']:
                    continue

                # Handle cases where one source failed
                if not data['a']:
                    if data['b']:
                        final_avg_b = np.mean([res['score'] for res in data['b'] if 'score' in res])
                        worst_chunk_b = max(data['b'], key=lambda x: x.get('score', 0))
                        final_issues[issue_name] = {
                            'a': {'score': -1, 'summary': 'Analysis failed', 'worst_frame_timestamp': None},
                            'b': {
                                'score': final_avg_b,
                                'summary': worst_chunk_b.get('summary', 'N/A'),
                                'worst_frame_timestamp': worst_chunk_b.get('worst_frame_timestamp')
                            },
                            'winner': "B"
                        }
                    continue

                if not data['b']:
                    if data['a']:
                        final_avg_a = np.mean([res['score'] for res in data['a'] if 'score' in res])
                        worst_chunk_a = max(data['a'], key=lambda x: x.get('score', 0))
                        final_issues[issue_name] = {
                            'a': {
                                'score': final_avg_a,
                                'summary': worst_chunk_a.get('summary', 'N/A'),
                                'worst_frame_timestamp': worst_chunk_a.get('worst_frame_timestamp')
                            },
                            'b': {'score': -1, 'summary': 'Analysis failed', 'worst_frame_timestamp': None},
                            'winner': "A"
                        }
                    continue

                # Calculate averages
                scores_a = [res['score'] for res in data['a'] if 'score' in res and res['score'] >= 0]
                scores_b = [res['score'] for res in data['b'] if 'score' in res and res['score'] >= 0]

                if not scores_a or not scores_b:
                    continue

                final_avg_a = np.mean(scores_a)
                final_avg_b = np.mean(scores_b)

                worst_chunk_a = max(data['a'], key=lambda x: x.get('score', 0))
                worst_chunk_b = max(data['b'], key=lambda x: x.get('score', 0))

                # Determine winner with significance threshold
                score_diff = abs(final_avg_a - final_avg_b)
                significance_threshold = 5.0

                if score_diff < significance_threshold:
                    winner = "Tie"
                else:
                    winner = "A" if final_avg_a <= final_avg_b else "B"

                final_issues[issue_name] = {
                    'a': {
                        'score': final_avg_a,
                        'summary': worst_chunk_a.get('summary', 'N/A'),
                        'worst_frame_timestamp': worst_chunk_a.get('worst_frame_timestamp')
                    },
                    'b': {
                        'score': final_avg_b,
                        'summary': worst_chunk_b.get('summary', 'N/A'),
                        'worst_frame_timestamp': worst_chunk_b.get('worst_frame_timestamp')
                    },
                    'winner': winner
                }

            except Exception as e:
                print(f"Failed to compile issue {issue_name}: {e}")
                continue

        return final_issues
