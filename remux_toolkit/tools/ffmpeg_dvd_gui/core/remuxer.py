# remux_toolkit/tools/ffmpeg_dvd_gui/core/remuxer.py
"""
FFmpeg DVD remuxer worker with chapter renaming support.
"""
import os
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from xml.etree import ElementTree as ET

from PyQt6.QtCore import QObject, pyqtSignal

from ..utils.paths import DiscInfo, create_output_structure, safe_name, get_dvd_input_path
from ..utils.ffmpeg_parser import (
    parse_ffmpeg_progress,
    duration_to_seconds,
    seconds_to_duration,
    check_tool_available,
)
from ..models.job import Job


class SpeedTracker:
    """Track remuxing speed and calculate ETA."""

    def __init__(self):
        self.start_time = None
        self.last_update_time = None
        self.current_seconds = 0
        self.total_seconds = 0
        self.speed_samples = []
        self.max_samples = 10

    def start(self, total_seconds: float = 0):
        """Start tracking."""
        self.start_time = time.time()
        self.last_update_time = self.start_time
        self.total_seconds = total_seconds
        self.current_seconds = 0
        self.speed_samples.clear()

    def update(self, current_seconds: float):
        """Update with current progress in seconds."""
        now = time.time()
        if self.last_update_time is None:
            self.last_update_time = now
            return

        time_delta = now - self.last_update_time
        if time_delta < 0.5:
            return

        seconds_delta = current_seconds - self.current_seconds
        if seconds_delta > 0 and time_delta > 0:
            speed = seconds_delta / time_delta
            self.speed_samples.append(speed)
            if len(self.speed_samples) > self.max_samples:
                self.speed_samples.pop(0)
            self.current_seconds = current_seconds

        self.last_update_time = now

    def get_average_speed(self) -> float:
        """Get average speed in content-seconds per real-second."""
        if not self.speed_samples:
            return 0.0
        return sum(self.speed_samples) / len(self.speed_samples)

    def get_speed_string(self) -> str:
        """Get formatted speed string (e.g., '2.5x')."""
        speed = self.get_average_speed()
        if speed == 0:
            return "-- x"
        return f"{speed:.1f}x"

    def get_elapsed_string(self) -> str:
        """Get formatted elapsed time string."""
        if not self.start_time:
            return "00:00:00"
        elapsed = int(time.time() - self.start_time)
        hours = elapsed // 3600
        minutes = (elapsed % 3600) // 60
        seconds = elapsed % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def get_eta_string(self) -> str:
        """Get formatted ETA string based on current speed."""
        speed = self.get_average_speed()
        if speed == 0 or self.total_seconds == 0:
            return "--:--:--"

        remaining_content = self.total_seconds - self.current_seconds
        if remaining_content <= 0:
            return "00:00:00"

        eta_seconds = int(remaining_content / speed)
        hours = eta_seconds // 3600
        minutes = (eta_seconds % 3600) // 60
        seconds = eta_seconds % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def rename_chapters_with_mkvpropedit(mkv_path: Path, mkvpropedit_path: str) -> tuple[bool, str]:
    """
    Rename chapters in an MKV file to "Chapter 1", "Chapter 2", etc.
    Uses mkvpropedit to extract chapters, modify them, and re-apply.

    Returns (success, message).
    """
    try:
        # First, extract chapters to a temp file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as tmp:
            tmp_path = tmp.name

        # Extract chapters (no -s flag = XML format, -s = simple/OGM format)
        extract_cmd = [
            "mkvextract", str(mkv_path), "chapters", tmp_path
        ]
        result = subprocess.run(extract_cmd, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            # No chapters or extraction failed - that's OK
            try:
                os.unlink(tmp_path)
            except:
                pass
            return True, "No chapters to rename"

        # Check if file has content
        if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            try:
                os.unlink(tmp_path)
            except:
                pass
            return True, "No chapters found"

        # Parse and modify the XML
        try:
            tree = ET.parse(tmp_path)
            root = tree.getroot()

            # Find all ChapterAtom elements and rename them
            chapter_num = 1
            for chapter_atom in root.iter('ChapterAtom'):
                # Find or create ChapterDisplay
                display = chapter_atom.find('ChapterDisplay')
                if display is None:
                    display = ET.SubElement(chapter_atom, 'ChapterDisplay')

                # Find or create ChapterString
                chapter_string = display.find('ChapterString')
                if chapter_string is None:
                    chapter_string = ET.SubElement(display, 'ChapterString')

                # Set the chapter name
                chapter_string.text = f"Chapter {chapter_num}"
                chapter_num += 1

            # Write back the modified XML
            tree.write(tmp_path, encoding='utf-8', xml_declaration=True)

        except ET.ParseError as e:
            try:
                os.unlink(tmp_path)
            except:
                pass
            return False, f"XML parse error: {e}"

        # Apply the modified chapters back
        apply_cmd = [
            mkvpropedit_path, str(mkv_path), "--chapters", tmp_path
        ]
        result = subprocess.run(apply_cmd, capture_output=True, text=True, timeout=60)

        # Clean up temp file
        try:
            os.unlink(tmp_path)
        except:
            pass

        if result.returncode != 0:
            return False, f"mkvpropedit failed: {result.stderr}"

        return True, f"Renamed {chapter_num - 1} chapters"

    except subprocess.TimeoutExpired:
        return False, "Chapter rename timed out"
    except FileNotFoundError as e:
        return False, f"Tool not found: {e}"
    except Exception as e:
        return False, str(e)


class FFmpegDVDWorker(QObject):
    """
    Worker for remuxing DVD titles using FFmpeg's dvdvideo demuxer.
    """
    progress = pyqtSignal(int, int)  # row, percentage
    status_text = pyqtSignal(int, str)  # row, status
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

            self.status_text.emit(original_row, "Starting...")
            self.progress.emit(original_row, 0)
            overall_success = True
            error_message = ""

            try:
                output_root = Path(self.settings["output_root"])
                output_root.mkdir(parents=True, exist_ok=True)

                # Create output structure
                if (hasattr(job, "relative_path") and job.relative_path and
                    hasattr(job, "drop_root") and job.drop_root):
                    from ..utils.paths import DiscInfo
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
                    from ..utils.paths import unique_dir
                    base_name = safe_name(job.label_hint or job.child_name)
                    dest_dir = unique_dir(output_root / base_name)
                    dest_dir.mkdir(parents=True, exist_ok=True)

                log_filename = f"{dest_dir.name}_ffmpeg_dvd.log"
                log_path = dest_dir / log_filename
                job.out_dir, job.log_path = dest_dir, log_path

                # Get tool paths
                ffmpeg_path = self.settings.get("ffmpeg_path", "ffmpeg")
                mkvpropedit_path = self.settings.get("mkvpropedit_path", "mkvpropedit")

                # Check ffmpeg availability
                ffmpeg_ok, ffmpeg_ver = check_tool_available(ffmpeg_path)
                if not ffmpeg_ok:
                    error_message = f"FFmpeg not available: {ffmpeg_ver}"
                    self.line_out.emit(original_row, f"ERROR: {error_message}", "error")
                    self.job_done.emit(original_row, False, error_message)
                    continue

                # Check mkvpropedit (only warn if chapter naming is enabled)
                chapter_naming = self.settings.get("chapter_naming", "numbered")
                mkvpropedit_ok = False
                if chapter_naming == "numbered":
                    mkvpropedit_ok, _ = check_tool_available(mkvpropedit_path)
                    if not mkvpropedit_ok:
                        self.line_out.emit(
                            original_row,
                            "WARNING: mkvpropedit not found - chapters will remain unnamed",
                            "warning"
                        )

                # Handle title selection
                if isinstance(captured_selection, set) and not captured_selection:
                    self.line_out.emit(original_row, "No titles selected - skipping job", "info")
                    self.job_done.emit(original_row, True, "")
                    continue

                titles_to_remux = (
                    sorted(list(captured_selection))
                    if isinstance(captured_selection, set)
                    else list(job.titles_info.keys()) if job.titles_info else [1]
                )

                total_titles = len(titles_to_remux)
                self.line_out.emit(original_row, f"Processing {total_titles} title(s)", "info")

                # Get DVD input path
                dvd_input = get_dvd_input_path(Path(job.source_path))

                with open(log_path, "w", encoding="utf-8") as lf:
                    lf.write(f"FFmpeg DVD Remux Log\n")
                    lf.write(f"Source: {dvd_input}\n")
                    lf.write(f"Output: {dest_dir}\n")
                    lf.write(f"=" * 60 + "\n\n")

                    for title_idx, title_num in enumerate(titles_to_remux):
                        if self._stop:
                            self.status_text.emit(original_row, "Stopped")
                            overall_success = False
                            error_message = "Stopped by user"
                            break

                        current_title_display = title_idx + 1

                        # Get title info for duration
                        title_info = (job.titles_info or {}).get(title_num, {})
                        duration_str = title_info.get("duration", "")
                        duration_secs = title_info.get("duration_seconds", 0) or duration_to_seconds(duration_str)

                        # Build output filename
                        output_name = f"{safe_name(job.label_hint or job.child_name)}_t{title_num:02d}.mkv"
                        output_path = dest_dir / output_name

                        # Build FFmpeg command
                        cmd = [ffmpeg_path, "-hide_banner", "-nostdin"]

                        # DVD input options
                        cmd.extend(["-f", "dvdvideo"])
                        cmd.extend(["-title", str(title_num)])

                        if self.settings.get("enable_preindex", True):
                            cmd.extend(["-preindex", "1"])

                        if self.settings.get("trim_padding", True):
                            cmd.extend(["-trim", "1"])

                        region = self.settings.get("default_region", 0)
                        if region > 0:
                            cmd.extend(["-region", str(region)])

                        cmd.extend(["-i", dvd_input])

                        # Output options - copy all streams
                        cmd.extend(["-map", "0"])
                        cmd.extend(["-c", "copy"])

                        # DVD cell boundaries can cause timestamp
                        # discontinuities that lead to audio packet loss:
                        # - Queue overflow when muxer buffers fill during
                        #   large PTS gaps between cells
                        # - Negative PTS from cell boundary timestamp resets
                        cmd.extend(["-max_muxing_queue_size", "9999"])
                        cmd.extend(["-avoid_negative_ts", "make_zero"])

                        # Extra args
                        if extra := self.settings.get("extra_args", "").strip():
                            cmd.extend(shlex.split(extra))

                        # Progress output
                        cmd.extend(["-progress", "pipe:1"])

                        # Overwrite output
                        cmd.extend(["-y", str(output_path)])

                        cmdline = " ".join(shlex.quote(c) for c in cmd)
                        self.line_out.emit(
                            original_row,
                            f"Title {current_title_display}/{total_titles}: $ {cmdline}",
                            "info"
                        )
                        lf.write(f"\n=== Title {title_num} ({current_title_display}/{total_titles}) ===\n")
                        lf.write(f"Command: {cmdline}\n\n")
                        lf.flush()

                        # Create speed tracker
                        speed_tracker = SpeedTracker()
                        speed_tracker.start(duration_secs)

                        self.status_text.emit(
                            original_row,
                            f"Title {current_title_display}/{total_titles} (#{title_num})"
                        )

                        # Run FFmpeg
                        try:
                            proc = subprocess.Popen(
                                cmd,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True,
                                bufsize=1,
                            )

                            # Read progress from stdout, errors from stderr
                            import select
                            while True:
                                if self._stop:
                                    proc.terminate()
                                    break

                                # Check for output
                                rlist, _, _ = select.select(
                                    [proc.stdout, proc.stderr], [], [], 0.1
                                )

                                for stream in rlist:
                                    line = stream.readline()
                                    if not line:
                                        continue

                                    line = line.strip()

                                    if stream == proc.stdout:
                                        # Parse progress
                                        if line.startswith("out_time="):
                                            time_str = line.split("=", 1)[1]
                                            current_secs = duration_to_seconds(time_str)
                                            speed_tracker.update(current_secs)

                                            # Calculate progress percentage
                                            if duration_secs > 0:
                                                title_pct = int(100 * current_secs / duration_secs)
                                                title_pct = min(100, max(0, title_pct))

                                                # Overall progress
                                                base_pct = int(100 * title_idx / total_titles)
                                                title_contrib = int(title_pct / total_titles)
                                                overall_pct = base_pct + title_contrib

                                                self.progress.emit(original_row, overall_pct)

                                                speed_str = speed_tracker.get_speed_string()
                                                elapsed_str = speed_tracker.get_elapsed_string()
                                                eta_str = speed_tracker.get_eta_string()

                                                status = f"Title {current_title_display}/{total_titles} (#{title_num}) - {title_pct}% - {speed_str} - {elapsed_str} / {eta_str}"
                                                self.status_text.emit(original_row, status)

                                    else:
                                        # stderr - log it
                                        if line:
                                            lf.write(f"{line}\n")
                                            lf.flush()
                                            # Determine severity
                                            severity = "info"
                                            if "error" in line.lower():
                                                severity = "error"
                                            elif "warning" in line.lower():
                                                severity = "warning"
                                            self.line_out.emit(
                                                original_row,
                                                f"Title {title_num}: {line}",
                                                severity
                                            )

                                if proc.poll() is not None:
                                    # Read remaining output
                                    for remaining in proc.stderr.readlines():
                                        if remaining.strip():
                                            lf.write(f"{remaining}")
                                    break

                            returncode = proc.wait()
                            title_success = returncode == 0

                            if title_success:
                                self.line_out.emit(
                                    original_row,
                                    f"Title {title_num} remuxed successfully: {output_name}",
                                    "success"
                                )
                                lf.write(f"\nTitle {title_num} completed successfully.\n")

                                # Rename chapters if enabled and mkvpropedit is available
                                if chapter_naming == "numbered" and mkvpropedit_ok and output_path.exists():
                                    self.line_out.emit(
                                        original_row,
                                        f"Renaming chapters for {output_name}...",
                                        "info"
                                    )
                                    ch_ok, ch_msg = rename_chapters_with_mkvpropedit(
                                        output_path, mkvpropedit_path
                                    )
                                    if ch_ok:
                                        self.line_out.emit(
                                            original_row,
                                            f"Chapters renamed: {ch_msg}",
                                            "success"
                                        )
                                        lf.write(f"Chapters renamed: {ch_msg}\n")
                                    else:
                                        self.line_out.emit(
                                            original_row,
                                            f"Chapter rename warning: {ch_msg}",
                                            "warning"
                                        )
                                        lf.write(f"Chapter rename warning: {ch_msg}\n")

                            else:
                                overall_success = False
                                err_msg = f"Title {title_num} failed (exit code {returncode})"
                                error_message = err_msg if not error_message else f"{error_message}; {err_msg}"
                                self.line_out.emit(original_row, f"ERROR: {err_msg}", "error")
                                lf.write(f"\nERROR: {err_msg}\n")

                        except Exception as e:
                            overall_success = False
                            err_msg = f"Title {title_num} error: {e}"
                            error_message = err_msg if not error_message else f"{error_message}; {err_msg}"
                            self.line_out.emit(original_row, f"ERROR: {err_msg}", "error")
                            lf.write(f"\nERROR: {err_msg}\n")

                        # Update progress after title completion
                        completed_pct = int(100 * (title_idx + 1) / total_titles)
                        self.progress.emit(original_row, completed_pct)

            except FileNotFoundError:
                error_message = "FFmpeg not found. Check path in Preferences."
                self.line_out.emit(original_row, f"ERROR: {error_message}", "error")
                overall_success = False
            except Exception as e:
                error_message = str(e)
                self.line_out.emit(original_row, f"CRITICAL ERROR: {error_message}", "error")
                overall_success = False

            self.job_done.emit(original_row, overall_success, error_message)
