# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/demux.py
import re
from ..utils.helpers import run_stream, time_str_to_seconds

class DemuxStep:
    def __init__(self, config):
        self.config = config

    def run(self, context: dict, log_emitter, stop_event):
        """This step is now a generator to yield real-time ffmpeg progress."""
        log_emitter("[STEP 1/5] Demuxing to temporary file with ffmpeg...")

        input_path = context['input_path']
        title_num = context['title_num']
        temp_mkv = context['out_folder'] / f"title_{title_num}_temp.mkv"
        context['temp_mkv_path'] = temp_mkv

        # Get total duration for progress calculation
        title_info = context.get('title_info', {})
        duration_s = time_str_to_seconds(title_info.get('length'))
        duration_us = duration_s * 1_000_000 if duration_s > 0 else 0

        ffmpeg_cmd = [
            "ffmpeg", "-y", "-hide_banner",
            "-progress", "-", "-nostats",
            "-preindex", "1",  # Added for more reliable stream reading
            "-f", "dvdvideo", "-title", str(title_num), "-i", str(input_path),
            "-map", "0", "-c:v", "copy", "-c:a", "copy", "-c:s", "copy"
        ]
        if self.config.get("remove_eia_608", True):
            ffmpeg_cmd.extend(["-bsf:v", "filter_units=remove_types=178"])
        ffmpeg_cmd.append(str(temp_mkv))

        for line in run_stream(ffmpeg_cmd, stop_event):
            # Parse ffmpeg's progress output and yield percentages
            if line.strip().startswith("out_time_us="):
                try:
                    current_us = int(line.strip().split('=')[1])
                    if duration_us > 0:
                        percent = int((current_us / duration_us) * 100)
                        yield min(100, max(0, percent))
                except (ValueError, IndexError):
                    pass
            else:
                log_emitter(line)

        if stop_event.is_set():
            yield False # The last yielded value is the success status
            return

        if not temp_mkv.exists() or temp_mkv.stat().st_size < 1024:
            log_emitter("!! ERROR: ffmpeg failed to create the temporary MKV file. Aborting.")
            yield False
            return

        log_emitter("  -> Temporary file created successfully.")
        yield True
