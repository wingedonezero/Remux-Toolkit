"""
core/pipeline.py - Main matching pipeline orchestrator
"""

from pathlib import Path
from typing import List, Dict, Generator, Optional
from dataclasses import dataclass

from remux_toolkit.tools.video_renamer.matchers.audio.correlation import CorrelationMatcher
from remux_toolkit.tools.video_renamer.matchers.audio.chromaprint import ChromaprintMatcher
from remux_toolkit.tools.video_renamer.matchers.audio.peak_matcher import PeakMatcher
from remux_toolkit.tools.video_renamer.matchers.audio.invariant_matcher import InvariantMatcher
from remux_toolkit.tools.video_renamer.matchers.audio.mfcc import MFCCMatcher
from remux_toolkit.tools.video_renamer.matchers.video.phash import PerceptualHashMatcher
from remux_toolkit.tools.video_renamer.matchers.video.scene import SceneDetectionMatcher

@dataclass
class MatchConfig:
    mode: str = "correlation"
    language: Optional[str] = None
    confidence_threshold: float = 0.75

class MatchingPipeline:
    """Orchestrates the matching process using selected matcher"""

    def __init__(self, cache, config, app_data_dir: Path):
        self.cache = cache
        self.config = config
        self.app_data_dir = app_data_dir
        self._mode = "correlation"
        self._language = None
        self._threshold = 0.75
        self._running = False
        self._matcher = None

    def set_mode(self, mode: str):
        self._mode = mode

    def set_language(self, language: Optional[str]):
        self._language = language.lower() if language else None

    def set_threshold(self, threshold: float):
        self._threshold = threshold

    def stop(self):
        self._running = False
        if self._matcher:
            self._matcher.stop()

    def match(self, references: List[Path], remuxes: List[Path]) -> Generator[Dict, None, None]:
        self._running = True
        self._matcher = self._get_matcher()
        if not self._matcher:
            yield {'type': 'progress', 'message': 'Invalid matcher mode', 'value': 0}
            return

        yield {'type': 'progress', 'message': f'Starting {self._mode} matching...', 'value': 0}

        audio_fingerprinters = ['chromaprint', 'peak_matcher', 'invariant_matcher']
        if self._mode in audio_fingerprinters:
            yield from self._run_fingerprint_batch(references, remuxes)
        else:
            yield from self._run_exhaustive_compare(references, remuxes)

        yield {'type': 'progress', 'message': 'Matching complete', 'value': 100}

    def _run_fingerprint_batch(self, references: List[Path], remuxes: List[Path]):
        """Runs a fast, two-step process for fingerprinting matchers."""
        total_files = len(references) + len(remuxes)
        files_done = 0

        ref_fingerprints = {}
        for ref_path in references:
            if not self._running: return
            files_done += 1
            progress = int((files_done / total_files) * 50) if total_files > 0 else 0
            yield {'type': 'progress', 'message': f'Analyzing ref: {ref_path.name}', 'value': progress}
            fp = self._matcher.get_fingerprint(ref_path, self._language)
            if fp: ref_fingerprints[ref_path] = fp

        remux_fingerprints = {}
        for remux_path in remuxes:
            if not self._running: return
            files_done += 1
            progress = int((files_done / total_files) * 50) if total_files > 0 else 0
            yield {'type': 'progress', 'message': f'Analyzing remux: {remux_path.name}', 'value': progress}
            fp = self._matcher.get_fingerprint(remux_path, self._language)
            if fp: remux_fingerprints[remux_path] = fp

        yield {'type': 'progress', 'message': 'Comparing fingerprints...', 'value': 50}
        best_matches = {remux: {'score': -1, 'ref': None} for remux in remuxes}
        used_references = set()

        for remux_path, remux_fp in remux_fingerprints.items():
            for ref_path, ref_fp in ref_fingerprints.items():
                if not self._running: return
                score = self._matcher.compare_fingerprints(ref_fp, remux_fp)
                if score > best_matches[remux_path]['score']:
                    best_matches[remux_path] = {'score': score, 'ref': ref_path}

        for remux_path, match_info in best_matches.items():
            best_ref = match_info['ref']
            best_score = match_info['score']
            if best_ref and best_score >= self._threshold:
                used_references.add(best_ref)

            info = f"{self._mode.replace('_', ' ').capitalize()} similarity: {best_score:.1%}" if best_score >= 0 else "Fingerprint failed"
            yield {'type': 'match', 'data': {'remux_path': str(remux_path), 'reference_path': str(best_ref) if best_ref else None, 'confidence': best_score, 'info': info}}

        for ref_path in references:
            if ref_path not in used_references:
                yield {'type': 'match', 'data': {'remux_path': None, 'reference_path': str(ref_path), 'confidence': 0.0, 'info': 'Reference file not used', 'status': 'Unused'}}

    def _run_exhaustive_compare(self, references: List[Path], remuxes: List[Path]):
        """Original exhaustive comparison for non-fingerprinting modes."""
        best_matches = {remux: {'score': -1, 'ref': None, 'info': ''} for remux in remuxes}
        used_references = set()
        total_comparisons = len(references) * len(remuxes)
        comparisons_done = 0

        for ref_path in references:
            if not self._running: break
            for remux_path in remuxes:
                if not self._running: break

                comparisons_done += 1
                progress = int((comparisons_done / total_comparisons) * 100) if total_comparisons > 0 else 0
                yield {'type': 'progress', 'message': f'Comparing {ref_path.name} to {remux_path.name}', 'value': progress}

                if not self._should_compare(ref_path, remux_path): continue

                score, info = self._matcher.compare(ref_path, remux_path, self._language)

                if score > best_matches[remux_path]['score']:
                    best_matches[remux_path] = {'score': score, 'ref': ref_path, 'info': info}

        if self._running:
            for remux_path, match_info in best_matches.items():
                best_ref = match_info['ref']
                best_score = match_info['score']

                if best_ref and best_score >= self._threshold:
                    used_references.add(best_ref)

                if best_ref and best_score >= 0:
                    yield {'type': 'match', 'data': {'remux_path': str(remux_path), 'reference_path': str(best_ref), 'confidence': best_score, 'info': match_info['info']}}
                else:
                    yield {'type': 'match', 'data': {'remux_path': str(remux_path), 'reference_path': None, 'confidence': 0.0, 'info': 'No suitable match found'}}

            for ref_path in references:
                if ref_path not in used_references:
                    yield {'type': 'match', 'data': {'remux_path': None, 'reference_path': str(ref_path), 'confidence': 0.0, 'info': 'Reference file not used', 'status': 'Unused'}}

    def _get_matcher(self):
        if self._mode == "correlation":
            return CorrelationMatcher(self.cache, self.config, self.app_data_dir)
        elif self._mode == "chromaprint":
            return ChromaprintMatcher(self.cache, self.config, self.app_data_dir)
        elif self._mode == "peak_matcher":
            return PeakMatcher(self.cache, self.config, self.app_data_dir)
        elif self._mode == "invariant_matcher":
            return InvariantMatcher(self.cache, self.config, self.app_data_dir)
        elif self._mode == "mfcc":
            return MFCCMatcher(self.cache, self.config, self.app_data_dir)
        elif self._mode == "phash":
            return PerceptualHashMatcher(self.cache, self.config, self.app_data_dir)
        elif self._mode == "scene":
            return SceneDetectionMatcher(self.cache, self.config, self.app_data_dir)
        return None

    def _should_compare(self, ref_path: Path, remux_path: Path) -> bool:
        ref_duration = self.cache.get_duration(ref_path)
        remux_duration = self.cache.get_duration(remux_path)
        if ref_duration and remux_duration:
            if abs(ref_duration - remux_duration) > 5.0:
                return False
        return True
