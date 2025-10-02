# remux_toolkit/tools/video_ab_comparator/video_ab_comparator_gui.py
from PyQt6 import QtWidgets, QtCore, QtGui
import json
import cv2
import numpy as np

from .core.pipeline import ComparisonPipeline
from .gui.results_widget import ResultsWidget
from .gui.settings_dialog import SettingsDialog
from .gui.detailed_comparison_widget import DetailedComparisonWidget
from .video_ab_comparator_config import DEFAULTS

class FrameLoader(QtCore.QObject):
    frames_ready = QtCore.pyqtSignal(object, object, float, float)

    def __init__(self, source_a, source_b):
        super().__init__()
        self.source_a = source_a
        self.source_b = source_b

    @QtCore.pyqtSlot(float, float)
    def load_frames(self, ts_a, ts_b):
        """Load frames for summary report (worst frames)."""
        frame_a = self.source_a.get_frame(ts_a, accurate=True)
        frame_b = self.source_b.get_frame(ts_b, accurate=True)
        self.frames_ready.emit(frame_a, frame_b, ts_a, ts_b)


class ChunkFrameLoader(QtCore.QObject):
    chunk_frames_ready = QtCore.pyqtSignal(int, int, object, object)

    def __init__(self, source_a, source_b, chunk_data):
        super().__init__()
        self.source_a = source_a
        self.source_b = source_b
        self.chunk_data = chunk_data

    @QtCore.pyqtSlot(int, int)
    def load_chunk_frames(self, chunk_idx, frame_idx):
        """Load specific frame from a chunk (frame-by-frame browsing)."""
        if chunk_idx < 0 or chunk_idx >= len(self.chunk_data):
            return

        chunk = self.chunk_data[chunk_idx]
        chunk_start_a = chunk['timestamp_a']
        chunk_start_b = chunk['timestamp_b']

        # Calculate timestamp for this specific frame (10fps = 0.1s per frame)
        frame_offset = frame_idx * 0.1
        ts_a = chunk_start_a + frame_offset
        ts_b = chunk_start_b + frame_offset

        frame_a = self.source_a.get_frame(ts_a, accurate=True)
        frame_b = self.source_b.get_frame(ts_b, accurate=True)

        self.chunk_frames_ready.emit(chunk_idx, frame_idx, frame_a, frame_b)


