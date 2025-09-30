# remux_toolkit/tools/video_ab_comparator/video_ab_comparator_gui.py

from PyQt6 import QtWidgets, QtCore, QtGui
import json
from .core.pipeline import ComparisonPipeline
from .gui.results_widget import ResultsWidget
from .gui.settings_dialog import SettingsDialog
from .video_ab_comparator_config import DEFAULTS

class VideoABComparatorWidget(QtWidgets.QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'video_ab_comparator'
        self.pipeline_thread = None
        self.pipeline = None
        self.results_data = None
        self.settings = self.app_manager.load_config(self.tool_name, DEFAULTS)
        self._init_ui()

    def _init_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        # --- Inputs ---
        input_group = QtWidgets.QGroupBox("Input Sources")
        input_layout = QtWidgets.QFormLayout(input_group)
        self.source_a_input = QtWidgets.QLineEdit()
        self.source_b_input = QtWidgets.QLineEdit()
        btn_a = QtWidgets.QPushButton("Browse...")
        btn_a.clicked.connect(lambda: self._select_file(self.source_a_input))
        btn_b = QtWidgets.QPushButton("Browse...")
        btn_b.clicked.connect(lambda: self._select_file(self.source_b_input))

        row_a = QtWidgets.QHBoxLayout(); row_a.addWidget(self.source_a_input); row_a.addWidget(btn_a)
        row_b = QtWidgets.QHBoxLayout(); row_b.addWidget(self.source_b_input); row_b.addWidget(btn_b)

        input_layout.addRow("Source A:", row_a)
        input_layout.addRow("Source B:", row_b)
        layout.addWidget(input_group)

        # --- Controls ---
        controls_layout = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("Start Comparison")
        self.start_button.clicked.connect(self.start_comparison)
        self.settings_button = QtWidgets.QPushButton("Settings...")
        self.settings_button.clicked.connect(self.open_settings)
        self.export_button = QtWidgets.QPushButton("Export to HTML")
        self.export_button.clicked.connect(self.export_html)
        self.export_button.setEnabled(False) # Disabled until results are ready

        controls_layout.addWidget(self.start_button)
        controls_layout.addWidget(self.settings_button)
        controls_layout.addStretch()
        controls_layout.addWidget(self.export_button)
        layout.addLayout(controls_layout)

        # --- Results ---
        self.results_tabs = QtWidgets.QTabWidget()
        self.results_widget = ResultsWidget()
        self.log_tab = QtWidgets.QTextEdit()
        self.log_tab.setReadOnly(True)

        self.results_tabs.addTab(self.results_widget, "Summary Report")
        self.results_tabs.addTab(self.log_tab, "Detailed Log")
        layout.addWidget(self.results_tabs, 1)

        self.progress_bar = QtWidgets.QProgressBar()
        layout.addWidget(self.progress_bar)

        # Connect the scorecard click signal
        self.results_widget.scorecard_tree.itemClicked.connect(self.on_scorecard_item_clicked)

    def _select_file(self, line_edit):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select Video File", "", "Video Files (*.mkv *.vob *.iso *.ts);;All Files (*)")
        if path:
            line_edit.setText(path)

    def open_settings(self):
        """Opens the settings dialog."""
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            self.settings = dialog.get_settings()
            self.app_manager.save_config(self.tool_name, self.settings)
            self.log_tab.append("Settings saved.")

    def export_html(self):
        """Exports the current results to a self-contained HTML file."""
        if not self.results_data:
            QtWidgets.QMessageBox.warning(self, "No Data", "Please run a comparison first.")
            return

        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Report", "", "HTML Files (*.html)")
        if not path:
            return

        # Basic HTML structure
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
        path_a = self.source_a_input.text()
        path_b = self.source_b_input.text()

        if not path_a or not path_b:
            self.log_tab.setText("Error: Please select both source files.")
            return

        self.start_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.log_tab.clear()
        self.results_widget.clear()

        self.pipeline = ComparisonPipeline(path_a, path_b)
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
        self.results_tabs.setCurrentWidget(self.results_widget)
        self.progress_bar.setValue(100)
        self.start_button.setEnabled(True)
        self.export_button.setEnabled(True)
        self.pipeline_thread.quit()
        self.pipeline_thread.wait()

    def on_scorecard_item_clicked(self, item, column):
        """Loads and displays frames when an issue is clicked."""
        if not self.pipeline or not self.results_data:
            return

        issue_name = item.text(0).strip()
        # Handle nested items from audio report
        if item.parent():
            issue_name = item.parent().text(0)

        issue_results = self.results_data.get("issues", {}).get(issue_name, {})

        ts_a = issue_results.get('a', {}).get('worst_frame_timestamp')
        if ts_a is None:
            self.results_widget.frame_a_label.setText("No specific frame\nfor this metric")
            self.results_widget.frame_b_label.setText("No specific frame\nfor this metric")
            return

        # Apply the alignment offset to get the corresponding time in video B
        time_offset = self.results_data.get("alignment_offset_secs", 0.0)
        ts_b = ts_a - time_offset # If B starts later, its timestamp will be smaller

        self.display_frames(ts_a, ts_b)

    def display_frames(self, ts_a, ts_b):
        """Fetches and displays the two frames in the viewer."""
        frame_a = self.pipeline.source_a.get_frame(ts_a)
        frame_b = self.pipeline.source_b.get_frame(ts_b)

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
            self.pipeline_thread.quit()
            self.pipeline_thread.wait()
