# remux_toolkit/tools/makemkvcon_gui/core/info_probe.py
import subprocess
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
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
    """
    Runs `makemkvcon info` off the GUI thread.

    `probe()` is a slot, so it must be reached through a queued signal (see the
    widget's `probe_requested`) rather than called directly - a direct call
    would run the scan on the GUI thread and freeze the window.
    """
    probed = pyqtSignal(int, object, object, object, object, str)  # probe_id, label, titles_total, titles_info, disc_info, err
    probe_started = pyqtSignal(int)  # probe_id

    def __init__(self, settings: dict):
        super().__init__()
        self.settings = settings
        self._cancelled = False
        self._proc = None

    def cancel(self):
        """
        Abandon probing (called from the GUI thread, e.g. on tab close).

        Kills the scan in flight and makes every queued probe return at once,
        so the probe thread can be joined promptly instead of blocking on a
        disc that is taking its time.
        """
        self._cancelled = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def _run(self, cmd, timeout: int):
        """Run a probe command via Popen so cancel() can terminate it."""
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        self._proc = proc
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
        finally:
            self._proc = None
        return out, proc.returncode

    @pyqtSlot(int, object)
    def probe(self, probe_id: int, job):
        err = ""
        label = None
        tcount = None
        details = None
        disc_info = None

        if self._cancelled:
            self.probed.emit(probe_id, None, None, None, None, "Probe cancelled")
            return

        self.probe_started.emit(probe_id)

        try:
            cmd = [self.settings["makemkvcon_path"], "-r", "info", job.source_spec]

            # Add minlength if specified (affects which titles are reported)
            if minlen := self.settings.get("minlength"):
                cmd.extend(["--minlength", str(minlen)])

            out, returncode = self._run(cmd, timeout=180)

            if self._cancelled:
                err = "Probe cancelled"
            elif returncode != 0:
                err = f"makemkvcon info {parse_exit_code_message(returncode)}"
            else:
                # Parse all information using enhanced parser
                label = parse_label_from_info(out)
                tcount = count_titles_from_info(out)
                details = parse_info_details(out)
                disc_info = parse_disc_info(out)

                # === Parse protection and filesystem info ===
                disc_info["protection"] = parse_disc_protection_flags(out)
                disc_info["filesystem"] = parse_disc_filesystem_info(out)

        except FileNotFoundError:
            err = "makemkvcon not found (check Preferences)."
        except subprocess.TimeoutExpired:
            err = "Probe timed out (disc may be unreadable)"
        except Exception as e:
            err = str(e)

        self.probed.emit(probe_id, label, tcount, details, disc_info, err)
