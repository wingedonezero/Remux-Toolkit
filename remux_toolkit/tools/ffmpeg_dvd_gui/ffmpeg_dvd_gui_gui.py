# remux_toolkit/tools/ffmpeg_dvd_gui/ffmpeg_dvd_gui_gui.py
"""
FFmpeg DVD Remuxer GUI - Uses FFmpeg's dvdvideo demuxer to remux DVDs to MKV.
Designed as an alternative to MakeMKV for discs it can't handle.
"""
import os
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QThread, QUrl
from PyQt6.QtGui import QAction, QColor, QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QHBoxLayout, QPushButton, QSplitter,
    QTreeWidgetItem, QProgressBar, QMenu, QFileDialog, QHeaderView, QDialog
)

from .ffmpeg_dvd_gui_config import DEFAULTS
from .utils.paths import find_dvd_roots_with_structure, is_iso, get_dvd_input_path
from .utils.ffmpeg_parser import duration_to_seconds, format_bytes_human
from .models.job import Job
from .core.info_probe import DVDProbeWorker as FFprobeDVDProbeWorker
from .core.info_probe_native import DVDProbeWorker as NativeDVDProbeWorker
from .core.remuxer import FFmpegDVDWorker
from .gui.queue_tree import DropTree
from .gui.details_panel import DetailsPanel
from .gui.console_widget import FilterableConsole
from .gui.prefs_dialog import PrefsDialog


