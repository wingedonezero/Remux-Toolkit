# remux_toolkit/tools/ffmpeg_dvd_remuxer/ffmpeg_dvd_remuxer_gui.py
from pathlib import Path
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QTableWidgetItem,
    QHeaderView, QProgressBar, QTextEdit, QCheckBox, QDialog, QSplitter
)
from PyQt6.QtCore import QThread

from .ffmpeg_dvd_remuxer_config import DEFAULTS
from .core.worker import Worker
from .models.job import Job
from .gui.prefs_dialog import PrefsDialog
from .gui.title_table import DropTable
from .utils.paths import find_dvd_sources, get_base_name
from .utils.helpers import time_str_to_seconds

class FFmpegDVDRemuxerWidget(QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'ffmpeg_dvd_remuxer'
        self.config = {}
        self.queue = [] # This will now be a list of analyzed discs
        self.worker_thread = None
        self.worker = None

        self._init_ui()
        self._load_settings()

    def _init_ui(self):
        main_layout = QVBoxLayout(self)

        self.queue_table = DropTable()
        self.queue_table.setColumnCount(6)
        self.queue_table.setHorizontalHeaderLabels(["Process", "Source", "Titles", "Length", "Chapters", "Status"])
        header = self.queue_table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for i in range(2, 6): header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)

        splitter = QSplitter(self)
        splitter.setOrientation(self.get_closest_orientation(Qt.Orientation.Vertical))
        splitter.addWidget(self.queue_table)
        splitter.addWidget(self.log_box)

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

        self.progress_bar = QProgressBar()

        main_layout.addLayout(action_layout)
        main_layout.addWidget(splitter)
        main_layout.addWidget(self.progress_bar)

        # Connect signals
        self.add_btn.clicked.connect(self.add_source)
        self.clear_btn.clicked.connect(self.clear_queue)
        self.queue_table.pathsDropped.connect(self.handle_drop)
        self.prefs_btn.clicked.connect(self.open_prefs)
        self.process_btn.clicked.connect(self.start_processing)
        self.stop_btn.clicked.connect(self.stop_processing)

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
        if self.worker_thread and self.worker_thread.isRunning():
            self.worker_thread.quit()
            self.worker_thread.wait(2000)

    def set_controls_enabled(self, enabled: bool):
        self.add_btn.setEnabled(enabled)
        self.clear_btn.setEnabled(enabled)
        self.process_btn.setEnabled(enabled)
        self.prefs_btn.setEnabled(enabled)
        self.stop_btn.setEnabled(not enabled)

    def handle_drop(self, paths: list[str]):
        group_name = Path(paths[0]).parent.name if len(paths) > 1 and len(set(Path(p).parent for p in paths)) == 1 else None

        all_sources = []
        for p_str in paths:
            all_sources.extend(find_dvd_sources(Path(p_str)))

        for source_path in all_sources:
            self._run_analysis(source_path, group_name)

    def add_source(self):
        # In a real implementation, you would open a file dialog here.
        # For simplicity, we'll just log a message.
        self.log_box.append("Please drag and drop DVD sources (ISO/VIDEO_TS folders) onto the table.")

    def clear_queue(self):
        self.queue.clear()
        self.queue_table.setRowCount(0)
        self.log_box.append("Queue cleared.")

    def open_prefs(self):
        dialog = PrefsDialog(self.config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.config.update(dialog.get_values())
            self.save_settings()
            self.log_box.append("[INFO] Settings saved.")

    def _run_analysis(self, source_path: Path, group_name: str | None):
        row = self.queue_table.rowCount()
        self.queue_table.insertRow(row)

        base_name = get_base_name(source_path)
        self.queue_table.setItem(row, 1, QTableWidgetItem(base_name))
        self.queue_table.setItem(row, 5, QTableWidgetItem("Analyzing..."))

        # Store path in user data role for later retrieval
        self.queue_table.item(row, 1).setData(Qt.ItemDataRole.UserRole, (source_path, group_name))

        self.worker_thread = QThread()
        self.worker = Worker(self.config)
        self.worker.moveToThread(self.worker_thread)
        self.worker.analysis_finished.connect(lambda titles, r=row: self.on_analysis_finished(r, titles))
        self.worker.finished.connect(self.on_worker_finished)

        self.worker_thread.started.connect(lambda p=str(source_path): self.worker.run_analysis(p))
        self.worker_thread.start()

    def on_analysis_finished(self, row, titles):
        item = self.queue_table.item(row, 1)
        if not item: return

        (source_path, group_name) = item.data(Qt.ItemDataRole.UserRole)
        self.queue.append({'path': source_path, 'group': group_name, 'titles': titles, 'row': row})

        if not titles:
            self.queue_table.setItem(row, 5, QTableWidgetItem("Analysis Failed"))
            return

        min_len = self.config.get("minimum_title_length", 120)
        long_titles = [t for t in titles if time_str_to_seconds(t['length']) >= min_len]

        main_title = max(long_titles, key=lambda t: time_str_to_seconds(t['length']), default=None)

        self.queue_table.setItem(row, 2, QTableWidgetItem(f"{len(long_titles)}/{len(titles)}")) # Titles
        if main_title:
            self.queue_table.setItem(row, 3, QTableWidgetItem(main_title.get('length', '')))
            self.queue_table.setItem(row, 4, QTableWidgetItem(main_title.get('chapters', '')))

        self.queue_table.setItem(row, 5, QTableWidgetItem("Ready"))

        cb = QCheckBox()
        cb.setChecked(True)
        w = QWidget()
        l = QHBoxLayout(w)
        l.addWidget(cb)
        l.setContentsMargins(0,0,0,0)
        self.queue_table.setCellWidget(row, 0, w)

    def start_processing(self):
        jobs_to_run = []
        for disc in self.queue:
            row = disc['row']
            cb_widget = self.queue_table.cellWidget(row, 0)
            if cb_widget and cb_widget.findChild(QCheckBox).isChecked():
                min_len = self.config.get("minimum_title_length", 120)
                titles_to_process = [int(t['title']) for t in disc['titles'] if time_str_to_seconds(t['length']) >= min_len]
                if titles_to_process:
                    jobs_to_run.append(Job(
                        input_path=disc['path'],
                        base_name=get_base_name(disc['path']),
                        group_name=disc['group'],
                        titles_to_process=titles_to_process
                    ))

        if not jobs_to_run:
            self.log_box.append("No items in queue selected for processing.")
            return

        self.set_controls_enabled(False)
        self.worker_thread = QThread()
        self.worker = Worker(self.config)
        self.worker.moveToThread(self.worker_thread)
        self.worker.log.connect(self.log_box.append)
        self.worker.progress.connect(lambda cur, tot: self.progress_bar.setValue(int(cur / tot * 100)))
        self.worker.finished.connect(self.on_worker_finished)

        self.worker_thread.started.connect(lambda: self.worker.run_processing(jobs_to_run))
        self.worker_thread.start()

    def stop_processing(self):
        if self.worker:
            self.log_box.append("[ACTION] Stop requested...")
            self.worker.stop()

    def on_worker_finished(self):
        self.set_controls_enabled(True)
        if self.worker_thread:
            self.worker_thread.quit()
            self.worker_thread.wait()
        self.worker_thread = None
        self.worker = None
