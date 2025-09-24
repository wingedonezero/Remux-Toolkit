# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/ccextract.py
from ..utils.helpers import run_stream

class CCExtractStep:
    def __init__(self, config, logger):
        self.config = config
        self.log = logger

    @property
    def is_enabled(self):
        return self.config.get("run_ccextractor", True)

    def run(self, context: dict, stop_event) -> bool:
        if not self.is_enabled:
            self.log.emit("[STEP 3/5] Skipping CCExtractor (disabled in settings).")
            context['cc_found'] = False
            return True

        self.log.emit("[STEP 3/5] Extracting closed captions...")
        temp_mkv = context['temp_mkv_path']
        cc_srt = context['out_folder'] / f"title_{context['title_num']}_cc.srt"
        context['cc_srt_path'] = cc_srt

        ccextractor_cmd = ["ccextractor", "-out=srt", "-o", str(cc_srt), str(temp_mkv), "-quiet"]
        for line in run_stream(ccextractor_cmd, stop_event): self.log.emit(line)
        if stop_event.is_set(): return False

        cc_found = cc_srt.exists() and cc_srt.stat().st_size > 10
        if cc_found:
            self.log.emit("  -> Closed captions extracted successfully.")
        else:
            self.log.emit("  -> No EIA-608 captions found.")

        context['cc_found'] = cc_found
        return True
