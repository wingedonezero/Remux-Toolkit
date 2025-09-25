# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/chapters.py
import xml.etree.ElementTree as ET
from ..utils.helpers import run_capture # Use run_capture instead of run_stream

class ChaptersStep:
    def __init__(self, config):
        self.config = config

    def run(self, context: dict, log_emitter, stop_event) -> bool:
        step_info = context.get('step_info', '[STEP]')
        log_emitter(f"{step_info} Processing chapter information...")

        # Get chapter data from metadata
        metadata = context.get('title_metadata', {})
        chapters_data = metadata.get('chapters', [])

        if not chapters_data:
            log_emitter("  -> No chapters found in metadata.")
            context['chapters_ok'] = False
            return True

        # Create XML chapter file for mkvmerge
        mod_chap_xml = context['out_folder'] / f"title_{context['title_num']}_chapters_mod.xml"
        context['mod_chap_xml_path'] = mod_chap_xml

        try:
            # Build chapter XML structure
            root = ET.Element("Chapters")
            edition = ET.SubElement(root, "EditionEntry")
            ET.SubElement(edition, "EditionFlagHidden").text = "0"
            ET.SubElement(edition, "EditionFlagDefault").text = "0"

            for chap in chapters_data:
                atom = ET.SubElement(edition, "ChapterAtom")

                # Convert times to HH:MM:SS.nnnnnnnnn format
                start_seconds = float(chap['start_time'])
                end_seconds = float(chap['end_time'])

                def format_time(seconds):
                    hours = int(seconds // 3600)
                    minutes = int((seconds % 3600) // 60)
                    secs = seconds % 60
                    # Format with 9 decimal places for nanosecond precision
                    return f"{hours:02d}:{minutes:02d}:{secs:012.9f}"

                ET.SubElement(atom, "ChapterTimeStart").text = format_time(start_seconds)
                ET.SubElement(atom, "ChapterTimeEnd").text = format_time(end_seconds)
                ET.SubElement(atom, "ChapterFlagHidden").text = "0"
                ET.SubElement(atom, "ChapterFlagEnabled").text = "1"

                display = ET.SubElement(atom, "ChapterDisplay")
                ET.SubElement(display, "ChapterString").text = f"Chapter {chap['number']:02d}"
                ET.SubElement(display, "ChapterLanguage").text = "eng"

            # Write XML file
            tree = ET.ElementTree(root)
            ET.indent(tree, space="  ")  # Format nicely
            tree.write(mod_chap_xml, encoding='UTF-8', xml_declaration=True)

            log_emitter(f"  -> Processed {len(chapters_data)} chapters successfully.")
            context['chapters_ok'] = True

        except Exception as e:
            log_emitter(f"!! ERROR: Failed to create chapter XML: {e}")
            context['chapters_ok'] = False

        return True
