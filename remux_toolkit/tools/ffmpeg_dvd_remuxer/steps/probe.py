# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/probe.py
import json
from ..utils.helpers import run_stream

class ProbeStep:
    def __init__(self, config, logger):
        self.config = config
        self.log = logger

    def run(self, context: dict, stop_event) -> bool:
        self.log.emit("[STEP 2/5] Probing temporary file for metadata...")
        temp_mkv = context['temp_mkv_path']

        ffprobe_cmd = ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(temp_mkv)]
        probe_output_lines = [line for line in run_stream(ffprobe_cmd, stop_event) if not line.startswith(">>>")]
        if stop_event.is_set(): return False

        field_order = None
        try:
            probe_data = json.loads("".join(probe_output_lines))
            video_stream = next((s for s in probe_data.get('streams', []) if s.get('codec_type') == 'video'), None)
            if video_stream and 'field_order' in video_stream:
                if video_stream['field_order'] in ('tt', 'tb'): field_order = 'top first'
                elif video_stream['field_order'] in ('bb', 'bt'): field_order = 'bottom first'
            if field_order:
                self.log.emit(f"  -> Detected interlaced video ({field_order}). Field order will be preserved.")
        except Exception as e:
            self.log.emit(f"!! WARNING: Could not parse ffprobe data: {e}")

        context['field_order'] = field_order
        return True
