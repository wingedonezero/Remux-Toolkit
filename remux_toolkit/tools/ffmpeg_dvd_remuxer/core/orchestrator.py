# remux_toolkit/tools/ffmpeg_dvd_remuxer/core/orchestrator.py
from pathlib import Path
import json

from ..steps import DemuxStep, CCExtractStep, ChaptersStep, FinalizeStep
from ..utils.helpers import run_stream, run_capture
from ..utils.paths import get_base_name

class Orchestrator:
    def __init__(self, config, temp_dir: Path):
        self.config = config
        self.temp_dir = temp_dir
        self.steps = [
            DemuxStep(self.config),
            CCExtractStep(self.config),
            ChaptersStep(self.config),
            FinalizeStep(self.config),
        ]

    def analyze_disc(self, path: Path, log_emitter, stop_event) -> tuple[list, str]:
        # This function is unchanged
        def fmt_len(s: float | None) -> str:
            if not s: return "00:00:00.000"
            h = int(s // 3600); m = int((s % 3600) // 60); sec = s - 3600*h - 60*m
            return f"{h:02d}:{m:02d}:{sec:06.3f}"
        log_emitter(f"Analyzing {path} with ffprobe...")
        titles = []
        max_scan, misses = 99, 0
        disc_base_name = get_base_name(path)
        for t in range(1, max_scan + 1):
            if stop_event.is_set(): return [], "Analysis stopped by user."
            cmd = ["ffprobe", "-v", "error", "-f", "dvdvideo", "-title", str(t), "-show_chapters", "-show_streams", "-show_format", "-print_format", "json", str(path)]
            rc, out = run_capture(cmd)
            if rc != 0 or not out.strip():
                misses += 1
                if misses >= 3: break
                continue
            try:
                probe_file = self.temp_dir / f"{disc_base_name}_title_{t}_probe.json"
                probe_file.write_text(out, encoding='utf-8')
            except IOError: pass
            misses = 0
            try:
                data = json.loads(out)
                all_streams = data.get("streams", [])
                v_streams = [s for s in all_streams if s.get("codec_type") == "video"]
                if not v_streams: continue
                dur_s = float(data.get("format", {}).get("duration", 0))
                chapters = len(data.get("chapters", []))
                a_streams = [s for s in all_streams if s.get("codec_type") == "audio"]
                s_streams = [s for s in all_streams if s.get("codec_type") == "subtitle"]
                field_order_str = v_streams[0].get('field_order')
                field_order = 'top first' if field_order_str in ('tt', 'tb') else ('bottom first' if field_order_str in ('bb', 'bt') else None)
                titles.append({
                    "title": str(t), "length": fmt_len(dur_s), "chapters": str(chapters),
                    "audio": str(len(a_streams)), "subs": str(len(s_streams)),
                    "v_codecs": ",".join(sorted({s.get("codec_name","") for s in v_streams})),
                    "a_codecs": ",".join(sorted({s.get("codec_name","") for s in a_streams})),
                    "field_order": field_order, "streams": all_streams,
                })
            except (json.JSONDecodeError, ValueError):
                log_emitter(f"Could not parse ffprobe output for title {t}.")
                continue
        if not titles: return [], "No valid titles were found on the disc."
        return titles, f"Analysis complete. Found {len(titles)} titles."

    def run_pipeline(self, context: dict, log_emitter, stop_event):
        """This is a generator that yields progress updates."""
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

        try:
            for step in self.steps:
                if stop_event.is_set(): return

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
