# remux_toolkit/tools/ffmpeg_dvd_remuxer/core/orchestrator.py
from pathlib import Path
import re
import shutil

from ..steps import * # Import all step classes
from ..utils.helpers import run_stream

class Orchestrator:
    def __init__(self, config, logger):
        self.config = config
        self.log = logger
        self.steps = [
            DemuxStep(config, logger),
            ProbeStep(config, logger),
            CCExtractStep(config, logger),
            ChaptersStep(config, logger),
            FinalizeStep(config, logger),
        ]

    def analyze_disc(self, path: Path, stop_event) -> tuple[list, str]:
        """Analyzes a DVD with lsdvd and returns all found titles."""
        self.log.emit(f"Analyzing {path} with lsdvd...")
        output_lines = [line for line in run_stream(["lsdvd", str(path)], stop_event)]
        full_output = "\n".join(output_lines)

        if "Failed to execute" in full_output or "read failed" in full_output.lower():
            return [], "lsdvd failed or reported errors reading the disc."

        titles = []
        title_re = re.compile(r"Title: (?P<title>\d+),.*Length: (?P<length>[\d:.]+).*Chapters: (?P<chapters>\d+).*Audio streams: (?P<audio>\d+).*Subpictures: (?P<subs>\d+)")
        for match in title_re.finditer(full_output):
            titles.append(match.groupdict())

        if not titles:
            return [], "No valid titles were found on the disc."

        return titles, f"Analysis complete. Found {len(titles)} titles."

    def run_pipeline(self, context: dict, stop_event) -> bool:
        """Executes the pipeline of steps for a single title."""
        title_num = context['title_num']
        self.log.emit(f"--- Processing Title {title_num} ---")

        out_folder = context['out_folder']
        context['temp_mkv_path'] = out_folder / f"title_{title_num}_temp.mkv"
        context['cc_srt_path'] = out_folder / f"title_{title_num}_cc.srt"
        context['mod_chap_xml_path'] = out_folder / f"title_{title_num}_chapters_mod.xml"

        files_to_clean = [context['temp_mkv_path'], context['cc_srt_path'], context['mod_chap_xml_path']]

        try:
            for step in self.steps:
                if stop_event.is_set(): return False

                if hasattr(step, 'is_enabled') and not step.is_enabled:
                    continue

                success = step.run(context, stop_event)
                if not success:
                    self.log.emit(f"!! Step {step.__class__.__name__} failed for Title {title_num}. Aborting title.")
                    return False
            return True
        finally:
            self.log.emit(f"Cleaning up temporary files for Title {title_num}...")
            for f in files_to_clean:
                if f.exists():
                    try: f.unlink()
                    except OSError: pass
