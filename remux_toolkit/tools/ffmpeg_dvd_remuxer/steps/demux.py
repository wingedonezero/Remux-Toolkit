# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/demux.py
from ..utils.helpers import run_stream

class DemuxStep:
    def __init__(self, config, logger):
        self.config = config
        self.log = logger

    def run(self, context: dict, stop_event) -> bool:
        self.log.emit("[STEP 1/5] Demuxing to temporary file with ffmpeg...")

        input_path = context['input_path']
        title_num = context['title_num']
        temp_mkv = context['out_folder'] / f"title_{title_num}_temp.mkv"
        context['temp_mkv_path'] = temp_mkv

        ffmpeg_cmd = [
            "ffmpeg", "-y", "-hide_banner", "-progress", "-", "-nostats",
            "-f", "dvdvideo", "-title", str(title_num), "-i", str(input_path),
            "-map", "0", "-c:v", "copy", "-c:a", "copy", "-c:s", "copy"
        ]
        if self.config.get("remove_eia_608", True):
            ffmpeg_cmd.extend(["-bsf:v", "filter_units=remove_types=178"])
        ffmpeg_cmd.append(str(temp_mkv))

        for line in run_stream(ffmpeg_cmd, stop_event):
            self.log.emit(line)
        if stop_event.is_set(): return False

        if not temp_mkv.exists() or temp_mkv.stat().st_size < 1024:
            self.log.emit("!! ERROR: ffmpeg failed to create the temporary MKV file. Aborting.")
            return False

        self.log.emit("  -> Temporary file created successfully.")
        return True
