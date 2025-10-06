# remux_toolkit/tools/telecine_detector/telecine_detector_gui.py

import os
from typing import Dict, List

from PyQt6 import QtWidgets, QtCore, QtGui
from . import telecine_detector_core as core
from . import telecine_detector_config as config


class TelecineDetectorWidget(QtWidgets.QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'telecine_detector'
        self.setAcceptDrops(True)
        self.results: Dict[str, core.IdetResult] = {}
        self.worker_thread = None

        # --- NEW: Queue for sequential analysis ---
        self.analysis_queue: List[str] = []
        self.is_analyzing = False

        if core.which("ffmpeg") is None:
            QtWidgets.QMessageBox.critical(self, "Missing FFmpeg", "ffmpeg not found in PATH. Please install FFmpeg.")

        self._init_ui()
        self._load_settings()

    def _init_ui(self):
        main_layout = QtWidgets.QVBoxLayout(self)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)

        left_widget = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_widget)

        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["File", "Verdict", "Progressive", "TFF", "BFF"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for i in range(1, 5):
            header.setSectionResizeMode(i, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemSelectionChanged.connect(self.update_detail_view)

        controls_layout = QtWidgets.QHBoxLayout()
        add_button = QtWidgets.QPushButton("Add Files...")
        add_button.clicked.connect(self.add_files)
        self.analyze_selected_btn = QtWidgets.QPushButton("Analyze Selected")
        self.analyze_selected_btn.clicked.connect(self.analyze_selected)

        # --- NEW "Analyze All" BUTTON ---
        self.analyze_all_btn = QtWidgets.QPushButton("Analyze All")
        self.analyze_all_btn.clicked.connect(self.analyze_all)

        clear_button = QtWidgets.QPushButton("Clear List")
        clear_button.clicked.connect(self.clear_all)

        controls_layout.addWidget(add_button)
        controls_layout.addWidget(self.analyze_selected_btn)
        controls_layout.addWidget(self.analyze_all_btn)
        controls_layout.addWidget(clear_button)
        controls_layout.addStretch()

        controls_layout.addWidget(QtWidgets.QLabel("Telecine Threshold:"))
        self.threshold_spinbox = QtWidgets.QSpinBox()
        self.threshold_spinbox.setRange(50, 100)
        self.threshold_spinbox.setSuffix(" %")
        self.threshold_spinbox.setToolTip("Mark as 'Telecined (Film)' if progressive frames are at or above this percentage.")
        controls_layout.addWidget(self.threshold_spinbox)

        left_layout.addWidget(self.table)
        left_layout.addLayout(controls_layout)

        self.detail_view = QtWidgets.QTextEdit()
        self.detail_view.setReadOnly(True)
        self.detail_view.setLineWrapMode(QtWidgets.QTextEdit.LineWrapMode.NoWrap)
        self.detail_view.setPlaceholderText("Select an analyzed file to see the full FFmpeg output...")

        splitter.addWidget(left_widget)
        splitter.addWidget(self.detail_view)
        splitter.setSizes([600, 400])
        main_layout.addWidget(splitter)

    def _load_settings(self):
        self.settings = self.app_manager.load_config(self.tool_name, config.DEFAULTS)
        self.threshold_spinbox.setValue(self.settings.get('telecine_threshold_pct', config.DEFAULTS['telecine_threshold_pct']))

    def save_settings(self):
        self.settings['telecine_threshold_pct'] = self.threshold_spinbox.value()
        self.app_manager.save_config(self.tool_name, self.settings)

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent):
        if event.mimeData().hasUrls(): event.acceptProposedAction()

    def dropEvent(self, event: QtGui.QDropEvent):
        urls = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        self.add_files_to_table(core.collect_video_paths(urls))

    def add_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Add video files", "", "Video files (*.mkv *.mp4 *.m2ts *.ts *.vob *.mpg);;All files (*.*)")
        if files: self.add_files_to_table(core.collect_video_paths(files))

    def add_files_to_table(self, file_paths: List[str]):
        current_files = {self.table.item(row, 0).text() for row in range(self.table.rowCount())}
        for path in file_paths:
            if path not in current_files:
                rc = self.table.rowCount()
                self.table.insertRow(rc)
                self.table.setItem(rc, 0, QtWidgets.QTableWidgetItem(path))
                self.table.setItem(rc, 1, QtWidgets.QTableWidgetItem("Pending"))
                for i in range(2, 5):
                    item = QtWidgets.QTableWidgetItem("-")
                    item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                    self.table.setItem(rc, i, item)

    def analyze_selected(self):
        selected_rows = {item.row() for item in self.table.selectedItems()}
        if not selected_rows:
            QtWidgets.QMessageBox.information(self, "No Selection", "Please select one or more files to analyze.")
            return

        files_to_queue = [self.table.item(row, 0).text() for row in sorted(list(selected_rows))]
        self._queue_files_for_analysis(files_to_queue)

    def analyze_all(self):
        if self.table.rowCount() == 0:
            return
        files_to_queue = [self.table.item(row, 0).text() for row in range(self.table.rowCount())]
        self._queue_files_for_analysis(files_to_queue)

    def _queue_files_for_analysis(self, files: List[str]):
        if self.is_analyzing:
            QtWidgets.QMessageBox.warning(self, "Busy", "An analysis queue is already running.")
            return

        self.analysis_queue = files
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).text() in self.analysis_queue:
                self.table.item(row, 1).setText("Queued")

        self._start_next_in_queue()

    def _start_next_in_queue(self):
        if not self.analysis_queue:
            self.is_analyzing = False
            self._set_buttons_enabled(True)
            return

        self.is_analyzing = True
        self._set_buttons_enabled(False)

        file_path = self.analysis_queue.pop(0)
        self.settings['telecine_threshold_pct'] = self.threshold_spinbox.value()

        # Find the row and set its status
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).text() == file_path:
                self.table.item(row, 1).setText("Analyzing...")
                break

        self._run_worker(file_path)

    def _run_worker(self, file_path: str):
        self.worker = core.Worker(self.settings)
        self.worker_thread = QtCore.QThread()
        self.worker.moveToThread(self.worker_thread)
        self.worker.finished.connect(self.on_analysis_finished)
        self.worker_thread.started.connect(lambda p=file_path: self.worker.analyze(p))
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.start()

    @QtCore.pyqtSlot(str, core.IdetResult)
    def on_analysis_finished(self, file_path: str, result: core.IdetResult):
        threshold = self.settings.get('telecine_threshold_pct', config.DEFAULTS['telecine_threshold_pct'])
        verdict = result.get_verdict(threshold)
        self.results[file_path] = result

        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).text() == file_path:
                self.table.item(row, 1).setText(verdict)
                self.table.item(row, 2).setText(str(result.multi_prog))
                self.table.item(row, 3).setText(str(result.multi_tff))
                self.table.item(row, 4).setText(str(result.multi_bff))
                if self.table.currentRow() == row: self.update_detail_view()
                break

        self.worker_thread.quit()
        self.worker_thread.wait()

        # --- NEW: Trigger the next item in the queue ---
        self._start_next_in_queue()

    def update_detail_view(self):
        selected_rows = {item.row() for item in self.table.selectedItems()}
        if not selected_rows:
            self.detail_view.clear()
            return

        first_row = sorted(list(selected_rows))[0]
        file_path = self.table.item(first_row, 0).text()

        if file_path in self.results:
            result = self.results[file_path]
            summary = result.get_summary_text()
            self.detail_view.setText(summary + result.raw_output)
        else:
            self.detail_view.setPlaceholderText("File has not been analyzed yet.")
            self.detail_view.clear()

    def clear_all(self):
        if self.is_analyzing:
            return
        self.table.setRowCount(0)
        self.results.clear()
        self.detail_view.clear()

    def _set_buttons_enabled(self, enabled: bool):
        self.analyze_all_btn.setEnabled(enabled)
        self.analyze_selected_btn.setEnabled(enabled)

    def shutdown(self):
        if self.worker_thread and self.worker_thread.isRunning():
            self.worker_thread.quit()
            self.worker_thread.wait(2000)
