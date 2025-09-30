# remux_toolkit/tools/video_ab_comparator/core/pipeline.py

from PyQt6.QtCore import QObject, pyqtSignal
from .source import VideoSource
from .alignment import align_sources
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
        self.source_a = VideoSource(path_a)
        self.source_b = VideoSource(path_b)
        self.detectors = [
            UpscaleDetector(),
            CombingDetector(),
            GhostingDetector(),
            CadenceDetector(),
            BlockingDetector(),
            BandingDetector(),
            RingingDetector(),
            DotCrawlDetector(),
            ChromaShiftDetector(),
            RainbowingDetector(),
            ColorCastDetector(),
            DNRDetector(),
            SharpeningDetector(),
            AspectRatioDetector(),
            AudioDetector(),
        ]

    def run(self):
        # 1. Probing
        self.progress.emit("Probing sources...", 10)
        if not self.source_a.probe() or not self.source_b.probe():
            self.progress.emit("Error: Could not probe one or both sources.", 0)
            self.finished.emit({})
            return

        # 2. Alignment
        self.progress.emit("Generating fingerprints for alignment...", 20)
        fp_a = self.source_a.generate_fingerprints()
        fp_b = self.source_b.generate_fingerprints()

        self.progress.emit("Aligning sources...", 40)
        frame_offset = align_sources(fp_a, fp_b)

        fps_a = 23.976
        if self.source_a.info and self.source_a.info.streams:
            try: fps_a = eval(self.source_a.info.streams[0].frame_rate)
            except Exception: pass

        time_offset_secs = frame_offset / fps_a if fps_a > 0 else 0.0

        # 3. Run Detectors
        results = {
            "source_a": self.source_a.info, "source_b": self.source_b.info,
            "alignment_offset_secs": time_offset_secs, "issues": {}
        }
        score_a, score_b, a_wins, b_wins = 0, 0, 0, 0

        total_steps = len(self.detectors) * 2
        step_idx = 0
        for detector in self.detectors:
            prog = 50 + int(50 * (step_idx / total_steps))
            self.progress.emit(f"Running '{detector.issue_name}' on Source A...", prog)
            result_a = detector.run(self.source_a)
            if result_a.get('score', -1) > 0: score_a += result_a['score']
            step_idx += 1

            prog = 50 + int(50 * (step_idx / total_steps))
            self.progress.emit(f"Running '{detector.issue_name}' on Source B...", prog)
            result_b = detector.run(self.source_b)
            if result_b.get('score', -1) > 0: score_b += result_b['score']
            step_idx += 1

            results["issues"][detector.issue_name] = {"a": result_a, "b": result_b}
            # FIX: Use 'result_a' and 'result_b' for comparison, not undefined 'a'
            if result_a.get('score', -1) > 0 and result_b.get('score', -1) > 0:
                if result_a['score'] < result_b['score']:
                    a_wins += 1
                elif result_b['score'] < result_a['score']:
                    b_wins += 1

        # 4. Final Verdict
        num_scored_categories = sum(1 for d in self.detectors if d.issue_name != "Audio Analysis")
        if a_wins > b_wins:
            results['verdict'] = f"Verdict: Source A is recommended, winning in {a_wins} of {num_scored_categories} categories."
        elif b_wins > a_wins:
            results['verdict'] = f"Verdict: Source B is recommended, winning in {b_wins} of {num_scored_categories} categories."
        else:
            # FIX: Compare 'score_b' to 'score_a', not 'a_wins'
            if score_a < score_b:
                results['verdict'] = "Verdict: Source A is recommended due to a lower overall issue score."
            elif score_b < score_a:
                results['verdict'] = "Verdict: Source B is recommended due to a lower overall issue score."
            else:
                results['verdict'] = "Verdict: Both sources appear to be of very similar quality."

        self.progress.emit("Finalizing report...", 100)
        self.finished.emit(results)
