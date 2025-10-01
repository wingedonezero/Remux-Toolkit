# remux_toolkit/tools/video_ab_comparator/core/pipeline.py

from PyQt6.QtCore import QObject, pyqtSignal
from pathlib import Path
import subprocess
import numpy as np
import traceback
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

    def __init__(self, path_a: str, path_b: str, settings: dict):
        super().__init__()
        self.source_a = VideoSource(Path(path_a))
        self.source_b = VideoSource(Path(path_b))
        self.settings = settings
        self._lock = threading.Lock()

    def _emit(self, msg: str, pc: int):
        try:
            self.progress.emit(msg, pc)
        except Exception as e:
            print(f"Progress emit failed: {e}")

    def _analyze_chunk(self, chunk_idx: int, num_chunks: int, duration: float,
                       align_offset: float, align_drift: float,
                       detectors: List) -> Tuple[int, Dict]:
        ts_a = duration * (chunk_idx + 0.5) / num_chunks
        ts_b = ts_a - (align_offset + align_drift * ts_a)
        if ts_b < 0: return chunk_idx, {}

        chunk_duration = self.settings.get("analysis_chunk_duration", 2.0)
        chunk_results = {}
        try:
            frames_a = list(self.source_a.get_frame_iterator(ts_a, chunk_duration))
            frames_b = list(self.source_b.get_frame_iterator(ts_b, chunk_duration))

            if not frames_a or not frames_b: return chunk_idx, {}

            min_frames = min(len(frames_a), len(frames_b))
            frames_a, frames_b = frames_a[:min_frames], frames_b[:min_frames]

            for detector in detectors:
                try:
                    a_res = detector.run(self.source_a, frames_a)
                    b_res = detector.run(self.source_b, frames_b)
                    if a_res and 'worst_frame_timestamp' in a_res: a_res['worst_frame_timestamp'] += ts_a
                    if b_res and 'worst_frame_timestamp' in b_res: b_res['worst_frame_timestamp'] += ts_b
                    chunk_results[detector.issue_name] = {'a': a_res, 'b': b_res}
                except Exception as e:
                    print(f"Detector {detector.issue_name} failed on chunk {chunk_idx}: {e}")
        except Exception as e:
            print(f"Chunk {chunk_idx} analysis failed: {e}")
        return chunk_idx, chunk_results

    def run(self):
        try:
            self._emit("Probing sources…", 5)
            if not self.source_a.probe() or not self.source_b.probe():
                self.finished.emit({"error": "ffprobe failed"}); return

            duration = min(self.source_a.info.duration, self.source_b.info.duration)
            if duration <= 10:
                self.finished.emit({"error": "Video duration is too short."}); return

            global_detector_classes = []
            if self.settings.get("enable_audio_analysis", True): global_detector_classes.append(AudioDetector)
            if self.settings.get("enable_interlace_detection", True): global_detector_classes.append(CombingDetector)
            if self.settings.get("enable_cadence_detection", True): global_detector_classes.append(CadenceDetector)

            frame_detector_classes = [
                UpscaleDetector, AspectRatioDetector, BlockingDetector, BandingDetector, RingingDetector,
                DotCrawlDetector, ChromaShiftDetector, RainbowingDetector, ColorCastDetector,
                DNRDetector, SharpeningDetector, GhostingDetector
            ]

            global_detectors = [cls() for cls in global_detector_classes]
            frame_detectors = [cls() for cls in frame_detector_classes]

            self._emit("Computing alignment…", 10)
            align = robust_align(self.source_a, self.source_b, duration=duration,
                                 fps_a=self.source_a.info.video_stream.fps,
                                 fps_b=self.source_b.info.video_stream.fps,
                                 progress_callback=lambda c, t: self._emit(f"Aligning... ({c}/{t})", 10 + int(15*c/t)))

            aggregated_issues = {}
            if global_detectors:
                self._emit("Performing global analysis...", 25)
                for detector in global_detectors:
                    a_res = detector.run(self.source_a, [])
                    b_res = detector.run(self.source_b, [])
                    if a_res and b_res: aggregated_issues[detector.issue_name] = {'a': [a_res], 'b': [b_res]}

            num_chunks = self.settings.get('analysis_chunk_count', 8)
            self._emit(f"Analyzing {num_chunks} chunks in parallel...", 35)

            for det in frame_detectors:
                 aggregated_issues[det.issue_name] = {'a': [], 'b': []}

            with ThreadPoolExecutor() as executor:
                future_to_chunk = {executor.submit(self._analyze_chunk, i, num_chunks, duration, align.offset_sec, align.drift_ratio, frame_detectors): i for i in range(num_chunks)}
                for i, future in enumerate(as_completed(future_to_chunk)):
                    _, chunk_results = future.result()
                    with self._lock:
                        for issue_name, data in chunk_results.items():
                            if data.get('a') and data['a'].get('score', -1) > -1: aggregated_issues[issue_name]['a'].append(data['a'])
                            if data.get('b') and data['b'].get('score', -1) > -1: aggregated_issues[issue_name]['b'].append(data['b'])
                    self._emit(f"Analyzed chunk {i+1}/{num_chunks}", 35 + int(60 * ((i + 1) / num_chunks)))

            self._emit("Finalizing report…", 95)
            final_issues = self._compile_final_issues(aggregated_issues)
            wins_a = sum(1 for d in final_issues.values() if d.get('winner') == 'A')
            wins_b = sum(1 for d in final_issues.values() if d.get('winner') == 'B')
            verdict = f"Sources are equivalent ({wins_a} categories each)."
            if wins_a > wins_b: verdict = f"Source A is recommended ({wins_a}/{len(final_issues)} categories)."
            elif wins_b > wins_a: verdict = f"Source B is recommended ({wins_b}/{len(final_issues)} categories)."

            self._emit("Complete", 100)
            self.finished.emit({
                "source_a": self.source_a.info, "source_b": self.source_b.info,
                "alignment_offset_secs": align.offset_sec,
                "alignment_drift_ratio": align.drift_ratio,
                "verdict": verdict, "issues": final_issues
            })
        except Exception as e:
            self.finished.emit({"error": f"Pipeline failed: {e}"})

    def _compile_final_issues(self, aggregated_issues: Dict) -> Dict:
        final_issues = {}
        for issue_name, data in aggregated_issues.items():
            scores_a = [res['score'] for res in data.get('a', []) if res and 'score' in res and res['score'] >= 0]
            scores_b = [res['score'] for res in data.get('b', []) if res and 'score' in res and res['score'] >= 0]
            if not scores_a and not scores_b: continue

            avg_a = np.mean(scores_a) if scores_a else -1
            avg_b = np.mean(scores_b) if scores_b else -1

            worst_a = max(data.get('a', []), key=lambda x: x.get('score', -1)) if data.get('a') else {}
            worst_b = max(data.get('b', []), key=lambda x: x.get('score', -1)) if data.get('b') else {}

            summary_a = worst_a.get('summary', 'N/A')
            summary_b = worst_b.get('summary', 'N/A')

            if len(scores_a) > 1: summary_a = f"Avg Score: {avg_a:.1f} | Worst: ({worst_a.get('summary', 'N/A')})"
            if len(scores_b) > 1: summary_b = f"Avg Score: {avg_b:.1f} | Worst: ({worst_b.get('summary', 'N/A')})"

            winner = "Tie"
            if avg_a != -1 and avg_b != -1:
                if abs(avg_a - avg_b) >= 2.0: winner = "A" if avg_a < avg_b else "B"
            elif avg_a != -1: winner = "A"
            elif avg_b != -1: winner = "B"

            final_issues[issue_name] = {
                'a': {'score': avg_a, 'summary': summary_a, 'worst_frame_timestamp': worst_a.get('worst_frame_timestamp')},
                'b': {'score': avg_b, 'summary': summary_b, 'worst_frame_timestamp': worst_b.get('worst_frame_timestamp')},
                'winner': winner
            }
        return final_issues
