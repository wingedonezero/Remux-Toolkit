# remux_toolkit/tools/makemkvcon_gui/core/info_probe.py
import subprocess
from PyQt6.QtCore import QObject, pyqtSignal
from ..utils.makemkv_parser import parse_label_from_info, count_titles_from_info, parse_info_details

class InfoProbeWorker(QObject):
    probed = pyqtSignal(int, object, object, object, str)  # row, label, titles_total, titles_info, err
    def __init__(self, settings: dict):
        super().__init__()
        self.settings = settings
    def probe(self, row: int, job):
        err = ""; label = None; tcount = None; details = None
        try:
            cmd = [self.settings["makemkvcon_path"], "-r", "info", job.source_spec]
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=180)
            label = parse_label_from_info(out)
            tcount = count_titles_from_info(out)
            details = parse_info_details(out)
        except FileNotFoundError:
            err = "makemkvcon not found (check Preferences)."
        except subprocess.CalledProcessError as e:
            err = f"makemkvcon info failed (rc={e.returncode})."
        except Exception as e:
            err = str(e)
        self.probed.emit(row, label, tcount, details, err)
