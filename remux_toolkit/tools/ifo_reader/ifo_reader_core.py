# remux_toolkit/tools/ifo_reader/ifo_reader_core.py
import subprocess
import shutil
import xml.etree.ElementTree as ET
import json
import re
from pathlib import Path
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

class Worker(QObject):
    """A worker to parse DVD structures using lsdvd."""
    finished = pyqtSignal(dict, str)

    def _check_deps(self):
        if not shutil.which("lsdvd"):
            raise FileNotFoundError("The 'lsdvd' command was not found. Please install it to use this tool.")

    @pyqtSlot(str)
    def parse_ifo(self, path_str: str):
        """Public slot to start the parsing process."""
        try:
            self._check_deps()
            dvd_root = self._find_dvd_root(Path(path_str))
            if not dvd_root:
                raise ValueError("Please select a VIDEO_TS folder, its parent, or an ISO file.")

            # Attempt 1: JSON
            try:
                raw = self._run_lsdvd(["lsdvd", "-j", "-q", str(dvd_root)])
                data = self._parse_lsdvd_json(raw)
                data['parsing_mode'] = 'JSON'
                self.finished.emit(data, None)
                return
            except Exception: pass

            # Attempt 2: XML
            try:
                raw = self._run_lsdvd(["lsdvd", "-x", "-q", str(dvd_root)])
                data = self._parse_lsdvd_xml(raw)
                data['parsing_mode'] = 'XML'
                self.finished.emit(data, None)
                return
            except Exception: pass

            # Attempt 3: Text Fallback
            raw = self._run_lsdvd(["lsdvd", str(dvd_root)])
            data = self._parse_lsdvd_text(raw)
            if data:
                data['parsing_mode'] = 'Text Fallback'
                self.finished.emit(data, None)
                return

            raise ValueError(f"lsdvd did not return any usable output. Raw output:\n\n{raw}")

        except Exception as e:
            self.finished.emit({}, str(e))

    def _find_dvd_root(self, path: Path):
        if path.is_dir():
            if (path / "VIDEO_TS").is_dir(): return path
            if path.name.upper() == "VIDEO_TS": return path.parent
        elif path.is_file() and path.suffix.lower() in ['.iso', '.img']:
            return path
        return None

    def _run_lsdvd(self, cmd):
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30, encoding='utf-8', errors='ignore')
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd, stderr=result.stderr.strip())
        output = result.stdout.strip()
        if not output:
             raise ValueError("lsdvd produced no output. The disc may be copy-protected or the path is invalid.")
        return output

    def _parse_lsdvd_json(self, json_string: str):
        data = json.loads(json_string)
        if 'track' in data: data['titles'] = data.pop('track')
        return data

    def _parse_lsdvd_xml(self, xml_string: str):
        root = ET.fromstring(xml_string)
        disc_info = {"device": root.get("device"), "titles": []}
        for title_elem in root.findall("track"):
            title_data = {"title_number": title_elem.get("ix"), "properties": {}, "streams": {}}
            for prop in title_elem:
                if prop.tag in ['audio', 'subp']:
                    stream_type = "audio_tracks" if prop.tag == 'audio' else "subtitle_tracks"
                    if stream_type not in title_data["streams"]: title_data["streams"][stream_type] = []
                    stream_info = {k: v for k, v in prop.attrib.items()}
                    stream_info['content'] = prop.text
                    title_data["streams"][stream_type].append(stream_info)
                else:
                    title_data["properties"][prop.tag] = prop.text or {k: v for k, v in prop.attrib.items()}
            disc_info["titles"].append(title_data)
        return disc_info

    def _parse_lsdvd_text(self, text_output: str):
        """Parses the default human-readable output from lsdvd as a last resort."""
        disc_info = {"device": "N/A", "titles": []}
        title_re = re.compile(
            r"Title: (?P<title_number>\d+), "
            r"Length: (?P<length>[\d:.]+)\s "
            r"Chapters: (?P<chapters>\d+), "
            r"Cells: (?P<cells>\d+), "
            r"Audio streams: (?P<audio_streams>\d+), "
            r"Subpictures: (?P<subpictures>\d+)"
        )
        for line in text_output.splitlines():
            match = title_re.search(line)
            if match:
                data = match.groupdict()
                title_data = {
                    "title_number": data["title_number"],
                    "properties": {"length": data["length"], "chapters": data["chapters"]},
                    "streams": {"audio_stream_count": data["audio_streams"], "subtitle_stream_count": data["subpictures"]}
                }
                disc_info["titles"].append(title_data)
        return disc_info if disc_info["titles"] else None
