"""
Core logic for MKV Combiner Tool
"""

import os
import subprocess
import shlex
import json
from pathlib import Path
from PyQt6 import QtCore


def validate_mkv_file(file_path):
    """
    Validates if a file exists and is an MKV file.
    Returns: (is_valid, error_message)
    """
    if not file_path:
        return False, "Empty file path"

    path = Path(file_path)

    if not path.exists():
        return False, f"File does not exist: {file_path}"

    if not path.is_file():
        return False, f"Not a file: {file_path}"

    if path.suffix.lower() != '.mkv':
        return False, f"Not an MKV file: {file_path}"

    return True, ""


def get_mkv_info(file_path):
    """
    Gets MKV file information using mkvmerge -J.
    Returns: (info_dict, error_message)
    """
    try:
        result = subprocess.run(
            ['mkvmerge', '-J', file_path],
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=30
        )

        if result.returncode != 0:
            return None, f"mkvmerge error: {result.stderr}"

        info = json.loads(result.stdout)
        return info, ""

    except subprocess.TimeoutExpired:
        return None, "mkvmerge timed out"
    except json.JSONDecodeError as e:
        return None, f"Failed to parse mkvmerge output: {e}"
    except FileNotFoundError:
        return None, "mkvmerge not found. Please ensure MKVToolNix is installed."
    except Exception as e:
        return None, f"Error getting MKV info: {e}"


def generate_combine_command(input_files, output_path):
    """
    Generates the mkvmerge command to combine multiple MKV files.
    Format: mkvmerge -o "output.mkv" "file1.mkv" +"file2.mkv" +"file3.mkv" ...

    Returns: command string
    """
    if not input_files:
        return ""

    command_parts = ['mkvmerge', '-o', f'"{output_path}"']

    # First file without +
    command_parts.append(f'"{input_files[0]}"')

    # Subsequent files with + prefix
    for file_path in input_files[1:]:
        command_parts.append(f'+"{file_path}"')

    return ' '.join(command_parts)


class AnalysisWorker(QtCore.QThread):
    """
    Worker thread to analyze MKV files before combining.
    Validates each file and gets basic info.
    """
    result = QtCore.pyqtSignal(list, str)  # (file_info_list, error_message)
    progress = QtCore.pyqtSignal(str)  # Progress messages

    def __init__(self, file_paths):
        super().__init__()
        self.file_paths = file_paths

    def run(self):
        file_info_list = []

        for i, file_path in enumerate(self.file_paths, 1):
            self.progress.emit(f"Analyzing file {i}/{len(self.file_paths)}: {Path(file_path).name}")

            # Validate file
            is_valid, error = validate_mkv_file(file_path)
            if not is_valid:
                self.result.emit([], error)
                return

            # Get file info
            info, error = get_mkv_info(file_path)
            if error:
                self.result.emit([], f"Error analyzing {Path(file_path).name}: {error}")
                return

            file_info_list.append({
                'path': file_path,
                'info': info,
                'name': Path(file_path).name,
                'size': Path(file_path).stat().st_size
            })

        self.progress.emit(f"All {len(file_info_list)} files validated successfully")
        self.result.emit(file_info_list, "")


class CombineWorker(QtCore.QThread):
    """
    Worker thread to execute the mkvmerge combine command.
    Emits real-time output from mkvmerge.
    """
    line_ready = QtCore.pyqtSignal(str)  # Output line
    progress = QtCore.pyqtSignal(int)  # Progress percentage (0-100)
    finished_signal = QtCore.pyqtSignal(int, str)  # (return_code, error_message)

    def __init__(self, command):
        super().__init__()
        self.command = command
        self._stop_requested = False

    def run(self):
        try:
            self.line_ready.emit(f"Executing: {self.command}\n")

            # Use shlex.split to properly handle quoted paths
            cmd_list = shlex.split(self.command)

            proc = subprocess.Popen(
                cmd_list,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                bufsize=1
            )

            # Read output line by line
            for line in iter(proc.stdout.readline, ''):
                if self._stop_requested:
                    proc.terminate()
                    proc.wait(timeout=5)
                    self.finished_signal.emit(-1, "Operation cancelled by user")
                    return

                line = line.strip()
                if line:
                    self.line_ready.emit(line)

                    # Try to extract progress from mkvmerge output
                    # mkvmerge outputs progress like: "Progress: 45%"
                    if "Progress:" in line and "%" in line:
                        try:
                            percent_str = line.split("Progress:")[1].split("%")[0].strip()
                            percent = int(percent_str)
                            self.progress.emit(percent)
                        except (IndexError, ValueError):
                            pass

            proc.stdout.close()
            return_code = proc.wait()

            if return_code == 0:
                self.progress.emit(100)
                self.finished_signal.emit(return_code, "")
            else:
                self.finished_signal.emit(return_code, f"mkvmerge exited with code {return_code}")

        except FileNotFoundError:
            self.finished_signal.emit(-1, "mkvmerge not found. Please ensure MKVToolNix is installed.")
        except Exception as e:
            self.finished_signal.emit(-1, f"Error executing mkvmerge: {e}")

    def stop(self):
        """Request the worker to stop"""
        self._stop_requested = True
