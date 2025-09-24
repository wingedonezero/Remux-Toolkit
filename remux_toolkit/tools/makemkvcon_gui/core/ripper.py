# remux_toolkit/tools/makemkvcon_gui/core/ripper.py
import os
import re
import shlex
import subprocess
import select
import time
import math
from pathlib import Path
from PyQt6.QtCore import QObject, pyqtSignal

from ..utils.paths import DiscInfo, create_output_structure
from ..models.job import Job

_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')

def _unescape(s: str) -> str:
    return s.replace(r'\"', '"').replace(r"\\", "\\")

def _msg_to_human(line: str) -> str | None:
    if not line.startswith("MSG:"):
        return None
    m = list(_QUOTED.finditer(line))
    return _unescape(m[0].group(1)) if m else None

class MakeMKVWorker(QObject):
    progress = pyqtSignal(int, int)
    status_text = pyqtSignal(int, str)
    line_out = pyqtSignal(int, str)
    job_done = pyqtSignal(int, bool)

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
                self.job_done.emit(original_row, False)
                break

            self.status_text.emit(original_row, "Starting…")
            self.progress.emit(original_row, 0)
            overall_success = True

            try:
                # NOTE: Re-probing logic removed as it's now part of the initial probe.
                # The main GUI will pass all necessary info.

                output_root = Path(self.settings["output_root"])
                output_root.mkdir(parents=True, exist_ok=True)

                if (hasattr(job, "relative_path") and job.relative_path and hasattr(job, "drop_root") and job.drop_root):
                    disc_info = DiscInfo(
                        disc_path=Path(job.source_path),
                        display_name=job.child_name,
                        relative_path=job.relative_path,
                        drop_root=job.drop_root,
                    )
                    dest_dir = create_output_structure(disc_info, output_root, getattr(job, "preserve_structure", True))
                else:
                    # Fallback for safety, though should not be needed with new GUI code
                    from ..utils.paths import safe_name, unique_dir
                    base_name = safe_name(job.label_hint or job.child_name)
                    dest_dir = unique_dir(output_root / base_name)
                    dest_dir.mkdir(parents=True, exist_ok=True)

                log_filename = f"{dest_dir.name}_makemkv.log"
                pretty_log_path = dest_dir / log_filename
                job.out_dir, job.log_path = dest_dir, pretty_log_path

                mk, show_p, human, keep_raw, debugf = (
                    self.settings["makemkvcon_path"],
                    bool(self.settings.get("show_percent", True)),
                    bool(self.settings.get("human_log", True)),
                    bool(self.settings.get("keep_structured_messages", False)),
                    bool(self.settings.get("enable_debugfile", False)),
                )

                if isinstance(captured_selection, set) and not captured_selection:
                    self.line_out.emit(original_row, "No titles selected - skipping job")
                    self.job_done.emit(original_row, True)
                    continue

                titles_to_rip = sorted(list(captured_selection)) if isinstance(captured_selection, set) else ["all"]

                total_titles_to_rip = len(titles_to_rip) if "all" not in titles_to_rip else (job.titles_total or 1)
                self.line_out.emit(original_row, f"Processing {total_titles_to_rip} title(s)")

                for title_idx, title_id in enumerate(titles_to_rip):
                    if self._stop:
                        self.status_text.emit(original_row, "Stopped")
                        overall_success = False
                        break

                    current_title_num = title_idx + 1
                    raw_tmp_path = dest_dir / f".mkvq_messages_title_{title_id}.tmp"

                    cmd = [mk, "-r"]
                    if show_p: cmd.append("--progress=-stdout")
                    cmd.extend(["--messages", str(raw_tmp_path)])
                    if debugf: cmd.extend(["--debug", str(dest_dir / f"{dest_dir.name}_title_{title_id}_debug.log")])
                    if prof := self.settings.get("profile_path", "").strip(): cmd.extend(["--profile", prof])
                    if extra := self.settings.get("extra_args", "").strip(): cmd.extend(shlex.split(extra))

                    cmd.extend(["mkv", job.source_spec, str(title_id), str(dest_dir)])

                    title_cmdline = " ".join(shlex.quote(c) for c in cmd)
                    self.line_out.emit(original_row, f"Title {current_title_num}/{total_titles_to_rip}: $ {title_cmdline}")

                    raw_tmp_path.touch(exist_ok=True)
                    with (
                        open(pretty_log_path, "a", encoding="utf-8") as lf,
                        subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, universal_newlines=True) as proc,
                        open(raw_tmp_path, "r", encoding="utf-8", errors="replace") as tail,
                    ):
                        lf.write(f"\n=== Title {title_id} ({current_title_num}/{total_titles_to_rip}) ===\n"); lf.flush()
                        tail.seek(0, os.SEEK_END)
                        last_tail_pos = tail.tell()

                        def tail_messages():
                            nonlocal last_tail_pos
                            tail.seek(last_tail_pos)
                            if not (chunk := tail.read()): return
                            last_tail_pos = tail.tell()
                            for raw in chunk.splitlines():
                                if not raw or raw.startswith("PRG"): continue
                                if out := (_msg_to_human(raw) if human else raw):
                                    self.line_out.emit(original_row, f"Title {title_id}: {out}")
                                    try: lf.write(f"Title {title_id}: {out}\n"); lf.flush()
                                    except: pass

                        self.status_text.emit(original_row, f"Title {current_title_num}/{total_titles_to_rip} (#{title_id})")

                        while True:
                            if self._stop:
                                proc.terminate(); break
                            tail_messages()
                            rl, _, _ = select.select([proc.stdout], [], [], 0.1)
                            if rl and (line := proc.stdout.readline()):
                                line = line.strip()
                                if mv := re.match(r"^PRGV:(\d+),(\d+),(\d+)\s*$", line):
                                    x, z = int(mv.group(1)), int(mv.group(3)) or 65536
                                    title_pct = int(100 * x / z) if z > 0 else 0
                                    title_weight = 100.0 / total_titles_to_rip
                                    completed_progress = title_idx * title_weight
                                    current_progress = (title_pct / 100.0) * title_weight
                                    overall_pct = int(completed_progress + current_progress)
                                    self.progress.emit(original_row, max(0, min(100, overall_pct)))
                                    self.status_text.emit(original_row, f"Title {current_title_num}/{total_titles_to_rip} (#{title_id}) • {title_pct}%")
                                elif line:
                                    self.line_out.emit(original_row, f"Title {title_id}: {line}")

                            if proc.poll() is not None:
                                tail_messages(); break

                        title_success = proc.wait() == 0
                        if not title_success: overall_success = False

                    try:
                        if raw_tmp_path.exists():
                            if keep_raw: raw_tmp_path.rename(dest_dir / f"{pretty_log_path.stem}_title_{title_id}.raw.txt")
                            else: raw_tmp_path.unlink()
                    except Exception: pass

            except FileNotFoundError:
                self.line_out.emit(original_row, f"ERROR: makemkvcon not found. Check path in Preferences.")
                overall_success = False
            except Exception as e:
                self.line_out.emit(original_row, f"CRITICAL ERROR: {e}")
                overall_success = False

            self.job_done.emit(original_row, overall_success)
