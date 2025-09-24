# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/chapters.py
import xml.etree.ElementTree as ET
from ..utils.helpers import run_stream

class ChaptersStep:
    def __init__(self, config, logger):
        self.config = config
        self.log = logger

    def run(self, context: dict, stop_event) -> bool:
        self.log.emit("[STEP 4/5] Extracting and renaming chapters...")
        temp_mkv = context['temp_mkv_path']
        mod_chap_xml = context['out_folder'] / f"title_{context['title_num']}_chapters_mod.xml"
        context['mod_chap_xml_path'] = mod_chap_xml

        mkvextract_cmd = ["mkvextract", str(temp_mkv), "chapters", "-"]
        out_lines = [line for line in run_stream(mkvextract_cmd, stop_event) if not line.startswith(">>>")]
        if stop_event.is_set(): return False

        out_ext = "\n".join(out_lines)
        chapters_ok = False
        if out_ext.strip():
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
                self.log.emit("  -> Chapters processed successfully.")
            except Exception as e:
                self.log.emit(f"!! ERROR: Failed to process chapter XML: {e}")
        else:
            self.log.emit("  -> No chapters found in temporary file.")

        context['chapters_ok'] = chapters_ok
        return True
