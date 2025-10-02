# remux_toolkit/tools/video_ab_comparator/core/pipeline.py

from PyQt6.QtCore import QObject, pyqtSignal
from pathlib import Path
import subprocess
import numpy as np
import traceback
import json
import os
from typing import Optional, List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
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

    def __init__(self, path_a: str, path_b: str, settings: dict, temp_dir: str = None):
        super().__init__()
        self.source_a = VideoSource(Path(path_a))
        self.source_b = VideoSource(Path(path_b))
        self.settings = settings
        self.temp_dir = temp_dir
        self._lock = threading.Lock()
        self._stop_requested = False
        self.chunk_metadata = []  # Store metadata for each chunk

    def _emit(self, msg: str, pc: int):
        try:
            if not self._stop_requested:
                self.progress.emit(msg, pc)
        except Exception as e:
            print(f"Progress emit failed: {e}")

    def stop(self):
        """Allow pipeline to be stopped gracefully."""
        self._stop_requested = True

    def _analyze_chunk(self, chunk_idx: int, num_chunks: int, duration: float,
                       align_offset: float, align_drift: float,
                       detectors: List) -> Tuple[int, Dict]:
        if self._stop_requested:
            return chunk_idx, {}

        # Calculate timestamp for chunk in source A
        ts_a = duration * (chunk_idx + 0.5) / num_chunks

        # Map to corresponding timestamp in source B
        # offset convention: negative = B is behind, positive = B is ahead
        # To sync: ts_b = ts_a - offset - drift*ts_a
        ts_b = ts_a - (align_offset + align_drift * ts_a)

        if ts_b < 0 or ts_b >= self.source_b.info.duration:
            return chunk_idx, {}

        chunk_duration = self.settings.get("analysis_chunk_duration", 2.0)
        chunk_results = {}

        # Store chunk metadata with per-frame scores
        chunk_meta = {
            'chunk_index': chunk_idx,
            'timestamp_a': float(ts_a),
            'timestamp_b': float(ts_b),
            'duration': float(chunk_duration),
            'detector_scores': {},
            'frame_scores': []  # NEW: Store per-frame detector scores
        }

        try:
            frames_a = list(self.source_a.get_frame_iterator(ts_a, chunk_duration))
            frames_b = list(self.source_b.get_frame_iterator(ts_b, chunk_duration))

            if not frames_a or not frames_b:
                return chunk_idx, {}

            # Ensure same number of frames
            min_frames = min(len(frames_a), len(frames_b))
            frames_a, frames_b = frames_a[:min_frames], frames_b[:min_frames]

            # Initialize per-frame storage
            for frame_idx in range(min_frames):
                frame_ts_a = ts_a + (frame_idx * 0.1)  # 10fps = 0.1s per frame
                frame_ts_b = ts_b + (frame_idx * 0.1)

                chunk_meta['frame_scores'].append({
                    'frame_index': frame_idx,
                    'timestamp_a': float(frame_ts_a),
                    'timestamp_b': float(frame_ts_b),
                    'detectors': {}
                })

            # Run detectors - use BATCH mode to maintain compatibility
            for detector in detectors:
                if self._stop_requested:
                    break

                try:
                    # Always run on full frame list for aggregate scores
                    a_res = detector.run(self.source_a, frames_a)
                    b_res = detector.run(self.source_b, frames_b)

                    # For frame-based detectors, ALSO analyze each frame individually
                    if self._is_frame_based_detector(detector):
                        for frame_idx in range(min_frames):
                            # Analyze single frame
                            a_frame_res = detector.run(self.source_a, [frames_a[frame_idx]])
                            b_frame_res = detector.run(self.source_b, [frames_b[frame_idx]])

                            # Store in per-frame metadata
                            chunk_meta['frame_scores'][frame_idx]['detectors'][detector.issue_name] = {
                                'score_a': float(a_frame_res.get('score', -1)) if a_frame_res else -1,
                                'score_b': float(b_frame_res.get('score', -1)) if b_frame_res else -1,
                                'summary_a': a_frame_res.get('summary', '') if a_frame_res else '',
                                'summary_b': b_frame_res.get('summary', '') if b_frame_res else ''
                            }
                    else:
                        # For chunk-based detectors, store same score for all frames
                        for frame_idx in range(min_frames):
                            chunk_meta['frame_scores'][frame_idx]['detectors'][detector.issue_name] = {
                                'score_a': float(a_res.get('score', -1)) if a_res else -1,
                                'score_b': float(b_res.get('score', -1)) if b_res else -1,
                                'summary_a': a_res.get('summary', '') if a_res else '',
                                'summary_b': b_res.get('summary', '') if b_res else ''
                            }

                    # Add timestamps
                    if a_res and 'worst_frame_timestamp' in a_res:
                        a_res['worst_frame_timestamp'] += ts_a
                    if b_res and 'worst_frame_timestamp' in b_res:
                        b_res['worst_frame_timestamp'] += ts_b

                    chunk_results[detector.issue_name] = {'a': a_res, 'b': b_res}

                    # Store chunk-level aggregate scores
                    chunk_meta['detector_scores'][detector.issue_name] = {
                        'score_a': float(a_res.get('score', -1)) if a_res else -1,
                        'score_b': float(b_res.get('score', -1)) if b_res else -1,
                        'summary_a': a_res.get('summary', '') if a_res else '',
                        'summary_b': b_res.get('summary', '') if b_res else ''
                    }

                except Exception as e:
                    print(f"Detector {detector.issue_name} failed on chunk {chunk_idx}: {e}")
                    import traceback
                    traceback.print_exc()

        except Exception as e:
            print(f"Chunk {chunk_idx} analysis failed: {e}")
            import traceback
            traceback.print_exc()

        # Save chunk metadata
        with self._lock:
            self.chunk_metadata.append(chunk_meta)

        return chunk_idx, chunk_results

    def _is_frame_based_detector(self, detector) -> bool:
        """Check if detector should analyze frames individually for per-frame scores."""
        # These detectors can provide meaningful per-frame analysis
        frame_based = [
            'Color Banding', 'Ringing / Halos', 'Dot Crawl',
            'Chroma Shift', 'Rainbowing / Cross-Color', 'Color Cast',
            'Over-DNR / Waxiness', 'Excessive Sharpening',
            'Ghosting / Blending', 'Compression Artifacts'
        ]
        return detector.issue_name in frame_based

    def _save_chunk_metadata(self):
        """Save chunk metadata to temp directory for later viewing."""
        if not self.temp_dir:
            return

        try:
            # Sort by chunk index
            sorted_metadata = sorted(self.chunk_metadata, key=lambda x: x['chunk_index'])

            metadata_path = os.path.join(self.temp_dir, 'chunk_metadata.json')
            with open(metadata_path, 'w', encoding='utf-8') as f:
                json.dump(sorted_metadata, f, indent=2)

            print(f"Saved chunk metadata with per-frame scores to {metadata_path}")
        except Exception as e:
            print(f"Failed to save chunk metadata: {e}")

    def run(self):
        try:
            # 1. Probe sources
            self._emit("Probing sources…", 5)
            if not self.source_a.probe() or not self.source_b.probe():
                self.finished.emit({"error": "ffprobe failed"})
                return

            duration = min(self.source_a.info.duration, self.source_b.info.duration)
            if duration <= 10:
                self.finished.emit({"error": "Video duration is too short (< 10 seconds)."})
                return

            # 2. Initialize detectors
            global_detector_classes = []
            if self.settings.get("enable_audio_analysis", True):
                global_detector_classes.append(AudioDetector)
            if self.settings.get("enable_interlace_detection", True):
                global_detector_classes.append(CombingDetector)
            if self.settings.get("enable_cadence_detection", True):
                global_detector_classes.append(CadenceDetector)

            frame_detector_classes = [
                UpscaleDetector, AspectRatioDetector, BlockingDetector,
                BandingDetector, RingingDetector, DotCrawlDetector,
                ChromaShiftDetector, RainbowingDetector, ColorCastDetector,
                DNRDetector, SharpeningDetector, GhostingDetector
            ]

            global_detectors = [cls() for cls in global_detector_classes]
            frame_detectors = [cls() for cls in frame_detector_classes]

            # 3. Compute alignment (now much faster!)
            self._emit("Computing alignment (fast hybrid method)…", 10)

            try:
                align = robust_align(
                    self.source_a, self.source_b,
                    duration=duration,
                    fps_a=self.source_a.info.video_stream.fps,
                    fps_b=self.source_b.info.video_stream.fps,
                    progress_callback=lambda msg, pc: self._emit(msg, pc)
                )

                # Better alignment reporting
                if align.offset_sec < 0:
                    direction = f"B is {abs(align.offset_sec):.3f}s behind A"
                elif align.offset_sec > 0:
                    direction = f"B is {align.offset_sec:.3f}s ahead of A"
                else:
                    direction = "Perfect sync"

                self._emit(f"Alignment: {direction} (confidence: {align.confidence:.2f})", 25)

            except Exception as e:
                print(f"Alignment failed: {e}, using zero offset")
                align = type('obj', (object,), {
                    'offset_sec': 0.0,
                    'drift_ratio': 0.0,
                    'confidence': 0.0
                })()

            if self._stop_requested:
                self.finished.emit({"error": "Analysis cancelled"})
                return

            # 4. Global analysis
            aggregated_issues = {}
            if global_detectors:
                self._emit("Performing global analysis...", 30)
                for detector in global_detectors:
                    if self._stop_requested:
                        break

                    try:
                        a_res = detector.run(self.source_a, [])
                        b_res = detector.run(self.source_b, [])
                        if a_res and b_res:
                            aggregated_issues[detector.issue_name] = {'a': [a_res], 'b': [b_res]}
                    except Exception as e:
                        print(f"Global detector {detector.issue_name} failed: {e}")

            # 5. Frame-based analysis
            num_chunks = self.settings.get('analysis_chunk_count', 8)
            self._emit(f"Analyzing {num_chunks} chunks with per-frame detection...", 35)

            # Initialize issue storage
            for det in frame_detectors:
                aggregated_issues[det.issue_name] = {'a': [], 'b': []}

            # Clear chunk metadata for new run
            self.chunk_metadata = []

            # Use ThreadPoolExecutor for parallel processing
            max_workers = min(4, num_chunks)  # Limit threads to avoid overwhelming system

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_chunk = {
                    executor.submit(
                        self._analyze_chunk, i, num_chunks, duration,
                        align.offset_sec, align.drift_ratio, frame_detectors
                    ): i for i in range(num_chunks)
                }

                completed = 0
                for future in as_completed(future_to_chunk):
                    if self._stop_requested:
                        executor.shutdown(wait=False)
                        self.finished.emit({"error": "Analysis cancelled"})
                        return

                    chunk_idx, chunk_results = future.result()

                    with self._lock:
                        for issue_name, data in chunk_results.items():
                            if data.get('a') and data['a'].get('score', -1) >= 0:
                                aggregated_issues[issue_name]['a'].append(data['a'])
                            if data.get('b') and data['b'].get('score', -1) >= 0:
                                aggregated_issues[issue_name]['b'].append(data['b'])

                    completed += 1
                    progress = 35 + int(55 * (completed / num_chunks))
                    self._emit(f"Analyzed chunk {completed}/{num_chunks}", progress)

            # 6. Save chunk metadata
            self._emit("Saving per-frame metadata...", 92)
            self._save_chunk_metadata()

            # 7. Compile results
            self._emit("Finalizing report…", 95)
            final_issues = self._compile_final_issues(aggregated_issues)

            # Calculate verdict
            wins_a = sum(1 for d in final_issues.values() if d.get('winner') == 'A')
            wins_b = sum(1 for d in final_issues.values() if d.get('winner') == 'B')

            if wins_a > wins_b:
                verdict = f"✅ Source A is recommended ({wins_a}/{len(final_issues)} categories)"
            elif wins_b > wins_a:
                verdict = f"✅ Source B is recommended ({wins_b}/{len(final_issues)} categories)"
            else:
                verdict = f"⚖️ Sources are equivalent ({wins_a} categories each)"

            # Add alignment info to verdict
            if abs(align.offset_sec) > 0.02:  # More than ~half a frame at 24fps
                if align.offset_sec < 0:
                    verdict += f"\n📍 Alignment: B is {abs(align.offset_sec):.3f}s behind A"
                else:
                    verdict += f"\n📍 Alignment: B is {align.offset_sec:.3f}s ahead of A"

            self._emit("Complete!", 100)

            self.finished.emit({
                "source_a": self.source_a.info,
                "source_b": self.source_b.info,
                "alignment_offset_secs": align.offset_sec,
                "alignment_drift_ratio": align.drift_ratio,
                "alignment_confidence": align.confidence,
                "verdict": verdict,
                "issues": final_issues,
                "temp_dir": self.temp_dir
            })

        except Exception as e:
            self.finished.emit({"error": f"Pipeline failed: {e}\n{traceback.format_exc()}"})

    def _compile_final_issues(self, aggregated_issues: Dict) -> Dict:
        """Compile and summarize the aggregated issue results."""
        final_issues = {}

        for issue_name, data in aggregated_issues.items():
            scores_a = [res['score'] for res in data.get('a', [])
                       if res and 'score' in res and res['score'] >= 0]
            scores_b = [res['score'] for res in data.get('b', [])
                       if res and 'score' in res and res['score'] >= 0]

            if not scores_a and not scores_b:
                continue

            # Calculate averages
            avg_a = np.mean(scores_a) if scores_a else -1
            avg_b = np.mean(scores_b) if scores_b else -1

            # Find worst instances
            worst_a = max(data.get('a', []), key=lambda x: x.get('score', -1), default={})
            worst_b = max(data.get('b', []), key=lambda x: x.get('score', -1), default={})

            # Build summaries
            summary_a = worst_a.get('summary', 'N/A')
            summary_b = worst_b.get('summary', 'N/A')

            if len(scores_a) > 1:
                summary_a = f"Avg: {avg_a:.1f} | Worst: {worst_a.get('summary', 'N/A')}"
            if len(scores_b) > 1:
                summary_b = f"Avg: {avg_b:.1f} | Worst: {worst_b.get('summary', 'N/A')}"

            # Determine winner (lower score is better for most detectors)
            winner = "Tie"
            if avg_a >= 0 and avg_b >= 0:
                if abs(avg_a - avg_b) >= 2.0:  # Significant difference threshold
                    winner = "A" if avg_a < avg_b else "B"
            elif avg_a >= 0:
                winner = "A"
            elif avg_b >= 0:
                winner = "B"

            final_issues[issue_name] = {
                'a': {
                    'score': avg_a,
                    'summary': summary_a,
                    'worst_frame_timestamp': worst_a.get('worst_frame_timestamp')
                },
                'b': {
                    'score': avg_b,
                    'summary': summary_b,
                    'worst_frame_timestamp': worst_b.get('worst_frame_timestamp')
                },
                'winner': winner
            }

        return final_issues
