# remux_toolkit/tools/makemkvcon_gui/core/info_probe.py
import subprocess
from PyQt6.QtCore import QObject, pyqtSignal
from ..utils.makemkv_parser import (
    parse_label_from_info,
    count_titles_from_info,
    parse_info_details,
    parse_disc_info,
    parse_disc_protection_flags,
    parse_disc_filesystem_info,
    parse_exit_code_message
)

class InfoProbeWorker(QObject):
    probed = pyqtSignal(int, object, object, object, object, str)  # row, label, titles_total, titles_info, disc_info, err

    def __init__(self, settings: dict):
        super().__init__()
        self.settings = settings

    def probe(self, row: int, job):
        err = ""
        label = None
        tcount = None
        details = None
        disc_info = None

        try:
            cmd = [self.settings["makemkvcon_path"], "-r", "info", job.source_spec]

            # Add minlength if specified (affects which titles are reported)
            if minlen := self.settings.get("minlength"):
                cmd.extend(["--minlength", str(minlen)])

            out = subprocess.check_output(
                cmd,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=180
            )

            # Parse all information using enhanced parser
            label = parse_label_from_info(out)
            tcount = count_titles_from_info(out)
            details = parse_info_details(out)
            disc_info = parse_disc_info(out)

            # === NEW: Parse protection and filesystem info ===
            disc_info["protection"] = parse_disc_protection_flags(out)
            disc_info["filesystem"] = parse_disc_filesystem_info(out)

        except FileNotFoundError:
            err = "makemkvcon not found (check Preferences)."
        except subprocess.CalledProcessError as e:
            err = f"makemkvcon info {parse_exit_code_message(e.returncode)}"
        except subprocess.TimeoutExpired:
            err = "Probe timed out (disc may be unreadable)"
        except Exception as e:
            err = str(e)

        self.probed.emit(row, label, tcount, details, disc_info, err)
