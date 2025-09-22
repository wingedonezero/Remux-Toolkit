import os
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import Tuple, Optional, Dict, List, Any
from remux_toolkit.tools.video_renamer.core.matcher import BaseMatcher
from remux_toolkit.tools.video_renamer.utils.media import extract_audio_to_wav

class PanakoMatcher(BaseMatcher):
    """Panako integration using the two-step batch process."""

    def __init__(self, cache, config, app_data_dir: Path):
        super().__init__(cache, config, app_data_dir)
        self.panako_jar = self.config.get('panako_jar')
        self.panako_work_dir = self.app_data_dir / "panako"
        self.panako_work_dir.mkdir(exist_ok=True)

    def compare(self, ref_path: Path, remux_path: Path, language: Optional[str] = None) -> Tuple[float, str]:
        # This method is not used by the new pipeline logic but is kept for compatibility
        ref_fp = self.get_fingerprint(ref_path, language)
        remux_fp = self.get_fingerprint(remux_path, language)
        if not ref_fp or not remux_fp:
            return 0.0, "Fingerprint failed"
        score = self.compare_fingerprints(ref_fp, remux_fp)
        return score, f"Panako similarity {score:.1%}"

    def get_fingerprint(self, path: Path, language: Optional[str] = None) -> Optional[Any]:
        """Generates a Panako fingerprint by creating and querying a temporary DB."""
        if not self.panako_jar or not Path(self.panako_jar).exists():
            return None

        with tempfile.TemporaryDirectory(prefix="panako_fp_", dir=self.panako_work_dir) as temp_dir_str:
            temp_dir = Path(temp_dir_str)
            wav = self._prepare_wav(path, language, temp_dir, "fp_wav")
            if not wav: return None

            # Store the wav to create a fingerprint/DB entry for it
            self._run_panako_cmd(temp_dir, ['store', 'STRATEGY=panako', str(wav)])
            # Query the DB against itself to extract the fingerprint data
            result_output = self._run_panako_cmd(temp_dir, ['query', 'STRATEGY=panako', str(wav)], capture=True)
            if not result_output: return None

            # The parsed output serves as the "fingerprint" for this file
            return self._parse_panako_output(result_output)

    def compare_fingerprints(self, fp1: Any, fp2: Any) -> float:
        """
        For Panako, the 'fingerprints' are result dictionaries.
        A true match is when the coverage is high. We can't directly compare two
        fingerprints, so we return 1.0 if they are identical, 0.0 otherwise.
        The main pipeline handles the actual matching. This is a bit of a hack
        to fit the interface, but the heavy lifting is done in the pipeline.
        A better implementation would involve a more complex comparison here.
        For now, we rely on the pipeline's exhaustive search.
        Since the 'fingerprint' is the result of a self-query, we can't compare two different ones.
        The logic in the pipeline's _run_fingerprint_batch is what matters.
        Let's just return a basic similarity for now.
        """
        if fp1 and fp2 and 'match_score' in fp1 and 'match_score' in fp2:
            return 1.0 - abs(fp1['match_score'] - fp2['match_score']) / max(fp1['match_score'], fp2['match_score'], 1)
        return 0.0

    def _prepare_wav(self, path: Path, language: Optional[str], work_dir: Path, prefix: str) -> Optional[Path]:
        stream_idx = self.get_audio_stream_index(path, language)
        if stream_idx is None: return None
        wav_path = work_dir / f"{prefix}_{path.stem}.wav"
        if extract_audio_to_wav(path, stream_idx, wav_path, sample_rate=22050):
            return wav_path
        return None

    def _run_panako_cmd(self, work_dir: Path, args: List[str], capture: bool = False) -> Optional[str]:
        try:
            cmd = ['java', f'-Duser.home={work_dir}', '--add-opens=java.base/java.nio=ALL-UNNAMED', '-jar', str(self.panako_jar), *args]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

            if capture:
                return result.stdout if result.returncode == 0 else None
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    def _parse_panako_output(self, output: str) -> Optional[Dict]:
        best_result = None
        for line in output.splitlines():
            if ';' not in line: continue
            parts = [p.strip() for p in line.split(";")]
            if len(parts) < 13: continue
            try:
                score = int(float(parts[9]))
                seconds_ratio = float(parts[12].replace("%", "")) / 100.0
                current_result = {"match_score": score, "seconds_ratio": seconds_ratio}
                if best_result is None or current_result["seconds_ratio"] > best_result["seconds_ratio"]:
                    best_result = current_result
            except (ValueError, IndexError):
                continue
        return best_result
