# remux_toolkit/tools/video_ab_comparator/core/pipeline.py

from PyQt6.QtCore import QObject, pyqtSignal
from pathlib import Path
import subprocess
import numpy as np
from typing import Optional

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

    def _emit(self, msg: str, pc: int):
        self.progress.emit(msg, pc)

    def _extract_chunk_to_memory(self, source_path: Path, start_time: float, duration: float) -> Optional[bytes]:
        """Uses ffmpeg to quickly copy a compressed chunk into a memory buffer."""
        try:
            cmd = [
                'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                '-ss', str(start_time),
                '-i', str(source_path),
                '-t', str(duration),
                '-c', 'copy',
                '-f', 'matroska',
                '-'
            ]
            result = subprocess.run(cmd, capture_output=True, check=True)
            return result.stdout
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            self._emit(f"ERROR: Failed to extract chunk to memory: {e}", 0)
            return None

    def run(self):
        self._emit("Probing sources…", 5)
        if not self.source_a.probe() or not self.source_b.probe():
            self.finished.emit({"error": "ffprobe failed"}); return
        duration = min(self.source_a.info.duration, self.source_b.info.duration)
        fps_a = self.source_a.info.video_stream.fps if self.source_a.info.video_stream else 24.0
        fps_b = self.source_b.info.video_stream.fps if self.source_b.info.video_stream else 24.0

        self._emit("Computing alignment…", 10)
        align = robust_align(self.source_a, self.source_b, fps_a=fps_a, fps_b=fps_b, duration=duration, progress_callback=lambda c, t: self._emit(f"Aligning... ({c}/{t})", 10 + int(20*c/t)))

        detectors = [
            UpscaleDetector(), AspectRatioDetector(), BlockingDetector(), BandingDetector(),
            RingingDetector(), DotCrawlDetector(), ChromaShiftDetector(), RainbowingDetector(),
            ColorCastDetector(), DNRDetector(), SharpeningDetector(), CombingDetector(),
            GhostingDetector(), CadenceDetector(), AudioDetector()
        ]
        num_chunks = 10
        chunk_duration = 15.0
        aggregated_issues = {det.issue_name: {'a': [], 'b': []} for det in detectors}

        for i in range(num_chunks):
            progress = 30 + int(65 * (i / num_chunks))
            self._emit(f"Analyzing chunk {i+1}/{num_chunks}...", progress)

            ts_a = duration * (i + 0.5) / num_chunks
            ts_b = ts_a - (align.offset_sec + align.drift_ppm * ts_a)
            if ts_b < 0: continue

            chunk_data_a = self._extract_chunk_to_memory(self.source_a.source, ts_a, chunk_duration)
            chunk_data_b = self._extract_chunk_to_memory(self.source_b.source, ts_b, chunk_duration)

            if not chunk_data_a or not chunk_data_b: continue

            chunk_source_a = VideoSource(chunk_data_a)
            chunk_source_b = VideoSource(chunk_data_b)

            if not chunk_source_a.probe() or not chunk_source_b.probe(): continue

            frames_a = list(chunk_source_a.get_frame_iterator())
            frames_b = list(chunk_source_b.get_frame_iterator())

            for det in detectors:
                try:
                    a_res = det.run(chunk_source_a, frames_a)
                    b_res = det.run(chunk_source_b, frames_b)

                    if a_res and a_res.get('score', -1) > -1:
                        if 'worst_frame_timestamp' in a_res: a_res['worst_frame_timestamp'] += ts_a
                        aggregated_issues[det.issue_name]['a'].append(a_res)
                    if b_res and b_res.get('score', -1) > -1:
                        if 'worst_frame_timestamp' in b_res: b_res['worst_frame_timestamp'] += ts_b
                        aggregated_issues[det.issue_name]['b'].append(b_res)
                except Exception as e:
                    print(f"Detector {det.issue_name} failed on chunk {i}: {e}")

        self._emit("Finalizing report…", 98)
        final_issues = {} # <-- THIS IS THE LINE THAT HAD THE SYNTAX ERROR
        for issue_name, data in aggregated_issues.items():
            if not data['a'] and not data['b']: continue

            if not data['a']:
                final_avg_b = np.mean([res['score'] for res in data['b']])
                worst_chunk_b = max(data['b'], key=lambda x: x.get('peak_score', x.get('score', 0)))
                final_issues[issue_name] = {
                    'a': {'score': -1, 'summary': 'Analysis failed', 'worst_frame_timestamp': None},
                    'b': {'score': final_avg_b, 'summary': worst_chunk_b['summary'], 'worst_frame_timestamp': worst_chunk_b.get('worst_frame_timestamp')}, 'winner': "B" }
                continue
            if not data['b']:
                final_avg_a = np.mean([res['score'] for res in data['a']])
                worst_chunk_a = max(data['a'], key=lambda x: x.get('peak_score', x.get('score', 0)))
                final_issues[issue_name] = {
                    'b': {'score': -1, 'summary': 'Analysis failed', 'worst_frame_timestamp': None},
                    'a': {'score': final_avg_a, 'summary': worst_chunk_a['summary'], 'worst_frame_timestamp': worst_chunk_a.get('worst_frame_timestamp')}, 'winner': "A" }
                continue

            final_avg_a = np.mean([res['score'] for res in data['a']])
            final_avg_b = np.mean([res['score'] for res in data['b']])
            worst_chunk_a = max(data['a'], key=lambda x: x.get('peak_score', x.get('score', 0)))
            worst_chunk_b = max(data['b'], key=lambda x: x.get('peak_score', x.get('score', 0)))

            final_issues[issue_name] = {
                'a': {'score': final_avg_a, 'summary': worst_chunk_a['summary'], 'worst_frame_timestamp': worst_chunk_a.get('worst_frame_timestamp')},
                'b': {'score': final_avg_b, 'summary': worst_chunk_b['summary'], 'worst_frame_timestamp': worst_chunk_b.get('worst_frame_timestamp')},
                'winner': "A" if final_avg_a <= final_avg_b else "B"
            }

        wins_a = sum(1 for d in final_issues.values() if d['winner'] == 'A')
        wins_b = sum(1 for d in final_issues.values() if d['winner'] == 'B')
        total_cats = len(final_issues)
        verdict = "Tie."
        if wins_a > wins_b: verdict = f"Source A is recommended, winning in {wins_a} of {total_cats} categories."
        elif wins_b > wins_a: verdict = f"Source B is recommended, winning in {wins_b} of {total_cats} categories."
        self._emit("Complete", 100)
        payload = {"source_a": self.source_a.info, "source_b": self.source_b.info, "alignment_offset_secs": align.offset_sec, "alignment_drift_ppm": align.drift_ppm, "verdict": verdict, "issues": final_issues}
        self.finished.emit(payload)
