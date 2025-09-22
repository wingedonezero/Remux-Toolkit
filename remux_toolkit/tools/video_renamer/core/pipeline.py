"""
core/pipeline.py - Main matching pipeline orchestrator
"""

from pathlib import Path
from typing import List, Dict, Generator, Optional
from dataclasses import dataclass
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from remux_toolkit.tools.video_renamer.matchers.audio.correlation import CorrelationMatcher
from remux_toolkit.tools.video_renamer.matchers.audio.chromaprint import ChromaprintMatcher
from remux_toolkit.tools.video_renamer.matchers.audio.peak_matcher import PeakMatcher
from remux_toolkit.tools.video_renamer.matchers.audio.invariant_matcher import InvariantMatcher
from remux_toolkit.tools.video_renamer.matchers.audio.mfcc import MFCCMatcher
from remux_toolkit.tools.video_renamer.matchers.video.phash import PerceptualHashMatcher
from remux_toolkit.tools.video_renamer.matchers.video.scene import SceneDetectionMatcher
from remux_toolkit.tools.video_renamer.matchers.video.videohash_matcher import VideoHashMatcher

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
        self._num_workers = 8

    def set_mode(self, mode: str):
        self._mode = mode

    def set_language(self, language: Optional[str]):
        self._language = language.lower() if language else None

    def set_threshold(self, threshold: float):
        self._threshold = threshold

    def set_num_workers(self, num_workers: int):
        self._num_workers = max(1, num_workers)

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

        yield {'type': 'progress', 'message': f'Starting {self._mode} matching with {self._num_workers} workers...', 'value': 0}

        fingerprinting_modes = ['chromaprint', 'peak_matcher', 'invariant_matcher', 'videohash']
        if self._mode in fingerprinting_modes:
            yield from self._run_fingerprint_batch(references, remuxes)
        else:
            yield from self._run_exhaustive_compare(references, remuxes)

        if self._running:
             yield {'type': 'progress', 'message': 'Matching complete', 'value': 100}
        else:
             yield {'type': 'progress', 'message': 'Matching stopped', 'value': 0}


    def _run_fingerprint_batch(self, references: List[Path], remuxes: List[Path]):
        total_files = len(references) + len(remuxes)
        files_done = 0
        ref_fingerprints = {}
        remux_fingerprints = {}
        with ThreadPoolExecutor(max_workers=self._num_workers) as executor:
            tasks = {executor.submit(self._matcher.get_fingerprint, path, self._language): (path, 'ref') for path in references}
            tasks.update({executor.submit(self._matcher.get_fingerprint, path, self._language): (path, 'remux') for path in remuxes})
            for future in as_completed(tasks):
                if not self._running:
                    executor.shutdown(wait=False, cancel_futures=True)
                    return
                path, file_type = tasks[future]
                try:
                    fp = future.result()
                    if fp is not None:
                        if file_type == 'ref': ref_fingerprints[path] = fp
                        else: remux_fingerprints[path] = fp
                except Exception as e: print(f"Error fingerprinting {path.name}: {e}")
                files_done += 1
                progress = int((files_done / total_files) * 50) if total_files > 0 else 0
                yield {'type': 'progress', 'message': f'Analyzing files: {files_done}/{total_files}', 'value': progress}
        if not self._running: return
        yield {'type': 'progress', 'message': 'Comparing fingerprints...', 'value': 50}
        all_matches = defaultdict(list)
        used_references = set()
        for remux_path, remux_fp in remux_fingerprints.items():
            for ref_path, ref_fp in ref_fingerprints.items():
                score = self._matcher.compare_fingerprints(ref_fp, remux_fp)
                all_matches[remux_path].append({'ref': ref_path, 'score': score})
        for remux_path, matches in all_matches.items():
            sorted_matches = sorted(matches, key=lambda x: x['score'], reverse=True)
            if sorted_matches and sorted_matches[0]['score'] >= self._threshold: used_references.add(sorted_matches[0]['ref'])
            yield {'type': 'match_list', 'data': {'remux_path': str(remux_path), 'matches': sorted_matches}}
        for remux_path in remuxes:
            if remux_path not in all_matches: yield {'type': 'match_list', 'data': {'remux_path': str(remux_path), 'matches': []}}
        for ref_path in references:
            if ref_path not in used_references: yield {'type': 'unused_ref', 'data': {'reference_path': str(ref_path)}}

    def _run_exhaustive_compare(self, references: List[Path], remuxes: List[Path]):
        all_matches = defaultdict(list)
        used_references = set()
        comparison_pairs = []
        for ref_path in references:
            for remux_path in remuxes:
                 if self._should_compare(ref_path, remux_path):
                      comparison_pairs.append((ref_path, remux_path))
        total_comparisons = len(comparison_pairs)
        comparisons_done = 0
        with ThreadPoolExecutor(max_workers=self._num_workers) as executor:
            tasks = {executor.submit(self._matcher.compare, ref, remux, self._language): (ref, remux) for ref, remux in comparison_pairs}
            for future in as_completed(tasks):
                if not self._running:
                    executor.shutdown(wait=False, cancel_futures=True)
                    return
                ref_path, remux_path = tasks[future]
                try:
                    score, info = future.result()
                    all_matches[remux_path].append({'ref': ref_path, 'score': score, 'info': info})
                except Exception as e: print(f"Error comparing {ref_path.name} to {remux_path.name}: {e}")
                comparisons_done += 1
                progress = int((comparisons_done / total_comparisons) * 100) if total_comparisons > 0 else 0
                yield {'type': 'progress', 'message': f'Comparing pairs: {comparisons_done}/{total_comparisons}', 'value': progress}
        if not self._running: return
        for remux_path, matches in all_matches.items():
            sorted_matches = sorted(matches, key=lambda x: x['score'], reverse=True)
            if sorted_matches and sorted_matches[0]['score'] >= self._threshold: used_references.add(sorted_matches[0]['ref'])
            yield {'type': 'match_list', 'data': {'remux_path': str(remux_path), 'matches': sorted_matches}}
        for remux_path in remuxes:
            if remux_path not in all_matches: yield {'type': 'match_list', 'data': {'remux_path': str(remux_path), 'matches': []}}
        for ref_path in references:
            if ref_path not in used_references: yield {'type': 'unused_ref', 'data': {'reference_path': str(ref_path)}}

    def _get_matcher(self):
        if self._mode == "correlation": return CorrelationMatcher(self.cache, self.config, self.app_data_dir)
        elif self._mode == "chromaprint": return ChromaprintMatcher(self.cache, self.config, self.app_data_dir)
        elif self._mode == "peak_matcher": return PeakMatcher(self.cache, self.config, self.app_data_dir)
        elif self._mode == "invariant_matcher": return InvariantMatcher(self.cache, self.config, self.app_data_dir)
        elif self._mode == "mfcc": return MFCCMatcher(self.cache, self.config, self.app_data_dir)
        elif self._mode == "phash": return PerceptualHashMatcher(self.cache, self.config, self.app_data_dir)
        elif self._mode == "scene": return SceneDetectionMatcher(self.cache, self.config, self.app_data_dir)
        elif self._mode == "videohash": return VideoHashMatcher(self.cache, self.config, self.app_data_dir)
        return None

    def _should_compare(self, ref_path: Path, remux_path: Path) -> bool:
        ref_duration = self.cache.get_duration(ref_path)
        remux_duration = self.cache.get_duration(remux_path)
        if ref_duration and remux_duration and abs(ref_duration - remux_duration) > 5.0:
            return False
        return True
