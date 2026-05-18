"""
Qt worker for the native (libmkv-shim) rip backend.

Mirrors ``FFmpegDVDWorker``'s signal interface (``progress``,
``status_text``, ``line_out``, ``job_done``) so the GUI can swap the
two with no other changes. Set ``settings["use_native_remux"] = True``
to route the rip through this worker.

The native backend is opt-in until cross-validation against MakeMKV on
the full corpus confirms behavior matches.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from ..utils.paths import (
    create_output_structure, safe_name, unique_dir,
)
from .native_rip import NativeRipOptions, rip_title_native
from .mux.native import ShimStatus, get_shim_status


_logger = logging.getLogger(__name__)


class NativeRipWorker(QObject):
    """Qt worker for the libmkv_shim-based rip pipeline.

    Signal interface matches FFmpegDVDWorker for drop-in compatibility:
        progress     (row: int, percent: int)
        status_text  (row: int, status: str)
        line_out     (row: int, text: str, severity: str)
        job_done     (row: int, success: bool, error_message: str)

    Job format also matches: ``jobs_to_run`` is a list of either
    ``(row, job)`` or ``(row, job, captured_selection)`` tuples.
    """
    progress = pyqtSignal(int, int)
    status_text = pyqtSignal(int, str)
    line_out = pyqtSignal(int, str, str)
    job_done = pyqtSignal(int, bool, str)

    def __init__(self, settings: dict):
        super().__init__()
        self.settings = settings
        self.jobs_to_run: list = []
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def set_jobs(self, jobs_to_run: list) -> None:
        self.jobs_to_run = jobs_to_run

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        # Verify the native shim is built before doing anything. We do
        # this once per worker run rather than per-title so the user
        # sees the same error once, not N times.
        info = get_shim_status()
        if info.status != ShimStatus.OK:
            for job_data in self.jobs_to_run:
                row = job_data[0]
                self.line_out.emit(
                    row,
                    f"Native muxer unavailable: {info.status.value}. "
                    f"Build via Settings → Native Muxer → Rebuild.",
                    "error",
                )
                self.job_done.emit(row, False, "native shim not built")
            return

        for job_data in self.jobs_to_run:
            if self._stop:
                row = job_data[0]
                self.status_text.emit(row, "Stopped")
                self.job_done.emit(row, False, "Stopped by user")
                break
            if len(job_data) == 3:
                row, job, selection = job_data
            else:
                row, job = job_data
                selection = job.selected_titles
            self._run_one_job(row, job, selection)

    def _run_one_job(self, row: int, job: Any, selection) -> None:
        self.status_text.emit(row, "Starting...")
        self.progress.emit(row, 0)
        try:
            output_root = Path(self.settings["output_root"])
            output_root.mkdir(parents=True, exist_ok=True)
            # Resolve output directory — same layout as FFmpegDVDWorker.
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
                    disc_info, output_root,
                    getattr(job, "preserve_structure", True),
                )
            else:
                base_name = safe_name(job.label_hint or job.child_name)
                dest_dir = unique_dir(output_root / base_name)
                dest_dir.mkdir(parents=True, exist_ok=True)

            log_path = dest_dir / f"{dest_dir.name}_native.log"
            job.out_dir, job.log_path = dest_dir, log_path
        except Exception as e:
            err = f"setup failed: {type(e).__name__}: {e}"
            self.line_out.emit(row, f"ERROR: {err}", "error")
            self.job_done.emit(row, False, err)
            return

        # Title selection ----------------------------------------------
        if isinstance(selection, set) and not selection:
            self.line_out.emit(row, "No titles selected - skipping job", "info")
            self.job_done.emit(row, True, "")
            return

        titles_to_rip: list[int]
        if isinstance(selection, (list, set, tuple)):
            titles_to_rip = sorted(int(t) for t in selection)
        else:
            titles_to_rip = []   # caller should pass an iterable; fail closed
        if not titles_to_rip:
            self.line_out.emit(row, "No titles selected - skipping job", "info")
            self.job_done.emit(row, True, "")
            return

        total = len(titles_to_rip)
        self.line_out.emit(row, f"Native rip: {total} title(s)", "info")

        success_count = 0
        last_error = ""
        for i, title_num in enumerate(titles_to_rip):
            if self._stop:
                self.status_text.emit(row, "Stopped")
                self.job_done.emit(row, False, "Stopped by user")
                return

            self.status_text.emit(row, f"Title {title_num} ({i+1}/{total})")
            out_path = dest_dir / f"title_{title_num:02d}.mkv"

            opts = NativeRipOptions(
                include_subpictures=self.settings.get("include_subpictures", True),
                include_closed_captions=self.settings.get("include_closed_captions", True),
                write_chapters=self.settings.get("write_chapters", True),
                angle=1,
                apply_trim=self.settings.get("apply_trim", False),
                log_callback=lambda sev, msg, _r=row: self.line_out.emit(_r, msg, sev),
                cancel_check=lambda: self._stop,
            )

            result = rip_title_native(
                disc_path=job.source_path, title_num=title_num,
                output_path=out_path, options=opts,
            )

            if result.success:
                success_count += 1
                self.line_out.emit(
                    row,
                    f"T{title_num} OK: {out_path.name} "
                    f"({result.frames_written} frames, "
                    f"{result.bytes_written / 1024 / 1024:.1f} MB, "
                    f"{result.elapsed_s:.1f}s, "
                    f"CC: {'merged' if result.cc_merged else ('declared' if result.cc_declared else 'none')})",
                    "info",
                )
                for w in result.warnings:
                    self.line_out.emit(row, f"warn T{title_num}: {w}", "warning")
            else:
                last_error = result.error_message or "unknown error"
                self.line_out.emit(
                    row, f"T{title_num} FAILED: {last_error}", "error",
                )
            # Overall progress = titles completed / total
            self.progress.emit(row, int(100 * (i + 1) / total))

        overall_ok = (success_count == total)
        self.status_text.emit(
            row, "Done" if overall_ok else f"Failed ({success_count}/{total})",
        )
        self.job_done.emit(row, overall_ok, "" if overall_ok else last_error)


__all__ = ["NativeRipWorker"]
