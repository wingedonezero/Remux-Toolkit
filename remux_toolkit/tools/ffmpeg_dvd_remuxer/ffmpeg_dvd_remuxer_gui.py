# remux_toolkit/tools/ffmpeg_dvd_remuxer/ffmpeg_dvd_remuxer_gui.py
from pathlib import Path
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QTreeWidgetItem,
    QHeaderView, QProgressBar, QTextEdit, QCheckBox, QDialog, QSplitter, QFileDialog
)
from PyQt6.QtCore import QThread, Qt

from .ffmpeg_dvd_remuxer_config import DEFAULTS
from .core.worker import Worker
from .models.job import Job
from .gui.prefs_dialog import PrefsDialog
from .gui.queue_tree import DropTree
from .gui.details_panel import DetailsPanel
from .utils.paths import find_dvd_sources
from .utils.helpers import time_str_to_seconds

class FFmpegDVDRemuxerWidget(QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'ffmpeg_dvd_remuxer'
        self.config = {}
        self.jobs = []

        # --- FIX for Race Condition ---
        # This pool holds references to active analysis workers
        self.active_analysis_workers = []
        # These are now only for the main *processing* task, not analysis
        self.processing_worker_thread = None
        self.processing_worker = None
        # --- END FIX ---

        self._updating_checks = False

        self._init_ui()
        self._load_settings()

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        action_layout = QHBoxLayout()
        self.add_btn = QPushButton("Add Source(s)...")
        self.clear_btn = QPushButton("Clear Queue")
        self.process_btn = QPushButton("Process Queue")
        self.prefs_btn = QPushButton("Preferences…")
        self.stop_btn = QPushButton("Stop Process")
        action_layout.addWidget(self.add_btn)
        action_layout.addWidget(self.clear_btn)
        action_layout.addWidget(self.process_btn)
        action_layout.addWidget(self.prefs_btn)
        action_layout.addStretch()
        action_layout.addWidget(self.stop_btn)

        self.queue_tree = DropTree()
        self.queue_tree.setColumnCount(5)
        self.queue_tree.setHeaderLabels(["Source / Title", "Length", "Chapters", "Audio", "Subs"])
        self.queue_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, 5): self.queue_tree.header().setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)

        self.details_panel = DetailsPanel()
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.progress_bar = QProgressBar()

        center_splitter = QSplitter(Qt.Orientation.Horizontal)
        center_splitter.addWidget(self.queue_tree)
        center_splitter.addWidget(self.details_panel)
        center_splitter.setSizes([700, 300])

        main_splitter = QSplitter(Qt.Orientation.Vertical)
        main_splitter.addWidget(center_splitter)
        main_splitter.addWidget(self.log_box)
        main_splitter.setSizes([500, 200])

        main_layout.addLayout(action_layout)
        main_layout.addWidget(main_splitter)
        main_layout.addWidget(self.progress_bar)

        self.add_btn.clicked.connect(self.add_source)
        self.clear_btn.clicked.connect(self.clear_queue)
        self.queue_tree.pathsDropped.connect(self.handle_drop)
        self.prefs_btn.clicked.connect(self.open_prefs)
        self.process_btn.clicked.connect(self.start_processing)
        self.stop_btn.clicked.connect(self.stop_processing)
        self.queue_tree.itemChanged.connect(self._on_item_checked)
        self.queue_tree.currentItemChanged.connect(self._on_item_selected)

        self.set_controls_enabled(True)

    def _load_settings(self):
        if not DEFAULTS.get("default_output_directory"):
            DEFAULTS["default_output_directory"] = str(Path.home() / "Remux-Toolkit-Output" / "DVDRemuxer")
        self.config = self.app_manager.load_config(self.tool_name, DEFAULTS)
        Path(self.config["default_output_directory"]).mkdir(parents=True, exist_ok=True)

    def save_settings(self):
        self.app_manager.save_config(self.tool_name, self.config)

    def shutdown(self):
        self.stop_processing()
        # Also stop any lingering analysis workers
        for thread, worker in self.active_analysis_workers:
            worker.stop()
            thread.quit()
            thread.wait(1000)

    def set_controls_enabled(self, enabled: bool):
        is_busy = bool(self.active_analysis_workers) or (self.processing_worker is not None)
        self.add_btn.setEnabled(not is_busy)
        self.clear_btn.setEnabled(not is_busy)
        self.process_btn.setEnabled(not is_busy)
        self.prefs_btn.setEnabled(not is_busy)
        self.stop_btn.setEnabled(is_busy)

    def handle_drop(self, paths: list[str]):
        group_name = Path(paths[0]).parent.name if len(paths) > 1 and len(set(Path(p).parent for p in paths)) == 1 else None
        all_sources = [source for p_str in paths for source in find_dvd_sources(Path(p_str))]
        if not all_sources:
            self.log_box.append("No valid DVD sources (ISO/VIDEO_TS) found.")
            return

        self.set_controls_enabled(False)
        for source_path in all_sources:
            if any(j.source_path == source_path for j in self.jobs): continue
            job = Job(source_path=source_path, group_name=group_name)
            self._add_job_to_queue(job)
            self._run_analysis(job)

    def add_source(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select DVD Source", str(Path.home()), "DVD Sources (*.iso);;All Files (*)")
        if path:
            p = Path(path)
            source_dir = p.parent.parent if p.parent.name.lower() == 'video_ts' else p
            self.handle_drop([str(source_dir)])

    def clear_queue(self):
        self.stop_processing()
        self.jobs.clear()
        self.queue_tree.clear()
        self.details_panel.clear_panel()
        self.log_box.append("Queue cleared.")

    def _add_job_to_queue(self, job: Job):
        self.jobs.append(job)
        item = QTreeWidgetItem([job.base_name, "", "", "", ""])
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, Qt.CheckState.Unchecked)
        job._gui_item = item
        item.setData(0, Qt.ItemDataRole.UserRole, job)
        self.queue_tree.addTopLevelItem(item)

    def _run_analysis(self, job: Job):
        job.status = "Analyzing..."
        job._gui_item.setText(0, f"{job.base_name} [Analyzing...]")

        thread = QThread()
        worker = Worker(self.config)
        worker.moveToThread(thread)

        self.active_analysis_workers.append((thread, worker))

        worker.analysis_finished.connect(self.on_analysis_finished)
        worker.finished.connect(lambda w=worker, t=thread: self._on_analysis_worker_finished(w, t))

        thread.started.connect(lambda j=job: worker.run_analysis(j))
        thread.start()

    def on_analysis_finished(self, job: Job, titles: list):
        job.titles_info = titles
        item = job._gui_item
        if not item: return

        self._updating_checks = True
        try:
            item.takeChildren()
            min_len = self.config.get("minimum_title_length", 120)

            if not titles:
                job.status = "Analysis Failed"
                item.setText(0, f"{job.base_name} [Failed]")
                item.setCheckState(0, Qt.CheckState.Unchecked)
                item.setDisabled(True)
                return

            job.status = "Ready"
            item.setText(0, job.base_name)

            long_titles = [t for t in titles if time_str_to_seconds(t['length']) >= min_len]
            main_title = max(long_titles, key=lambda t: time_str_to_seconds(t['length']), default=None)

            for title_data in titles:
                child = QTreeWidgetItem([f"  - Title {title_data['title']}", title_data['length'], title_data['chapters'], title_data['audio'], title_data['subs']])
                child.setData(0, Qt.ItemDataRole.UserRole, title_data)
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                is_main = main_title and title_data['title'] == main_title['title']
                child.setCheckState(0, Qt.CheckState.Checked if is_main else Qt.CheckState.Unchecked)
                item.addChild(child)

            item.setExpanded(True)
            self._set_parent_check_from_children(item)
        finally:
            self._updating_checks = False

    def _on_analysis_worker_finished(self, worker, thread):
        # Safely remove the finished worker from the pool
        self.active_analysis_workers = [(t, w) for t, w in self.active_analysis_workers if w is not worker]
        thread.quit()
        worker.deleteLater()
        thread.deleteLater()
        self.set_controls_enabled(True) # Re-evaluate controls now that a task is done

    def start_processing(self):
        self.stop_processing()

        jobs_to_run = []
        for job in self.jobs:
            job.selected_titles.clear()
            item = job._gui_item
            if not item: continue
            for i in range(item.childCount()):
                child = item.child(i)
                if child.checkState(0) == Qt.CheckState.Checked:
                    title_data = child.data(0, Qt.ItemDataRole.UserRole)
                    job.selected_titles.add(int(title_data['title']))
            if job.selected_titles:
                jobs_to_run.append(job)

        if not jobs_to_run:
            self.log_box.append("No titles selected for processing.")
            return

        self.set_controls_enabled(False)
        self.processing_worker_thread = QThread()
        self.processing_worker = Worker(self.config)
        self.processing_worker.moveToThread(self.processing_worker_thread)

        self.processing_worker.log.connect(self.log_box.append)
        self.processing_worker.progress.connect(lambda cur, tot: self.progress_bar.setValue(int(cur / tot * 100) if tot > 0 else 0))
        self.processing_worker.finished.connect(self.on_processing_finished)

        self.processing_worker_thread.started.connect(lambda j=jobs_to_run: self.processing_worker.run_processing(j))
        self.processing_worker_thread.start()

    def _on_item_checked(self, item, column):
        if self._updating_checks: return
        self._updating_checks = True
        try:
            if item.parent():
                self._set_parent_check_from_children(item.parent())
            else:
                for i in range(item.childCount()):
                    item.child(i).setCheckState(0, item.checkState(0))
        finally:
            self._updating_checks = False

    def _set_parent_check_from_children(self, parent_item):
        child_count = parent_item.childCount()
        if child_count == 0:
            parent_item.setCheckState(0, Qt.CheckState.Unchecked)
            return
        checked_count = sum(1 for i in range(child_count) if parent_item.child(i).checkState(0) == Qt.CheckState.Checked)
        if checked_count == 0:
            parent_item.setCheckState(0, Qt.CheckState.Unchecked)
        elif checked_count == child_count:
            parent_item.setCheckState(0, Qt.CheckState.Checked)
        else:
            parent_item.setCheckState(0, Qt.CheckState.PartiallyChecked)

    def _on_item_selected(self, current, previous):
        if not current:
            self.details_panel.clear_panel()
            return

        if current.parent():
            title_data = current.data(0, Qt.ItemDataRole.UserRole)
            disc_job = current.parent().data(0, Qt.ItemDataRole.UserRole)
            self.details_panel.show_title_info(disc_job, title_data)
        else:
            job = current.data(0, Qt.ItemDataRole.UserRole)
            self.details_panel.show_disc_info(job)

    def open_prefs(self):
        dialog = PrefsDialog(self.config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.config.update(dialog.get_values())
            self.save_settings()
            self.log_box.append("[INFO] Settings saved.")

    def stop_processing(self):
        if self.processing_worker:
            self.log_box.append("[ACTION] Stop requested...")
            self.processing_worker.stop()

    def on_processing_finished(self):
        self.set_controls_enabled(True)
        if self.processing_worker_thread:
            self.processing_worker_thread.quit()
            self.processing_worker_thread.wait()
        self.processing_worker_thread = None
        self.processing_worker = None
