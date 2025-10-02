# remux_toolkit/tools/makemkvcon_gui/core/ripper.py
# ADD these imports and update the MakeMKVWorker class

import os
import re
import shlex
import subprocess
import select
import time
from pathlib import Path
from PyQt6.QtCore import QObject, pyqtSignal

from ..utils.paths import DiscInfo, create_output_structure
from ..models.job import Job
from ..utils.makemkv_parser import parse_message_severity

_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')

def _unescape(s: str) -> str:
    """Unescape quoted strings from makemkvcon output"""
    return s.replace(r'\"', '"').replace(r"\\", "\\")

def _msg_to_human(line: str) -> tuple[str, str] | None:
    """
    Extract human-readable message from MSG: line with severity
    Returns: (severity, message) or None
    """
    if not line.startswith("MSG:"):
        return None
    severity, code, message = parse_message_severity(line)
    return (severity, message) if message else None

class SpeedTracker:
    """Track ripping speed and calculate ETA"""
    def __init__(self):
        self.start_time = None
        self.last_update_time = None
        self.bytes_processed = 0
        self.total_bytes = 0
        self.speed_samples = []  # Last N speed samples for smoothing
        self.max_samples = 10

    def start(self, total_bytes: int = 0):
        """Start tracking"""
        self.start_time = time.time()
        self.last_update_time = self.start_time
        self.total_bytes = total_bytes
        self.bytes_processed = 0
        self.speed_samples.clear()

    def update(self, current_progress: int, max_progress: int):
        """Update with current progress values"""
        now = time.time()
        if self.last_update_time is None:
            self.last_update_time = now
            return

        time_delta = now - self.last_update_time
        if time_delta < 0.5:  # Update at most every 0.5 seconds
            return

        # Calculate bytes processed (estimate from progress)
        if max_progress > 0 and self.total_bytes > 0:
            current_bytes = int((current_progress / max_progress) * self.total_bytes)
            bytes_delta = current_bytes - self.bytes_processed

            if bytes_delta > 0 and time_delta > 0:
                speed = bytes_delta / time_delta  # bytes per second
                self.speed_samples.append(speed)
                if len(self.speed_samples) > self.max_samples:
                    self.speed_samples.pop(0)

                self.bytes_processed = current_bytes

        self.last_update_time = now

    def get_average_speed(self) -> float:
        """Get average speed in bytes/second"""
        if not self.speed_samples:
            return 0.0
        return sum(self.speed_samples) / len(self.speed_samples)

    def get_speed_string(self) -> str:
        """Get formatted speed string (e.g., '5.2 MB/s')"""
        speed = self.get_average_speed()
        if speed == 0:
            return "-- MB/s"

        # Convert to MB/s
        speed_mb = speed / (1024 * 1024)
        return f"{speed_mb:.1f} MB/s"

    def get_elapsed_string(self) -> str:
        """Get formatted elapsed time string"""
        if not self.start_time:
            return "00:00:00"

        elapsed = int(time.time() - self.start_time)
        hours = elapsed // 3600
        minutes = (elapsed % 3600) // 60
        seconds = elapsed % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def get_eta_string(self) -> str:
        """Get formatted ETA string based on current speed"""
        speed = self.get_average_speed()
        if speed == 0 or self.total_bytes == 0:
            return "--:--:--"

        bytes_remaining = self.total_bytes - self.bytes_processed
        if bytes_remaining <= 0:
            return "00:00:00"

        eta_seconds = int(bytes_remaining / speed)
        hours = eta_seconds // 3600
        minutes = (eta_seconds % 3600) // 60
        seconds = eta_seconds % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

