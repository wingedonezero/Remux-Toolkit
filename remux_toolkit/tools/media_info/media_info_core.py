# remux_toolkit/tools/media_info/media_info_core.py

import subprocess
import json
from pathlib import Path
from PyQt6.QtCore import QObject, pyqtSignal, QRunnable
import re

class WorkerSignals(QObject):
    """Defines the signals available from a running worker thread."""
    finished = pyqtSignal(str, dict)
    error = pyqtSignal(str, str)

class InfoWorker(QRunnable):
    """Worker thread for running ffprobe."""
    def __init__(self, file_path: str):
        super().__init__()
        self.file_path = file_path
        self.signals = WorkerSignals()

    def run(self):
        """Execute the ffprobe command to get all available data."""
        try:
            # This enhanced command fetches everything: format, streams, all tags, and side data.
            command = [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                "-show_entries", "stream_tags=*:format_tags=*:stream_side_data=*",
                self.file_path
            ]
            result = subprocess.run(command, capture_output=True, text=True, check=True, encoding='utf-8')
            data = json.loads(result.stdout)
            processed_data = self._process_data(data)
            self.signals.finished.emit(self.file_path, processed_data)
        except FileNotFoundError:
            self.signals.error.emit(self.file_path, "ffprobe not found. Please ensure FFmpeg is installed and in your system's PATH.")
        except subprocess.CalledProcessError as e:
            self.signals.error.emit(self.file_path, f"ffprobe failed: {e.stderr}")
        except json.JSONDecodeError:
            self.signals.error.emit(self.file_path, "Failed to parse ffprobe output.")
        except Exception as e:
            self.signals.error.emit(self.file_path, f"An unexpected error occurred: {e}")

    def _process_data(self, data: dict) -> dict:
        """Converts raw ffprobe JSON to a more display-friendly and comprehensive format."""
        processed = {}
        if 'format' in data:
            # Rename 'format' to 'Container' for clarity
            processed['Container'] = data['format']

        if 'streams' in data:
            for stream in data.get('streams', []):
                # Check for Dolby Vision side data
                for side_data in stream.get('side_data_list', []):
                    if side_data.get('side_data_type') == "DOVI configuration record":
                        if 'tags' not in stream: stream['tags'] = {}
                        stream['tags']['DOVI'] = "Yes (Found DOVI RPU)"
                        # You can add more specific DOVI parsing here if needed

                # Check for HDR mastering display metadata (HDR10 / HLG)
                for side_data in stream.get('side_data_list', []):
                    if side_data.get('side_data_type') == "Mastering display metadata":
                        if 'tags' not in stream: stream['tags'] = {}
                        stream['tags']['HDR_Mastering_Display'] = self._format_hdr_metadata(side_data)

                # Check for HDR10+ metadata
                for side_data in stream.get('side_data_list', []):
                    if "HDR10+" in side_data.get('side_data_type', ''):
                         if 'tags' not in stream: stream['tags'] = {}
                         stream['tags']['HDR10+'] = "Yes"


            processed['Streams'] = data['streams']

        return processed

    def _format_hdr_metadata(self, side_data: dict) -> str:
        """Formats the HDR mastering display metadata into a readable string."""
        parts = []
        # Display primaries (e.g., R(0.6800, 0.3200))
        for color in ['red', 'green', 'blue']:
            x_key, y_key = f"{color}_x", f"{color}_y"
            if x_key in side_data and y_key in side_data:
                x = side_data[x_key].split('/')[0]
                y = side_data[y_key].split('/')[0]
                parts.append(f"{color.capitalize()[0]}({float(x)/50000:.4f}, {float(y)/50000:.4f})")
        # White point
        if 'white_point_x' in side_data and 'white_point_y' in side_data:
            x = side_data['white_point_x'].split('/')[0]
            y = side_data['white_point_y'].split('/')[0]
            parts.append(f"WP({float(x)/50000:.4f}, {float(y)/50000:.4f})")
        # Luminance
        if 'max_luminance' in side_data:
            lum = side_data['max_luminance'].split('/')[0]
            parts.append(f"MaxL({int(lum)/10000:.0f})")
        if 'min_luminance' in side_data:
            lum = side_data['min_luminance'].split('/')[0]
            parts.append(f"MinL({float(lum)/10000:.4f})")

        return " ".join(parts)
