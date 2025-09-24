# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/chapters.py
import xml.etree.ElementTree as ET
from ..utils.helpers import run_capture # Use run_capture instead of run_stream

class ChaptersStep:
    def __init__(self, config):
        self.config = config

    def run(self, context: dict, log_emitter, stop_event) -> bool:
        log_emitter("[STEP 4/5] Extracting and renaming chapters...")
        temp_mkv = context['temp_mkv_path']
        mod_chap_xml = context['out_folder'] / f"title_{context['title_num']}_chapters_mod.xml"
        context['mod_chap_xml_path'] = mod_chap_xml

        # --- THIS IS THE FIX ---
        # Use run_capture to get the entire XML output at once, which is required for mkvextract.
        mkvextract_cmd = ["mkvextract", str(temp_mkv), "chapters", "-"]
        rc, out_ext = run_capture(mkvextract_cmd)
        log_emitter(f">>> Executing: mkvextract ...\n{out_ext}")
        # --- END FIX ---

        if stop_event.is_set(): return False

        chapters_ok = False
        if rc == 0 and out_ext.strip():
            try:
                root = ET.fromstring(out_ext)
                for i, atom in enumerate(root.findall('.//ChapterAtom'), 1):
                    for display in atom.findall('ChapterDisplay'): atom.remove(display)
                    new_display = ET.SubElement(atom, 'ChapterDisplay')
                    ET.SubElement(new_display, 'ChapterString').text = f"Chapter {i:02d}"
                    ET.SubElement(new_display, 'ChapterLanguage').text = 'eng'

                tree = ET.ElementTree(root)
                tree.write(mod_chap_xml, encoding='UTF-8', xml_declaration=True)
                chapters_ok = True
                log_emitter("  -> Chapters processed successfully.")
            except Exception as e:
                log_emitter(f"!! ERROR: Failed to process chapter XML: {e}")
        else:
            log_emitter("  -> No chapters found or extraction failed.")

        context['chapters_ok'] = chapters_ok
        return True