class ProgressTracker:
    """
    Track progress across multiple titles using PRGC (current/total) messages
    for accurate overall progress calculation
    """
    def __init__(self, total_titles: int):
        self.total_titles = total_titles
        self.current_title_index = 0
        self.title_current = 0
        self.title_total = 1
        self.global_current = 0
        self.global_total = 1

    def update_from_prgc(self, current: int, total: int):
        """Update from PRGC:current,total,max message"""
        self.global_current = current
        self.global_total = total if total > 0 else 1

    def update_from_prgv(self, current: int, total: int):
        """Update from PRGV:current,y,total message (title-specific)"""
        self.title_current = current
        self.title_total = total if total > 0 else 1

    def get_overall_percent(self) -> int:
        """
        Calculate overall progress percentage
        Uses global progress if available (PRGC), falls back to title-based estimation
        """
        if self.global_total > 1 and self.global_current > 0:
            # Use actual global progress from PRGC
            return min(100, int(100 * self.global_current / self.global_total))
        else:
            # Fallback: estimate based on title completion
            if self.total_titles == 0:
                return 0
            title_weight = 100.0 / self.total_titles
            completed_progress = self.current_title_index * title_weight
            current_title_pct = 100 * self.title_current / self.title_total if self.title_total > 0 else 0
            current_progress = (current_title_pct / 100.0) * title_weight
            return min(100, int(completed_progress + current_progress))

    def advance_title(self):
        """Move to next title"""
        self.current_title_index += 1
        self.title_current = 0
        self.title_total = 1

