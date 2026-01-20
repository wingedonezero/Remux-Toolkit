# remux_toolkit/tools/ffmpeg_dvd_gui/core/info_probe.py
"""
DVD title probing using FFprobe's dvdvideo demuxer.
"""
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from ..utils.paths import get_dvd_input_path
from ..utils.ffmpeg_parser import (
    probe_all_dvd_titles,
    title_info_to_dict,
    check_tool_available,
)


class DVDProbeWorker(QObject):
    """
    Worker for probing DVD titles.
    Emits probed signal with results.
    """
    # row, label, titles_total, titles_info, disc_info, error
    probed = pyqtSignal(int, object, object, object, object, str)

    def __init__(self, settings: dict):
        super().__init__()
        self.settings = settings

    def probe(self, row: int, job):
        """Probe a DVD job for title information."""
        err = ""
        label = None
        titles_total = None
        titles_info = None
        disc_info = {}

        try:
            ffprobe_path = self.settings.get("ffprobe_path", "ffprobe")

            # Check if ffprobe is available
            available, version_or_err = check_tool_available(ffprobe_path)
            if not available:
                err = f"ffprobe not available: {version_or_err}"
                self.probed.emit(row, label, titles_total, titles_info, disc_info, err)
                return

            # Get the correct DVD input path
            dvd_path = get_dvd_input_path(Path(job.source_path))

            # Probe all titles
            titles = probe_all_dvd_titles(ffprobe_path, dvd_path)

            if not titles:
                err = "No titles found on disc (check if ffmpeg has dvdvideo support)"
                self.probed.emit(row, label, titles_total, titles_info, disc_info, err)
                return

            # Convert to dict format
            titles_info = {
                t_num: title_info_to_dict(t_info)
                for t_num, t_info in titles.items()
            }
            titles_total = len(titles)

            # Use the display name as label
            label = job.child_name

            # Disc info
            disc_info = {
                "type": "DVD",
                "titles_found": titles_total,
            }

        except FileNotFoundError:
            err = "ffprobe not found (check Preferences)"
        except Exception as e:
            err = str(e)

        self.probed.emit(row, label, titles_total, titles_info, disc_info, err)