class FFmpegDVDGUIWidget(QWidget):
    """Main widget for FFmpeg DVD Remuxer tool."""

    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'ffmpeg_dvd_gui'
        self.settings = {}
        self._updating_checks = False
        self.jobs: list[Job] = []
        self.running = False
        self.completed_jobs = {}
        self.current_job_row: Optional[int] = None

        self._init_ui()
        self._load_settings()
        self._setup_workers()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        self.queue_label = QLabel("Queue: 0 jobs loaded")
        self.queue_label.setStyleSheet("font-weight:600;")

        # Tree Widget for the queue
        self.tree = DropTree()
        self.tree.setColumnCount(8)
        self.tree.setHeaderLabels([
            "Source/Title", "Video", "Audio", "Subs", "Chapters", "Duration", "Status", "Progress"
        ])
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, 8):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)

        self.details = DetailsPanel()
        self.console = FilterableConsole()

        # Splitters
        self.center_split = QSplitter(Qt.Orientation.Horizontal)
        self.center_split.addWidget(self.tree)
        self.center_split.addWidget(self.details)
        self.v_split = QSplitter(Qt.Orientation.Vertical)
        self.v_split.addWidget(self.center_split)
        self.v_split.addWidget(self.console)

        # Buttons
        self.btn_add_iso = QPushButton("Add ISO(s)...")
        self.btn_add_folder = QPushButton("Add Folder(s)...")
        self.btn_remove = QPushButton("Remove Selected")
        self.btn_clear = QPushButton("Clear")
        self.btn_prefs = QPushButton("Preferences...")
        self.btn_start = QPushButton("Start Queue")
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)

        top_layout = QHBoxLayout()
        for b in (self.btn_add_iso, self.btn_add_folder, self.btn_remove,
                  self.btn_clear, self.btn_prefs, self.btn_start, self.btn_stop):
            top_layout.addWidget(b)
        top_layout.addStretch()

        # Assemble Layout
        layout.addWidget(self.queue_label)
        layout.addLayout(top_layout)
        layout.addWidget(self.v_split)

        # Connect signals
        self.tree.customContextMenuRequested.connect(self._row_menu)
        self.tree.itemChanged.connect(self._on_item_checked)
        self.tree.currentItemChanged.connect(self._on_current_item_changed)
        self.tree.pathsDropped.connect(self._add_paths)
        self.tree.itemsReordered.connect(self._on_jobs_reordered)
        self.btn_add_iso.clicked.connect(self.add_isos)
        self.btn_add_folder.clicked.connect(self.add_folders)
        self.btn_remove.clicked.connect(self.remove_selected)
        self.btn_clear.clicked.connect(self.clear_all)
        self.btn_prefs.clicked.connect(self.open_prefs)
        self.btn_start.clicked.connect(self.start_queue)
        self.btn_stop.clicked.connect(self.stop_queue)

    def _setup_workers(self):
        probe_cls = NativeDVDProbeWorker if self.settings.get("use_native_probe", True) else FFprobeDVDProbeWorker
        self.probe_worker = probe_cls(self.settings)
        self.probe_thread = QThread(self)
        self.probe_worker.moveToThread(self.probe_thread)
        self.probe_worker.probed.connect(self._on_probed)
        self.probe_thread.start()

        self.worker = FFmpegDVDWorker(self.settings)
        self.work_thread = QThread(self)
        self.worker.moveToThread(self.work_thread)
        self.worker.progress.connect(self.on_progress)
        self.worker.status_text.connect(self.on_status_text)
        self.worker.line_out.connect(self.on_line)
        self.worker.job_done.connect(self.on_done)
        self.work_thread.started.connect(self.worker.run)

    def _load_settings(self):
        if not DEFAULTS.get("output_root"):
            DEFAULTS["output_root"] = str(Path.home() / "Remux-Toolkit-Output" / "FFmpegDVD")

        self.settings = self.app_manager.load_config(self.tool_name, DEFAULTS)
        Path(self.settings["output_root"]).mkdir(parents=True, exist_ok=True)

        if cw := self.settings.get("col_widths"):
            if len(cw) == self.tree.columnCount():
                for i, w in enumerate(cw):
                    self.tree.setColumnWidth(i, int(w))
        if cs := self.settings.get("center_split_sizes"):
            self.center_split.setSizes([int(x) for x in cs])
        if vs := self.settings.get("v_split_sizes"):
            self.v_split.setSizes([int(x) for x in vs])

    def save_settings(self):
        self.settings["col_widths"] = [
            self.tree.columnWidth(i) for i in range(self.tree.columnCount())
        ]
        self.settings["center_split_sizes"] = self.center_split.sizes()
        self.settings["v_split_sizes"] = self.v_split.sizes()
        self.app_manager.save_config(self.tool_name, self.settings)

    def shutdown(self):
        if hasattr(self, 'worker') and self.worker:
            self.worker.stop()
        if hasattr(self, 'work_thread') and self.work_thread.isRunning():
            self.work_thread.quit()
            self.work_thread.wait(2000)
        if hasattr(self, 'probe_thread') and self.probe_thread.isRunning():
            self.probe_thread.quit()
            self.probe_thread.wait(2000)

    def open_prefs(self):
        dlg = PrefsDialog(self.settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.settings.update(dlg.get_values())
            self.save_settings()
            self.console.append("Saved preferences.", "success")
            self.probe_worker.settings = self.settings
            self.worker.settings = self.settings

    def _row_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item:
            return
        job = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(job, Job) and item.parent():
            job = item.parent().data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(job, Job):
            return

        menu = QMenu(self)

        def _open(p: Optional[Path]):
            if p and Path(p).exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

        act_open_out = QAction("Open Output Folder", self)
        act_open_out.triggered.connect(lambda: _open(job.out_dir))
        menu.addAction(act_open_out)

        act_open_log = QAction("Open Log File", self)
        act_open_log.triggered.connect(lambda: _open(job.log_path))
        menu.addAction(act_open_log)

        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def add_isos(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select ISO files", str(Path.home()),
            "Images (*.iso *.img)"
        )
        if files:
            self._add_paths(files)

    def add_folders(self):
        d = QFileDialog.getExistingDirectory(
            self, "Choose disc folder", str(Path.home())
        )
        if d:
            self._add_paths([d])

    def _add_paths(self, paths):
        for p_str in paths:
            if not (pth := Path(p_str)).exists():
                continue
            disc_infos = find_dvd_roots_with_structure(pth)
            for disc_info in disc_infos:
                self._queue_one_with_structure(disc_info)
        self._refresh_queue_label()

    def _queue_one_with_structure(self, disc_info):
        from .utils.paths import DiscInfo
        job = Job(
            source_type="iso" if is_iso(disc_info.disc_path) else "folder",
            source_path=str(disc_info.disc_path),
            child_name=disc_info.display_name,
            relative_path=disc_info.relative_path,
            drop_root=disc_info.drop_root,
        )
        self.jobs.append(job)
        item = QTreeWidgetItem([job.child_name, "", "", "", "", "", "Queued", ""])
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, Qt.CheckState.Checked)
        item.setData(0, Qt.ItemDataRole.UserRole, job)
        self.tree.addTopLevelItem(item)

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setFixedHeight(12)
        bar.setTextVisible(False)
        self.tree.setItemWidget(item, 7, bar)

        # Trigger probe
        self.probe_worker.probe(self.tree.indexOfTopLevelItem(item), job)

    def _on_jobs_reordered(self):
        new_jobs = [
            self.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
            for i in range(self.tree.topLevelItemCount())
        ]
        self.jobs = [j for j in new_jobs if isinstance(j, Job)]

    def _on_probed(self, row: int, label: Optional[str], titles_total: Optional[int],
                   titles_info: Optional[dict], disc_info: Optional[dict], err: str):
        if not (0 <= row < len(self.jobs)):
            return
        job, item = self.jobs[row], self.tree.topLevelItem(row)
        if not item:
            return

        if label:
            job.label_hint = label
        job.titles_total = titles_total
        job.titles_info = titles_info
        job.disc_info = disc_info

        self._updating_checks = True
        try:
            item.takeChildren()
            # Phase 3: every title is shown; analyzer's `hidden_by_default`
            # controls only the *initial check state*. User can always check
            # or uncheck anything. Reasons surface inline + tooltip + details.
            minlen = int(self.settings.get("minlength", 60))
            any_auto_checked = False

            for t_idx in sorted(titles_info or {}):
                info = titles_info[t_idx]
                duration_secs = info.get("duration_seconds", 0) or 0
                hidden_by_analyzer = bool(info.get("hidden_by_default", False))
                classification = info.get("classification") or ""
                dup_of = info.get("duplicate_of")
                contains = info.get("contains_titles") or []

                # Inline label so the row is self-explanatory at a glance.
                label_bits = [f"#{t_idx}"]
                if dup_of is not None:
                    label_bits.append(f"↳ duplicate of #{dup_of}")
                elif classification == "filler":
                    label_bits.append("(filler)")
                elif classification == "stub":
                    label_bits.append("(stub)")
                if contains:
                    label_bits.append(
                        f"⊇ contains {', '.join(f'#{n}' for n in contains)}"
                    )
                title_text = "  ".join(label_bits)

                child = QTreeWidgetItem([
                    title_text,
                    info.get("video_codec", ""),
                    str(info.get("audio_count", 0)),
                    str(info.get("subtitle_count", 0)),
                    str(info.get("chapters", 0)),
                    info.get("duration", ""),
                    "",
                    "",
                ])
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)

                # Auto-check rule (transparent; reasons visible in tooltip):
                #   - hidden_by_analyzer (filler or duplicate): unchecked
                #   - duration shorter than user minlength: unchecked
                #   - else: checked
                below_min = (duration_secs > 0 and duration_secs < minlen)
                auto_check = not (hidden_by_analyzer or below_min)
                child.setCheckState(
                    0, Qt.CheckState.Checked if auto_check else Qt.CheckState.Unchecked
                )
                if auto_check:
                    any_auto_checked = True

                # Tooltip explains the decision in plain language.
                tooltip = [f"Title #{t_idx}"]
                if classification:
                    tooltip.append(f"Classification: {classification}")
                if (vts := info.get("vts")) is not None:
                    tooltip.append(f"VTS {vts}, PGC {info.get('pgc', '?')}")
                if dup_of is not None:
                    basis = info.get("duplicate_basis") or ""
                    extra = f" (matched on {basis})" if basis else ""
                    tooltip.append(f"Duplicate of #{dup_of}{extra}")
                if contains:
                    tooltip.append(
                        "Compilation containing: "
                        + ", ".join(f"#{n}" for n in contains)
                    )
                if not auto_check:
                    if hidden_by_analyzer and dup_of is not None:
                        tooltip.append("Unchecked because it duplicates another title")
                    elif hidden_by_analyzer:
                        tooltip.append("Unchecked because classified as filler")
                    elif below_min:
                        tooltip.append(
                            f"Unchecked because duration {duration_secs:.0f}s "
                            f"< Preferences minimum length {minlen}s"
                        )
                tooltip.append("(Check or uncheck freely — nothing is filtered out.)")
                child.setToolTip(0, "\n".join(tooltip))

                # Visual cue: dim+italic for rows the analyzer would hide,
                # so the user can scan the tree and see at a glance.
                if hidden_by_analyzer:
                    font = child.font(0)
                    font.setItalic(True)
                    dim = QColor(140, 140, 140)
                    for col in range(child.columnCount()):
                        child.setFont(col, font)
                        child.setForeground(col, dim)

                item.addChild(child)

            # `selected_titles=None` means "use all checked children"; the
            # _on_item_checked handler keeps job.selected_titles in sync as
            # the user toggles.
            self._set_parent_check_from_children(item)
            self._on_item_checked(item, 0) if False else None  # no-op: handler runs on user events
            # Initialize job.selected_titles from current child check states.
            selected = set()
            checkable = 0
            for i in range(item.childCount()):
                ch = item.child(i)
                if ch.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    checkable += 1
                    if ch.checkState(0) == Qt.CheckState.Checked:
                        try:
                            selected.add(int(ch.text(0).split()[0].lstrip("#")))
                        except (ValueError, IndexError):
                            pass
            job.selected_titles = None if (checkable > 0 and len(selected) == checkable) else selected

        finally:
            self._updating_checks = False

        item.setText(6, "Ready" if not err else "Probe error")
        if err:
            self.console.append(f"ERROR for {job.child_name}: {err}", "error")

        self._refresh_queue_label()

    def _set_children_check(self, parent_item: QTreeWidgetItem, state: Qt.CheckState):
        for i in range(parent_item.childCount()):
            if (ch := parent_item.child(i)).flags() & Qt.ItemFlag.ItemIsUserCheckable:
                ch.setCheckState(0, state)

    def _set_parent_check_from_children(self, parent_item: QTreeWidgetItem):
        checkable = [
            parent_item.child(i) for i in range(parent_item.childCount())
            if parent_item.child(i).flags() & Qt.ItemFlag.ItemIsUserCheckable
        ]
        if not checkable:
            parent_item.setCheckState(0, Qt.CheckState.Unchecked)
            return

        checked_count = sum(1 for ch in checkable if ch.checkState(0) == Qt.CheckState.Checked)
        if checked_count == 0:
            parent_item.setCheckState(0, Qt.CheckState.Unchecked)
        elif checked_count == len(checkable):
            parent_item.setCheckState(0, Qt.CheckState.Checked)
        else:
            parent_item.setCheckState(0, Qt.CheckState.PartiallyChecked)

    def _on_item_checked(self, changed_item: QTreeWidgetItem, column: int):
        if self._updating_checks or self.running:
            return

        self._updating_checks = True
        try:
            top_item = changed_item if not (parent := changed_item.parent()) else parent
            job = top_item.data(0, Qt.ItemDataRole.UserRole)
            if not job:
                return

            if not parent:
                self._set_children_check(changed_item, changed_item.checkState(0))
            else:
                self._set_parent_check_from_children(parent)

            selected_titles = set()
            all_titles_selected = True
            checkable_child_count = 0
            for i in range(top_item.childCount()):
                child = top_item.child(i)
                if child.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    checkable_child_count += 1
                    if child.checkState(0) == Qt.CheckState.Checked:
                        try:
                            # Title cell may include trailing reason text like
                            # "#5  ↳ duplicate of #4" — extract the leading
                            # "#N" token only.
                            selected_titles.add(int(child.text(0).split()[0].lstrip("#")))
                        except (ValueError, IndexError):
                            pass
                    else:
                        all_titles_selected = False

            if checkable_child_count > 0 and all_titles_selected:
                job.selected_titles = None
            else:
                job.selected_titles = selected_titles

        finally:
            self._updating_checks = False

        self._refresh_queue_label()

    def _on_current_item_changed(self, cur, prev):
        if not cur:
            self.details.clear()
            return

        job, is_title = cur.data(0, Qt.ItemDataRole.UserRole), False
        if not isinstance(job, Job):
            if parent := cur.parent():
                job, is_title = parent.data(0, Qt.ItemDataRole.UserRole), True

        if not isinstance(job, Job):
            self.details.clear()
            return

        if not is_title:
            self.details.show_disc(
                job.label_hint or job.child_name,
                job.source_path,
                str(job.titles_total or "?"),
                job.disc_info
            )
            return

        try:
            t_idx = int(cur.text(0).split()[0].lstrip("#"))
        except (ValueError, IndexError):
            self.details.clear()
            return

        self.details.show_title(t_idx, (job.titles_info or {}).get(t_idx, {}))

    def remove_selected(self):
        item = self.tree.currentItem()
        if not item:
            return
        if item.parent():
            item = item.parent()
        if (row := self.tree.indexOfTopLevelItem(item)) >= 0:
            self.tree.takeTopLevelItem(row)
            self.jobs.pop(row)
            self._refresh_queue_label()
            self.details.clear()

    def clear_all(self):
        if self.running:
            self.worker.stop()
            if self.work_thread.isRunning():
                self.work_thread.quit()
                self.work_thread.wait(1000)
            self.running = False
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)

        self.tree.clear()
        self.jobs.clear()
        self.console.clear()
        self.details.clear()
        self.completed_jobs.clear()
        self.current_job_row = None
        self._refresh_queue_label()

    def start_queue(self):
        if self.running:
            return

        jobs_to_run = [
            (i, job, job.selected_titles)
            for i, job in enumerate(self.jobs)
            if job.selected_titles is None or job.selected_titles
        ]

        if not jobs_to_run:
            self.console.append("=== No jobs or titles selected to run ===", "warning")
            return

        self.console.clear()
        self.console.append("=== Starting queue ===", "info")
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.running = True
        self.completed_jobs.clear()
        self.worker.set_jobs(jobs_to_run)
        self.work_thread.start()

    def stop_queue(self):
        if self.running:
            self.worker.stop()
            self.console.append(">>> Stop requested...", "warning")
            self.running = False
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)

    def _calculate_estimated_duration(self) -> float:
        """Calculate estimated total duration for selected titles."""
        total_secs = 0
        for job in self.jobs:
            if not job.titles_info:
                continue

            if job.selected_titles is None:
                titles_to_count = job.titles_info.keys()
            elif job.selected_titles:
                titles_to_count = job.selected_titles
            else:
                continue

            for title_id in titles_to_count:
                if title_id in job.titles_info:
                    total_secs += job.titles_info[title_id].get("duration_seconds", 0)

        return total_secs

    def _refresh_queue_label(self):
        job_count = len(self.jobs)
        total_duration = self._calculate_estimated_duration()
        if total_duration > 0:
            hours = int(total_duration // 3600)
            minutes = int((total_duration % 3600) // 60)
            duration_str = f"{hours}h {minutes}m"
        else:
            duration_str = "Unknown"
        self.queue_label.setText(f"Queue: {job_count} jobs loaded (Total duration: {duration_str})")

    def on_progress(self, row, pct):
        if 0 <= row < self.tree.topLevelItemCount():
            if item := self.tree.topLevelItem(row):
                if bar := self.tree.itemWidget(item, 7):
                    bar.setValue(max(0, min(100, pct)))

    def on_status_text(self, row, text):
        if 0 <= row < self.tree.topLevelItemCount():
            if item := self.tree.topLevelItem(row):
                item.setText(6, text)

    def on_line(self, row, line, severity="info"):
        self.console.append(line, severity)

    def on_done(self, row, ok, error_message: str):
        self.completed_jobs[row] = ok
        if item := self.tree.topLevelItem(row):
            if ok:
                item.setText(6, "Done")
            else:
                status = "Failed"
                if error_message:
                    status = f"Failed: {error_message[:50]}..." if len(error_message) > 50 else f"Failed: {error_message}"
                item.setText(6, status)

        if not ok and error_message:
            self.console.append(f"Job {row} failed: {error_message}", "error")

        is_last = (len(self.completed_jobs) >= len(self.worker.jobs_to_run))
        if self.worker._stop or is_last:
            self.console.append("=== Queue finished ===", "info")

            success_count = sum(1 for success in self.completed_jobs.values() if success)
            total_count = len(self.completed_jobs)

            if success_count == total_count:
                self.console.append(f"Completed: {success_count}/{total_count} jobs successful", "success")
            else:
                self.console.append(f"Completed: {success_count}/{total_count} jobs successful", "warning")

            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self.running = False
            self.current_job_row = None
            self.work_thread.quit()
