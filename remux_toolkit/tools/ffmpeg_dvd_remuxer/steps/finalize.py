# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/finalize.py
from ..utils.helpers import run_stream

class FinalizeStep:
    def __init__(self, config, logger):
        self.config = config
        self.log = logger

    def run(self, context: dict, stop_event) -> bool:
        self.log.emit("[STEP 5/5] Building final MKV file with mkvmerge...")
        final_mkv = context['out_folder'] / f"title_{context['title_num']}.mkv"
        temp_mkv = context['temp_mkv_path']

        mkvmerge_cmd = ["mkvmerge", "-o", str(final_mkv), "--no-global-tags"]

        if context.get('field_order'):
             order_num = "1" if context['field_order'] == "top first" else "2"
             mkvmerge_cmd.extend(["--field-order", f"0:{order_num}"])

        mkvmerge_cmd.extend(["--no-chapters", str(temp_mkv)])

        if context.get('cc_found', False):
            cc_srt = context['cc_srt_path']
            mkvmerge_cmd.extend(["--language", "0:eng", "--track-name", "0:Closed Captions (EIA-608)", str(cc_srt)])

        if context.get('chapters_ok', False):
            mod_chap_xml = context['mod_chap_xml_path']
            mkvmerge_cmd.extend(["--chapters", str(mod_chap_xml)])

        for line in run_stream(mkvmerge_cmd, stop_event): self.log.emit(line)
        if stop_event.is_set(): return False

        if not final_mkv.exists() or final_mkv.stat().st_size < 1024:
             self.log.emit("!! ERROR: mkvmerge failed to create the final file.")
             return False

        self.log.emit(f"🎉 Successfully created: {final_mkv.name}")
        return True
