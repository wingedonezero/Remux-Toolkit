# remux_toolkit/tools/video_ab_comparator/core/pipeline.py

from PyQt6.QtCore import QObject, pyqtSignal
from pathlib import Path

from .source import VideoSource
from .alignment import robust_align, _safe_fraction_to_fps
from .models import ComparisonResult

# detectors
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
    """Orchestrates the entire A/B comparison process."""
    progress = pyqtSignal(str, int)
    finished = pyqtSignal(dict)

    def __init__(self, path_a: str, path_b: str):
        super().__init__()
        self.source_a = VideoSource(Path(path_a))
        self.source_b = VideoSource(Path(path_b))

    # ---------- helpers ----------

    def _emit(self, msg: str, pc: int):
        self.progress.emit(msg, pc)

    # ---------- main ----------

    def run(self):
        # 1) Probe
        self._emit("Probing sources…", 5)
        if not self.source_a.probe() or not self.source_b.probe():
            self.finished.emit({"error": "ffprobe failed"})
            return

        fps_a = _safe_fraction_to_fps(next((s.frame_rate for s in self.source_a.info.streams if s.codec_type == 'video'), None))
        fps_b = _safe_fraction_to_fps(next((s.frame_rate for s in self.source_b.info.streams if s.codec_type == 'video'), None))
        duration = min(self.source_a.info.duration, self.source_b.info.duration)

        # 2) Align
        self._emit("Computing alignment (hash + SSIM)…", 18)
        align = robust_align(self.source_a, self.source_b, fps_a=fps_a, fps_b=fps_b, duration=duration)
        offset_sec = align.offset_sec
        drift_ppm = align.drift_ppm

        # 3) Run detectors
        detectors = [
            UpscaleDetector(), AspectRatioDetector(),
            BlockingDetector(), BandingDetector(), RingingDetector(), DotCrawlDetector(),
            ChromaShiftDetector(), RainbowingDetector(), ColorCastDetector(),
            DNRDetector(), SharpeningDetector(),
            CombingDetector(), GhostingDetector(), CadenceDetector(),
            AudioDetector()
        ]

        results = ComparisonResult(
            source_a=self.source_a.info,
            source_b=self.source_b.info,
            alignment_offset_secs=offset_sec,
            verdict=""
        )

        issues = {}
        total = len(detectors)
        for i, det in enumerate(detectors, start=1):
            self._emit(f"Running detector: {det.issue_name}…", 18 + int(70 * i / max(1, total)))
            try:
                a_res = det.run(self.source_a)
                b_res = det.run(self.source_b)
            except Exception as e:
                a_res = {'score': -1, 'summary': f'Error: {e}'}
                b_res = {'score': -1, 'summary': f'Error: {e}'}

            winner = "A" if a_res.get('score', 0) <= b_res.get('score', 0) else "B"
            issues[det.issue_name] = {
                'a': a_res, 'b': b_res, 'winner': winner
            }

        results.issues = issues

        # simple verdict
        wins_a = sum(1 for d in issues.values() if d['winner'] == 'A')
        wins_b = sum(1 for d in issues.values() if d['winner'] == 'B')
        if wins_a > wins_b:
            results.verdict = f"Source A is recommended, winning in {wins_a} of {max(1, wins_a+wins_b)} categories."
        elif wins_b > wins_a:
            results.verdict = f"Source B is recommended, winning in {wins_b} of {max(1, wins_a+wins_b)} categories."
        else:
            results.verdict = "Tie."

        # 4) Emit
        self._emit("Finalizing report…", 100)

        # include mapping parameters for the viewer
        payload = {
            "source_a": results.source_a,
            "source_b": results.source_b,
            "alignment_offset_secs": offset_sec,
            "alignment_drift_ppm": drift_ppm,
            "verdict": results.verdict,
            "issues": results.issues,
        }
        self.finished.emit(payload)
