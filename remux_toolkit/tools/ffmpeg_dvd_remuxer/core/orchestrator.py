# remux_toolkit/tools/ffmpeg_dvd_remuxer/core/orchestrator.py
from pathlib import Path
import json

from ..steps import DemuxStep, CCExtractStep, ChaptersStep, FinalizeStep
from ..utils.helpers import run_stream, run_capture

class Orchestrator:
    def __init__(self, config):
        self.config = config
        # The redundant ProbeStep has been removed from the pipeline
        self.steps = [
            DemuxStep(self.config),
            CCExtractStep(self.config),
            ChaptersStep(self.config),
            FinalizeStep(self.config),
        ]

    def analyze_disc(self, path: Path, log_emitter, stop_event) -> tuple[list, str]:
        """Enumerate titles via ffprobe dvdvideo for richer, more reliable data."""
        def fmt_len(s: float | None) -> str:
            if not s: return "00:00:00.000"
            h = int(s // 3600); m = int((s % 3600) // 60); sec = s - 3600*h - 60*m
            return f"{h:02d}:{m:02d}:{sec:06.3f}"

        log_emitter(f"Analyzing {path} with ffprobe...")
        titles = []
        max_scan, misses = 99, 0

        for t in range(1, max_scan + 1):
            if stop_event.is_set():
                return [], "Analysis stopped by user."

            cmd = ["ffprobe", "-v", "error", "-f", "dvdvideo", "-title", str(t),
                   "-show_chapters", "-show_streams", "-show_format",
                   "-print_format", "json", str(path)]
            rc, out = run_capture(cmd)
            if rc != 0 or not out.strip():
                misses += 1
                if misses >= 3:
                    log_emitter(f"Stopping scan after {misses} consecutive empty titles.")
                    break
                continue

            misses = 0
            try:
                data = json.loads(out)
                v_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
                if not v_streams: continue

                dur_s = float(data.get("format", {}).get("duration", 0))
                chapters = len(data.get("chapters", []))
                a_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
                s_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "subtitle"]

                # Capture field order from the first video stream
                field_order_str = v_streams[0].get('field_order')
                field_order = None
                if field_order_str in ('tt', 'tb'): field_order = 'top first'
                elif field_order_str in ('bb', 'bt'): field_order = 'bottom first'

                titles.append({
                    "title": str(t),
                    "length": fmt_len(dur_s),
                    "chapters": str(chapters),
                    "audio": str(len(a_streams)),
                    "subs": str(len(s_streams)),
                    "v_codecs": ",".join(sorted({s.get("codec_name","") for s in v_streams})),
                    "a_codecs": ",".join(sorted({s.get("codec_name","") for s in a_streams})),
                    "field_order": field_order,
                })
            except (json.JSONDecodeError, ValueError):
                log_emitter(f"Could not parse ffprobe output for title {t}.")
                continue

        if not titles:
            return [], "No valid titles were found on the disc."

        return titles, f"Analysis complete. Found {len(titles)} titles."

    def run_pipeline(self, context: dict, log_emitter, stop_event) -> bool:
        title_num = context['title_num']
        log_emitter(f"--- Processing Title {title_num} ---")

        out_folder = context['out_folder']
        context['temp_mkv_path'] = out_folder / f"title_{title_num}_temp.mkv"
        context['cc_srt_path'] = out_folder / f"title_{title_num}_cc.srt"
        context['mod_chap_xml_path'] = out_folder / f"title_{title_num}_chapters_mod.xml"

        files_to_clean = [context['temp_mkv_path'], context['cc_srt_path'], context['mod_chap_xml_path']]

        try:
            for step in self.steps:
                if stop_event.is_set(): return False
                success = step.run(context, log_emitter, stop_event)
                if not success:
                    log_emitter(f"!! Step {step.__class__.__name__} failed for Title {title_num}. Aborting title.")
                    return False
            return True
        finally:
            log_emitter(f"Cleaning up temporary files for Title {title_num}...")
            for f in files_to_clean:
                if f.exists():
                    try: f.unlink()
                    except OSError: pass
