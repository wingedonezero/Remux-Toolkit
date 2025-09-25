# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/ccextract.py
from ..utils.helpers import run_stream

class CCExtractStep:
    def __init__(self, config):
        self.config = config

    @property
    def is_enabled(self):
        return self.config.get("run_ccextractor", True)

    def run(self, context: dict, log_emitter, stop_event) -> bool:
        if not self.is_enabled:
            log_emitter(f"{context.get('step_info', '[OPTIONAL]')} Skipping CCExtractor (disabled in settings).")
            context['cc_found'] = False
            return True

        step_info = context.get('step_info', '[STEP]')
        log_emitter(f"{step_info} Extracting closed captions...")

        # Find the video stream file from extracted streams
        extracted_streams = context.get('extracted_streams', [])
        video_file = None
        for stream_info in extracted_streams:
            if stream_info['type'] == 'video':
                video_file = stream_info['file']
                break

        if not video_file or not video_file.exists():
            log_emitter("  -> No video stream found for caption extraction.")
            context['cc_found'] = False
            return True

        cc_srt = context['out_folder'] / f"title_{context['title_num']}_cc.srt"
        context['cc_srt_path'] = cc_srt

        ccextractor_cmd = ["ccextractor", "-out=srt", "-o", str(cc_srt), str(video_file), "-quiet"]
        for line in run_stream(ccextractor_cmd, stop_event): log_emitter(line)
        if stop_event.is_set(): return False

        cc_found = cc_srt.exists() and cc_srt.stat().st_size > 10
        if cc_found:
            log_emitter("  -> Closed captions extracted successfully.")
        else:
            log_emitter("  -> No EIA-608 captions found.")

        context['cc_found'] = cc_found
        return True