class VideoABComparatorWidget(QtWidgets.QWidget):
    request_frames = QtCore.pyqtSignal(float, float)

    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'video_ab_comparator'
        self.pipeline_thread = None
        self.pipeline = None
        self.frame_loader_thread = None
        self.frame_loader = None
        self.chunk_frame_loader_thread = None
        self.chunk_frame_loader = None
        self.results_data = None
        self.settings = self.app_manager.load_config(self.tool_name, DEFAULTS)
        self._init_ui()

    def _init_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        input_group = QtWidgets.QGroupBox("Input Sources")
        input_layout = QtWidgets.QFormLayout(input_group)
        self.source_a_input = QtWidgets.QLineEdit()
        self.source_b_input = QtWidgets.QLineEdit()

        # Load last used paths
        self.source_a_input.setText(self.settings.get('source_a_path', ''))
        self.source_b_input.setText(self.settings.get('source_b_path', ''))

        btn_a = QtWidgets.QPushButton("Browse...")
        btn_a.clicked.connect(lambda: self._select_file(self.source_a_input))
        btn_b = QtWidgets.QPushButton("Browse...")
        btn_b.clicked.connect(lambda: self._select_file(self.source_b_input))
        row_a = QtWidgets.QHBoxLayout(); row_a.addWidget(self.source_a_input); row_a.addWidget(btn_a)
        row_b = QtWidgets.QHBoxLayout(); row_b.addWidget(self.source_b_input); row_b.addWidget(btn_b)
        input_layout.addRow("Source A:", row_a)
        input_layout.addRow("Source B:", row_b)
        layout.addWidget(input_group)

        controls_layout = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("Start Comparison")
        self.start_button.clicked.connect(self.start_comparison)
        self.settings_button = QtWidgets.QPushButton("Settings...")
        self.settings_button.clicked.connect(self.open_settings)
        self.export_button = QtWidgets.QPushButton("Export to HTML")
        self.export_button.clicked.connect(self.export_html)
        self.export_button.setEnabled(False)
        controls_layout.addWidget(self.start_button)
        controls_layout.addWidget(self.settings_button)
        controls_layout.addStretch()
        controls_layout.addWidget(self.export_button)
        layout.addLayout(controls_layout)

        self.results_tabs = QtWidgets.QTabWidget()
        self.results_widget = ResultsWidget()
        self.detailed_comparison_widget = DetailedComparisonWidget()
        self.log_tab = QtWidgets.QTextEdit()
        self.log_tab.setReadOnly(True)

        self.results_tabs.addTab(self.results_widget, "Summary Report")
        self.results_tabs.addTab(self.detailed_comparison_widget, "Frame-by-Frame")
        self.results_tabs.addTab(self.log_tab, "Analysis Log")
        layout.addWidget(self.results_tabs, 1)

        self.progress_bar = QtWidgets.QProgressBar()
        layout.addWidget(self.progress_bar)

        # Connect signals
        self.results_widget.scorecard_tree.itemClicked.connect(self.on_scorecard_item_clicked)

    def _select_file(self, line_edit):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select Video File", "", "Video Files (*.mkv *.vob *.iso *.ts);;All Files (*)")
        if path:
            line_edit.setText(path)

    def open_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            self.settings = dialog.get_settings()
            self.app_manager.save_config(self.tool_name, self.settings)
            self.log_tab.append("Settings saved.")

    def export_html(self):
        if not self.results_data:
            QtWidgets.QMessageBox.warning(self, "No Data", "Please run a comparison first.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Report", "", "HTML Files (*.html)")
        if not path:
            return
        html = f"""
        <html><head><title>Video A/B Comparison Report</title>
        <style>
            body {{ font-family: sans-serif; }}
            table {{ border-collapse: collapse; width: 100%; }}
            th, td {{ border: 1px solid #dddddd; text-align: left; padding: 8px; }}
            tr:nth-child(even) {{ background-color: #f2f2f2; }}
        </style>
        </head>
        <body><h1>{self.results_data.get('verdict')}</h1>
        <h2>Source A: {self.results_data['source_a'].path}</h2>
        <h2>Source B: {self.results_data['source_b'].path}</h2>
        <hr>
        <table><tr><th>Metric</th><th>Source A</th><th>Source B</th></tr>
        """
        for issue, data in self.results_data.get('issues', {}).items():
            html += f"<tr><td><b>{issue}</b></td><td>{data['a']['summary']}</td><td>{data['b']['summary']}</td></tr>"
        html += "</table></body></html>"
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(html)
            self.log_tab.append(f"Report saved to {path}")
        except Exception as e:
            self.log_tab.append(f"Error saving report: {e}")

    def start_comparison(self):
        path_a, path_b = self.source_a_input.text(), self.source_b_input.text()
        if not path_a or not path_b:
            self.log_tab.setText("Error: Please select both source files.")
            return

        # Save paths to settings
        self.settings['source_a_path'] = path_a
        self.settings['source_b_path'] = path_b
        self.app_manager.save_config(self.tool_name, self.settings)

        self.start_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.log_tab.clear()
        self.results_widget.clear()
        self.detailed_comparison_widget.clear()

        # Get temp directory for this tool
        temp_dir = self.app_manager.get_temp_dir(self.tool_name)

        self.pipeline = ComparisonPipeline(path_a, path_b, self.settings, temp_dir)

        self.pipeline_thread = QtCore.QThread()
        self.pipeline.moveToThread(self.pipeline_thread)
        self.pipeline.progress.connect(self.update_progress)
        self.pipeline.finished.connect(self.on_finished)
        self.pipeline_thread.started.connect(self.pipeline.run)
        self.pipeline_thread.start()

    def update_progress(self, message, value):
        self.log_tab.append(message)
        self.progress_bar.setValue(value)

    def on_finished(self, results):
        self.results_data = results
        self.log_tab.append("\n--- Analysis Complete ---")
        self.results_widget.populate(results)

        # Setup frame loaders
        self._setup_frame_loader()

        # Load detailed comparison data
        temp_dir = results.get('temp_dir')
        if temp_dir:
            self.detailed_comparison_widget.load_chunk_data(temp_dir, results)
            self._setup_chunk_frame_loader(temp_dir)

        self.results_tabs.setCurrentWidget(self.results_widget)
        self.progress_bar.setValue(100)
        self.start_button.setEnabled(True)
        self.export_button.setEnabled(True)
        self.pipeline_thread.quit()
        self.pipeline_thread.wait()

    def _setup_frame_loader(self):
        """Setup loader for summary report frames (worst frames)."""
        if self.frame_loader_thread and self.frame_loader_thread.isRunning():
            self.frame_loader_thread.quit()
            self.frame_loader_thread.wait()

        self.frame_loader = FrameLoader(self.pipeline.source_a, self.pipeline.source_b)
        self.frame_loader_thread = QtCore.QThread()
        self.frame_loader.moveToThread(self.frame_loader_thread)
        self.request_frames.connect(self.frame_loader.load_frames)
        self.frame_loader.frames_ready.connect(self._update_frame_viewers)
        self.frame_loader_thread.start()

    def _setup_chunk_frame_loader(self, temp_dir):
        """Setup loader for detailed comparison frames (frame-by-frame browsing)."""
        if self.chunk_frame_loader_thread and self.chunk_frame_loader_thread.isRunning():
            self.chunk_frame_loader_thread.quit()
            self.chunk_frame_loader_thread.wait()

        # Load chunk data
        import json
        import os
        chunk_metadata_path = os.path.join(temp_dir, "chunk_metadata.json")

        if os.path.exists(chunk_metadata_path):
            with open(chunk_metadata_path, 'r') as f:
                chunk_data = json.load(f)

            self.chunk_frame_loader = ChunkFrameLoader(
                self.pipeline.source_a,
                self.pipeline.source_b,
                chunk_data
            )
            self.chunk_frame_loader_thread = QtCore.QThread()
            self.chunk_frame_loader.moveToThread(self.chunk_frame_loader_thread)

            # Connect signals
            self.detailed_comparison_widget.request_chunk_frames.connect(
                self.chunk_frame_loader.load_chunk_frames
            )
            self.chunk_frame_loader.chunk_frames_ready.connect(
                self.detailed_comparison_widget.display_chunk_frames
            )

            self.chunk_frame_loader_thread.start()

    def on_scorecard_item_clicked(self, item, column):
        if not self.pipeline or not self.results_data or not self.frame_loader:
            return

        issue_name = item.parent().text(0) if item.parent() else item.text(0)
        issue_results = self.results_data.get("issues", {}).get(issue_name, {})

        ts_a = issue_results.get('a', {}).get('worst_frame_timestamp')
        if ts_a is None:
            self.results_widget.frame_a_label.setText("No specific frame\nfor this metric")
            self.results_widget.frame_b_label.setText("No specific frame\nfor this metric")
            return

        ts_b = self.results_widget.map_ts_b(ts_a)
        self.display_frames(ts_a, ts_b)

    def display_frames(self, ts_a, ts_b):
        self.results_widget.frame_a_label.setText(f"Loading frame at {ts_a:.2f}s...")
        self.results_widget.frame_b_label.setText(f"Loading frame at {ts_b:.2f}s...")
        self.request_frames.emit(ts_a, ts_b)

    @QtCore.pyqtSlot(object, object, float, float)
    def _update_frame_viewers(self, frame_a, frame_b, ts_a, ts_b):
        if frame_a is not None:
            h, w, ch = frame_a.shape
            q_img = QtGui.QImage(frame_a.data, w, h, ch * w, QtGui.QImage.Format.Format_BGR888)
            pixmap = QtGui.QPixmap.fromImage(q_img)
            self.results_widget.frame_a_label.setPixmap(pixmap.scaled(self.results_widget.frame_a_label.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation))
        else:
            self.results_widget.frame_a_label.setText(f"Frame A\n(Could not load at {ts_a:.2f}s)")

        if frame_b is not None:
            h, w, ch = frame_b.shape
            q_img = QtGui.QImage(frame_b.data, w, h, ch * w, QtGui.QImage.Format.Format_BGR888)
            pixmap = QtGui.QPixmap.fromImage(q_img)
            self.results_widget.frame_b_label.setPixmap(pixmap.scaled(self.results_widget.frame_b_label.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation))
        else:
            self.results_widget.frame_b_label.setText(f"Frame B\n(Could not load at {ts_b:.2f}s)")

    def shutdown(self):
        if self.pipeline_thread and self.pipeline_thread.isRunning():
            if self.pipeline:
                self.pipeline.stop()
            self.pipeline_thread.quit()
            self.pipeline_thread.wait()
        if self.frame_loader_thread and self.frame_loader_thread.isRunning():
            self.frame_loader_thread.quit()
            self.frame_loader_thread.wait()
        if self.chunk_frame_loader_thread and self.chunk_frame_loader_thread.isRunning():
            self.chunk_frame_loader_thread.quit()
            self.chunk_frame_loader_thread.wait()

    def save_settings(self):
        """Called when tab is closed to save settings."""
        self.app_manager.save_config(self.tool_name, self.settings)
