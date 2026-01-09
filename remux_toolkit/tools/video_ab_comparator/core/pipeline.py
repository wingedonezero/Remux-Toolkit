# remux_toolkit/tools/video_ab_comparator/core/pipeline.py

from PyQt6.QtCore import QObject, pyqtSignal
from pathlib import Path
import subprocess
import numpy as np
import cv2
import traceback
import json
import os
from typing import Optional, List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

from .source import VideoSource
from .alignment import robust_align
from .frame_mapper import FrameMapper
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
        self.frame_mapper = None  # VideoTimestamps-based frame mapper (optional)

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
        # Use frame mapper for precise mapping if available
        if self.frame_mapper and self.frame_mapper.is_available():
            mapping = self.frame_mapper.map_timestamp_a_to_frame_b(ts_a)
            if mapping:
                ts_b = mapping.timestamp_b
            else:
                # Fallback to formula if mapping fails
                ts_b = ts_a - (align_offset + align_drift * ts_a)
        else:
            # Standard formula (time-based)
            # offset convention: negative = B is behind, positive = B is ahead
            # To sync: ts_b = ts_a - offset - drift*ts_a
            ts_b = ts_a - (align_offset + align_drift * ts_a)

        if ts_b < 0 or ts_b >= self.source_b.info.duration:
            return chunk_idx, {}

        # IMPROVED: Reduced chunk duration from 2.0s to 1.0s for more granular sampling
        # With 60 chunks @ 1.0s each = 60 seconds sampled (vs previous 8 chunks @ 2.0s = 16s)
        chunk_duration = self.settings.get("analysis_chunk_duration", 1.0)
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
            # Use exact timestamps from VideoTimestamps when available
            if self.frame_mapper and self.frame_mapper.is_available():
                # Get exact frame timestamps for this chunk
                timestamps_a = self.frame_mapper.get_exact_frame_timestamps(
                    self.frame_mapper.vts_a, ts_a, chunk_duration, target_fps=10.0
                )
                timestamps_b = self.frame_mapper.get_exact_frame_timestamps(
                    self.frame_mapper.vts_b, ts_b, chunk_duration, target_fps=10.0
                )

                # Extract frames using exact timestamps
                if timestamps_a and timestamps_b:
                    frames_a = self.source_a.get_frames_at_exact_timestamps(timestamps_a)
                    frames_b = self.source_b.get_frames_at_exact_timestamps(timestamps_b)
                else:
                    # Fallback to regular extraction
                    frames_a = list(self.source_a.get_frame_iterator(ts_a, chunk_duration))
                    frames_b = list(self.source_b.get_frame_iterator(ts_b, chunk_duration))
            else:
                # Use regular frame iterator when VideoTimestamps not available
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

            # 1b. Initialize PyAV extractors for frame-accurate seeking (optional)
            use_pyav = self.settings.get("use_pyav_seeking", True)
            if use_pyav:
                self._emit("Initializing frame-accurate seeking...", 7)
                self.source_a.initialize_pyav()
                self.source_b.initialize_pyav()

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
            # Check if advanced alignment is requested
            use_advanced = self.settings.get("use_advanced_alignment", False)

            if use_advanced:
                self._emit("Computing alignment (advanced SCC method)…", 10)
            else:
                self._emit("Computing alignment (fast hybrid method)…", 10)

            try:
                # Prepare alignment configuration
                align_config = {
                    'chunk_count': self.settings.get('align_chunk_count', 30),
                    'chunk_duration': self.settings.get('align_chunk_duration', 30.0),
                    'scan_start_pct': self.settings.get('align_scan_start_pct', 5.0),
                    'scan_end_pct': self.settings.get('align_scan_end_pct', 95.0),
                    'min_match_pct': self.settings.get('align_min_match_pct', 20.0),
                    'target_confidence_pct': self.settings.get('align_target_confidence_pct', 70.0),
                    'sample_rate': self.settings.get('align_sample_rate', 48000),
                    'use_soxr': self.settings.get('align_use_soxr', True),
                    'peak_fit': self.settings.get('align_peak_fit', True),
                    'delay_selection': self.settings.get('align_delay_selection', 'first'),
                    'audio_lang': self.settings.get('align_audio_lang', None),
                    'visual_verification': self.settings.get('align_visual_verification', True),
                    'visual_search_range_frames': self.settings.get('align_visual_search_frames', 20)
                }

                align = robust_align(
                    self.source_a, self.source_b,
                    duration=duration,
                    fps_a=self.source_a.info.video_stream.fps,
                    fps_b=self.source_b.info.video_stream.fps,
                    progress_callback=lambda msg, pc: self._emit(msg, pc),
                    use_advanced=use_advanced,
                    align_config=align_config if use_advanced else None
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

            # 3b. Initialize VideoTimestamps-based frame mapper (optional)
            use_frame_mapper = self.settings.get("use_frame_mapper", True)
            if use_frame_mapper:
                try:
                    self._emit("Loading frame timestamps for precise mapping...", 27)
                    self.frame_mapper = FrameMapper(
                        str(self.source_a.path),
                        str(self.source_b.path),
                        align.offset_sec,
                        align.drift_ratio
                    )

                    if self.frame_mapper.is_available():
                        # Generate sync quality report
                        quality = self.frame_mapper.get_sync_quality_report(sample_points=10)
                        print(f"Frame mapping quality: {quality['exact_match_rate']:.1%} exact matches, "
                              f"avg drift {quality['avg_drift_ms']:.2f}ms, max drift {quality['max_drift_ms']:.2f}ms")
                    else:
                        print("VideoTimestamps not available, using time-based seeking")
                        self.frame_mapper = None

                except Exception as e:
                    print(f"Failed to initialize frame mapper: {e}")
                    print("Falling back to time-based seeking")
                    self.frame_mapper = None

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
            # IMPROVED: Increased from 8 to 60 chunks for 3.75x better coverage
            # Each chunk: 1 second at 10fps = 10 frames
            # Total: 600 frames analyzed (vs previous 160 frames)
            num_chunks = self.settings.get('analysis_chunk_count', 60)
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

    def _is_low_information_frame(self, frame: np.ndarray, timestamp: float) -> bool:
        """
        Detect low-information frames that should be filtered from worst frame selection.

        Filters out:
        - Near-black frames (fade to black, credits on black background)
        - Very low variance frames (static/freeze frames)
        - Frames that are likely text-heavy (credits, subtitles)

        Args:
            frame: BGR frame from OpenCV
            timestamp: Timestamp in seconds (for logging)

        Returns:
            True if frame should be filtered out, False if it's content
        """
        try:
            # Convert to grayscale for analysis
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            # Check 1: Near-black frame (>80% of pixels below threshold)
            black_threshold = 20  # Out of 255
            black_pixel_ratio = np.sum(gray < black_threshold) / gray.size
            if black_pixel_ratio > 0.80:
                return True  # Mostly black

            # Check 2: Very low variance (static/freeze frame or solid color)
            variance = np.var(gray)
            if variance < 10.0:  # Very low variance
                return True  # Too uniform

            # Check 3: Text detection (simple heuristic for credits)
            # Credits typically have: high contrast edges + low overall variance
            edges = cv2.Canny(gray, 50, 150)
            edge_density = np.sum(edges > 0) / edges.size

            # High edge density in specific regions = likely text
            if edge_density > 0.05 and variance < 300:
                # Additional check: text is usually in center or bottom
                h, w = gray.shape
                bottom_third = gray[int(h*0.66):, :]
                bottom_variance = np.var(bottom_third)

                if bottom_variance > variance * 0.5:  # Activity concentrated at bottom
                    return True  # Likely credits/subtitles

            return False  # Looks like real content

        except Exception as e:
            # If analysis fails, don't filter (safer)
            return False

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

            # Find worst instances with content filtering
            worst_a = self._find_worst_content_frame(data.get('a', []), 'A')
            worst_b = self._find_worst_content_frame(data.get('b', []), 'B')

            # Build summaries
            summary_a = worst_a.get('summary', 'N/A')
            summary_b = worst_b.get('summary', 'N/A')

            if len(scores_a) > 1:
                summary_a = f"Avg: {avg_a:.1f} | Worst: {worst_a.get('summary', 'N/A')}"
            if len(scores_b) > 1:
                summary_b = f"Avg: {avg_b:.1f} | Worst: {worst_b.get('summary', 'N/A')}"

            # Determine winner (lower score is better for most detectors)
            # IMPROVED: More sensitive tie threshold for better differentiation
            winner = "Tie"
            tie_threshold = self.settings.get('tie_threshold', 0.5)  # Configurable, default 0.5

            if avg_a >= 0 and avg_b >= 0:
                diff = abs(avg_a - avg_b)
                if diff >= tie_threshold:
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

    def _find_worst_content_frame(self, results: List[Dict], source_label: str) -> Dict:
        """
        Find worst frame from results, filtering out low-information frames.

        Args:
            results: List of detector results with scores and timestamps
            source_label: 'A' or 'B' for logging

        Returns:
            Worst result dict, or empty dict if none found
        """
        if not results:
            return {}

        # Filter setting
        enable_content_filter = self.settings.get('filter_low_information_frames', True)

        if not enable_content_filter:
            # Just return worst score (old behavior)
            return max(results, key=lambda x: x.get('score', -1), default={})

        # Try to find worst frame from actual content
        content_results = []

        for result in results:
            timestamp = result.get('worst_frame_timestamp')
            if timestamp is None:
                content_results.append(result)  # Can't filter, include it
                continue

            # Extract frame at timestamp to analyze
            try:
                source = self.source_a if source_label == 'A' else self.source_b
                frame = source.get_frame(timestamp, accurate=False)  # Fast seek is fine

                if frame is not None:
                    if not self._is_low_information_frame(frame, timestamp):
                        content_results.append(result)  # Real content
                else:
                    content_results.append(result)  # Can't analyze, include it

            except Exception:
                content_results.append(result)  # Error, include it to be safe

        # If we filtered everything out, fall back to original list
        if not content_results:
            print(f"Warning: Content filter removed all frames for source {source_label}, using unfiltered")
            content_results = results

        # Return worst from content frames
        return max(content_results, key=lambda x: x.get('score', -1), default={})
