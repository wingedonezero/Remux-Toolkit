# remux_toolkit/tools/ffmpeg_dvd_remuxer/core/orchestrator.py
from pathlib import Path

from ..steps import DemuxStep, CCExtractStep, ChaptersStep, FinalizeStep, DiscAnalysisStep, MetadataAnalysisStep, TelecineDetectionStep, IfoParserStep

class Orchestrator:
    def __init__(self, config, temp_dir: Path):
        self.config = config
        self.temp_dir = temp_dir

        # Analysis step is separate since it runs during queue addition
        self.analysis_step = DiscAnalysisStep(self.config)

        # Processing pipeline steps
        self.steps = [
            IfoParserStep(self.config),  # First, parse IFO for DVD metadata
            MetadataAnalysisStep(self.config),  # Then analyze with ffprobe and merge
            DemuxStep(self.config),
            CCExtractStep(self.config),
            ChaptersStep(self.config),
            TelecineDetectionStep(self.config),  # Detect telecined content
            FinalizeStep(self.config),
        ]

    def analyze_disc(self, path: Path, log_emitter, stop_event) -> tuple[list, str]:
        """Delegate disc analysis to the DiscAnalysisStep."""
        return self.analysis_step.run(path, self.temp_dir, log_emitter, stop_event)

    def run_pipeline(self, context: dict, log_emitter, stop_event):
        """Run the processing pipeline. This is a generator that yields progress updates."""
        title_num = context['title_num']
        log_emitter(f"--- Processing Title {title_num} ---")

        out_folder = context['out_folder']
        context['temp_mkv_path'] = out_folder / f"title_{title_num}_temp.mkv"
        context['cc_srt_path'] = out_folder / f"title_{title_num}_cc.srt"
        context['mod_chap_xml_path'] = out_folder / f"title_{title_num}_chapters_mod.xml"

        files_to_clean = [
            context['temp_mkv_path'],
            context['cc_srt_path'],
            context['mod_chap_xml_path']
        ]

        # Get list of enabled steps for accurate numbering
        enabled_steps = []
        for step in self.steps:
            # Check if step has is_enabled property and if it's False, skip it
            if hasattr(step, 'is_enabled') and not step.is_enabled:
                continue
            enabled_steps.append(step)

        total_steps = len(enabled_steps)

        try:
            step_num = 0
            for step in self.steps:
                if stop_event.is_set(): return

                # Check if this step should be counted/numbered
                is_numbered_step = not (hasattr(step, 'is_enabled') and not step.is_enabled)
                if is_numbered_step:
                    step_num += 1
                    context['step_info'] = f"[STEP {step_num}/{total_steps}]"
                else:
                    context['step_info'] = "[OPTIONAL]"

                step_runner = step.run(context, log_emitter, stop_event)
                if hasattr(step_runner, '__iter__') or hasattr(step_runner, '__next__'):
                    final_status = False
                    for progress_update in step_runner:
                        if isinstance(progress_update, bool):
                            final_status = progress_update
                        else:
                            yield progress_update
                    success = final_status
                else:
                    success = step_runner

                if not success:
                    log_emitter(f"!! Step {step.__class__.__name__} failed for Title {title_num}. Aborting title.")
                    return
        finally:
            log_emitter(f"Cleaning up temporary files for Title {title_num}...")
            # Improved cleanup logic to remove all temp files
            for f in files_to_clean:
                if f.exists():
                    try:
                        f.unlink()
                    except OSError:
                        pass
