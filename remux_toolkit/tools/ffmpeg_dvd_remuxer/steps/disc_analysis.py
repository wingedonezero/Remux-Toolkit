# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/disc_analysis.py
import json
from pathlib import Path
from ..utils.helpers import run_capture
from ..utils.paths import get_base_name

class DiscAnalysisStep:
    """Step for analyzing DVD disc structure and titles."""

    def __init__(self, config):
        self.config = config

    def run(self, path: Path, temp_dir: Path, log_emitter, stop_event) -> tuple[list, str]:
        """Analyze a DVD disc and return list of titles.

        Note: This step runs differently than pipeline steps - it's called during queue addition,
        not during processing, so it has a different signature.
        """
        def fmt_len(s: float | None) -> str:
            if not s: return "00:00:00.000"
            h = int(s // 3600); m = int((s % 3600) // 60); sec = s - 3600*h - 60*m
            return f"{h:02d}:{m:02d}:{sec:06.3f}"

        log_emitter(f"Analyzing {path} with ffprobe...")
        titles = []
        max_scan, misses = 99, 0
        disc_base_name = get_base_name(path)

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
                    break
                continue

            # Save probe data for debugging
            try:
                probe_file = temp_dir / f"{disc_base_name}_title_{t}_probe.json"
                probe_file.write_text(out, encoding='utf-8')
            except IOError:
                pass

            misses = 0

            try:
                data = json.loads(out)
                all_streams = data.get("streams", [])
                v_streams = [s for s in all_streams if s.get("codec_type") == "video"]

                if not v_streams:
                    continue

                dur_s = float(data.get("format", {}).get("duration", 0))
                chapters = len(data.get("chapters", []))
                a_streams = [s for s in all_streams if s.get("codec_type") == "audio"]
                s_streams = [s for s in all_streams if s.get("codec_type") == "subtitle"]

                field_order_str = v_streams[0].get('field_order')
                field_order = 'top first' if field_order_str in ('tt', 'tb') else (
                    'bottom first' if field_order_str in ('bb', 'bt') else None
                )

                titles.append({
                    "title": str(t),
                    "length": fmt_len(dur_s),
                    "chapters": str(chapters),
                    "audio": str(len(a_streams)),
                    "subs": str(len(s_streams)),
                    "v_codecs": ",".join(sorted({s.get("codec_name","") for s in v_streams})),
                    "a_codecs": ",".join(sorted({s.get("codec_name","") for s in a_streams})),
                    "field_order": field_order,
                    "streams": all_streams,
                })
            except (json.JSONDecodeError, ValueError):
                log_emitter(f"Could not parse ffprobe output for title {t}.")
                continue

        if not titles:
            return [], "No valid titles were found on the disc."

        return titles, f"Analysis complete. Found {len(titles)} titles."