class MakeMKVWorker(QObject):
    progress = pyqtSignal(int, int)
    status_text = pyqtSignal(int, str)
    line_out = pyqtSignal(int, str, str)  # row, text, severity
    job_done = pyqtSignal(int, bool, str)  # row, success, error_message

    def __init__(self, settings: dict):
        super().__init__()
        self.settings = settings
        self.jobs_to_run = []
        self._stop = False

    def stop(self):
        self._stop = True

    def set_jobs(self, jobs_to_run):
        self.jobs_to_run = jobs_to_run

    def run(self):
        for job_data in self.jobs_to_run:
            if len(job_data) == 3:
                original_row, job, captured_selection = job_data
            else:
                original_row, job = job_data
                captured_selection = job.selected_titles

            if self._stop:
                self.status_text.emit(original_row, "Stopped")
                self.job_done.emit(original_row, False, "Stopped by user")
                break

            self.status_text.emit(original_row, "Starting…")
            self.progress.emit(original_row, 0)
            overall_success = True
            error_message = ""

            try:
                output_root = Path(self.settings["output_root"])
                output_root.mkdir(parents=True, exist_ok=True)

                # Use your existing folder structure logic
                if (hasattr(job, "relative_path") and job.relative_path and
                    hasattr(job, "drop_root") and job.drop_root):
                    disc_info = DiscInfo(
                        disc_path=Path(job.source_path),
                        display_name=job.child_name,
                        relative_path=job.relative_path,
                        drop_root=job.drop_root,
                    )
                    dest_dir = create_output_structure(
                        disc_info,
                        output_root,
                        getattr(job, "preserve_structure", True)
                    )
                else:
                    # Fallback
                    from ..utils.paths import safe_name, unique_dir
                    base_name = safe_name(job.label_hint or job.child_name)
                    dest_dir = unique_dir(output_root / base_name)
                    dest_dir.mkdir(parents=True, exist_ok=True)

                log_filename = f"{dest_dir.name}_makemkv.log"
                pretty_log_path = dest_dir / log_filename
                job.out_dir, job.log_path = dest_dir, pretty_log_path

                # Extract settings
                mk = self.settings["makemkvcon_path"]
                show_p = bool(self.settings.get("show_percent", True))
                human = bool(self.settings.get("human_log", True))
                keep_raw = bool(self.settings.get("keep_structured_messages", False))
                debugf = bool(self.settings.get("enable_debugfile", False))

                if isinstance(captured_selection, set) and not captured_selection:
                    self.line_out.emit(original_row, "No titles selected - skipping job", "info")
                    self.job_done.emit(original_row, True, "")
                    continue

                titles_to_rip = (sorted(list(captured_selection))
                               if isinstance(captured_selection, set)
                               else ["all"])

                total_titles_to_rip = (len(titles_to_rip)
                                     if "all" not in titles_to_rip
                                     else (job.titles_total or 1))

                self.line_out.emit(original_row,
                                 f"Processing {total_titles_to_rip} title(s)", "info")

                # Create progress tracker
                progress_tracker = ProgressTracker(total_titles_to_rip)

                # Create speed tracker with estimated total size
                speed_tracker = SpeedTracker()
                estimated_total_bytes = 0
                if job.titles_info:
                    for title_id in titles_to_rip:
                        if title_id != "all" and title_id in job.titles_info:
                            from ..utils.makemkv_parser import calculate_title_size_bytes
                            estimated_total_bytes += calculate_title_size_bytes(job.titles_info[title_id])
                speed_tracker.start(estimated_total_bytes)

                for title_idx, title_id in enumerate(titles_to_rip):
                    if self._stop:
                        self.status_text.emit(original_row, "Stopped")
                        overall_success = False
                        error_message = "Stopped by user"
                        break

                    current_title_num = title_idx + 1
                    progress_tracker.current_title_index = title_idx

                    raw_tmp_path = dest_dir / f".mkvq_messages_title_{title_id}.tmp"

                    # Build command
                    cmd = [mk, "-r"]
                    if show_p:
                        cmd.append("--progress=-stdout")
                    cmd.extend(["--messages", str(raw_tmp_path)])
                    if debugf:
                        debug_log = dest_dir / f"{dest_dir.name}_title_{title_id}_debug.log"
                        cmd.extend(["--debug", str(debug_log)])
                    if prof := self.settings.get("profile_path", "").strip():
                        cmd.extend(["--profile", prof])
                    if extra := self.settings.get("extra_args", "").strip():
                        cmd.extend(shlex.split(extra))

                    cmd.extend(["mkv", job.source_spec, str(title_id), str(dest_dir)])

                    title_cmdline = " ".join(shlex.quote(c) for c in cmd)
                    self.line_out.emit(original_row,
                                     f"Title {current_title_num}/{total_titles_to_rip}: $ {title_cmdline}", "info")

                    raw_tmp_path.touch(exist_ok=True)

                    with (
                        open(pretty_log_path, "a", encoding="utf-8") as lf,
                        subprocess.Popen(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            bufsize=1,
                            universal_newlines=True
                        ) as proc,
                        open(raw_tmp_path, "r", encoding="utf-8", errors="replace") as tail,
                    ):
                        lf.write(f"\n=== Title {title_id} ({current_title_num}/{total_titles_to_rip}) ===\n")
                        lf.flush()

                        tail.seek(0, os.SEEK_END)
                        last_tail_pos = tail.tell()

                        def tail_messages():
                            """Read new messages from the message file"""
                            nonlocal last_tail_pos
                            tail.seek(last_tail_pos)
                            if not (chunk := tail.read()):
                                return
                            last_tail_pos = tail.tell()
                            for raw in chunk.splitlines():
                                if not raw or raw.startswith("PRG"):
                                    continue
                                if result := (_msg_to_human(raw) if human else (None, raw)):
                                    severity, out = result if human else ("info", result[1])
                                    self.line_out.emit(original_row, f"Title {title_id}: {out}", severity)
                                    try:
                                        lf.write(f"Title {title_id}: {out}\n")
                                        lf.flush()
                                    except:
                                        pass

                        self.status_text.emit(original_row,
                                            f"Title {current_title_num}/{total_titles_to_rip} (#{title_id})")

                        # Process output
                        while True:
                            if self._stop:
                                proc.terminate()
                                break

                            tail_messages()

                            rl, _, _ = select.select([proc.stdout], [], [], 0.1)
                            if rl and (line := proc.stdout.readline()):
                                line = line.strip()

                                # Parse PRGV: title-specific progress
                                if mv := re.match(r"^PRGV:(\d+),(\d+),(\d+)\s*$", line):
                                    x, y, z = int(mv.group(1)), int(mv.group(2)), int(mv.group(3))
                                    z = z or 65536
                                    progress_tracker.update_from_prgv(x, z)
                                    speed_tracker.update(x, z)

                                    overall_pct = progress_tracker.get_overall_percent()
                                    title_pct = int(100 * x / z) if z > 0 else 0

                                    self.progress.emit(original_row, overall_pct)

                                    # Enhanced status with speed and ETA
                                    speed_str = speed_tracker.get_speed_string()
                                    elapsed_str = speed_tracker.get_elapsed_string()
                                    eta_str = speed_tracker.get_eta_string()

                                    status = f"Title {current_title_num}/{total_titles_to_rip} (#{title_id}) • {title_pct}% • {speed_str} • {elapsed_str} / {eta_str}"
                                    self.status_text.emit(original_row, status)

                                # Parse PRGC: global progress (more accurate)
                                elif mc := re.match(r"^PRGC:(\d+),(\d+),(\d+)\s*$", line):
                                    current, total, max_val = int(mc.group(1)), int(mc.group(2)), int(mc.group(3))
                                    progress_tracker.update_from_prgc(current, total)
                                    speed_tracker.update(current, total)
                                    overall_pct = progress_tracker.get_overall_percent()
                                    self.progress.emit(original_row, overall_pct)

                                # Parse PRGT: progress title text
                                elif mt := re.match(r'^PRGT:(\d+),\d+,\d+,"([^"]*)"', line):
                                    title_text = _unescape(mt.group(2))
                                    if title_text:
                                        self.line_out.emit(original_row, f"Title {title_id}: {title_text}", "info")

                                # Other output
                                elif line and not line.startswith("PRGV") and not line.startswith("PRGC"):
                                    # Try to determine severity from line content
                                    severity = "info"
                                    if "error" in line.lower() or "fail" in line.lower():
                                        severity = "error"
                                    elif "warning" in line.lower():
                                        severity = "warning"
                                    self.line_out.emit(original_row, f"Title {title_id}: {line}", severity)

                            if proc.poll() is not None:
                                tail_messages()
                                break

                        # Check exit code and provide meaningful error
                        returncode = proc.wait()
                        title_success = returncode == 0

                        if not title_success:
                            overall_success = False
                            if returncode == 1:
                                err_msg = f"Title {title_id} failed (check log for details)"
                            elif returncode == 2:
                                err_msg = f"Title {title_id} failed (invalid arguments)"
                            else:
                                err_msg = f"Title {title_id} failed (exit code {returncode})"

                            error_message = err_msg if not error_message else f"{error_message}; {err_msg}"
                            self.line_out.emit(original_row, f"ERROR: {err_msg}", "error")

                    # Clean up or keep structured message file
                    try:
                        if raw_tmp_path.exists():
                            if keep_raw:
                                final_raw = dest_dir / f"{pretty_log_path.stem}_title_{title_id}.raw.txt"
                                raw_tmp_path.rename(final_raw)
                            else:
                                raw_tmp_path.unlink()
                    except Exception:
                        pass

                    # Advance progress tracker to next title
                    progress_tracker.advance_title()

            except FileNotFoundError:
                error_message = "makemkvcon not found. Check path in Preferences."
                self.line_out.emit(original_row, f"ERROR: {error_message}", "error")
                overall_success = False
            except subprocess.TimeoutExpired:
                error_message = "Operation timed out"
                self.line_out.emit(original_row, f"ERROR: {error_message}", "error")
                overall_success = False
            except Exception as e:
                error_message = str(e)
                self.line_out.emit(original_row, f"CRITICAL ERROR: {error_message}", "error")
                overall_success = False

            self.job_done.emit(original_row, overall_success, error_message)
