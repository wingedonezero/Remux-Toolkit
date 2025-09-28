# remux_toolkit/tools/mkv_splitter/mkv_splitter_gui.py

import os
import shlex
import subprocess
from PyQt6 import QtWidgets, QtCore, QtGui

from . import mkv_splitter_core as core
from . import mkv_splitter_config as config

class AnalysisWorker(QtCore.QThread):
    """Worker to handle the file analysis in the background."""
    # Emits: mkv_info dict, analysis log string, split_points list
    result = QtCore.pyqtSignal(dict, str, list)
    error = QtCore.pyqtSignal(str)

    def __init__(self, file_path, min_duration, num_episodes, analysis_mode, target_duration):
        super().__init__()
        self.file_path = file_path
        self.min_duration = min_duration
        self.num_episodes = num_episodes
        self.analysis_mode = analysis_mode
        self.target_duration = target_duration

    def run(self):
        try:
            mkv_info, error = core.get_mkv_info(self.file_path)
            if error:
                raise RuntimeError(error)

            log, split_points = core.analyze_chapters(
                mkv_info, self.min_duration, self.num_episodes,
                self.analysis_mode, self.target_duration
            )
            self.result.emit(mkv_info, log, split_points)
        except Exception as e:
            self.error.emit(str(e))

class ExecutionWorker(QtCore.QThread):
    """Worker to execute the mkvmerge command and stream its output."""
    line_ready = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal(int)

    def __init__(self, command, parent=None):
        super().__init__(parent)
        self.command = command

    def run(self):
        try:
            # Popen with settings for real-time text output
            proc = subprocess.Popen(
                shlex.split(self.command),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1
            )
            # Read output line by line as it comes in
            for line in iter(proc.stdout.readline, ''):
                self.line_ready.emit(line.strip())

            proc.stdout.close()
            return_code = proc.wait()
            self.finished.emit(return_code)
        except Exception as e:
            self.line_ready.emit(f"FATAL EXECUTION ERROR: {e}")
            self.finished.emit(-1)

class MKVSplitterWidget(QtWidgets.QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'mkv_splitter'
        self.analysis_worker = None
        self.execution_worker = None
        self.analysis_results = {}
        self._init_ui()
        self._load_settings()

    def _init_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        main_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)

        # --- Top Pane (Inputs & Analysis) ---
        top_pane = QtWidgets.QWidget()
        top_layout = QtWidgets.QVBoxLayout(top_pane)

        input_group = QtWidgets.QGroupBox("Input File")
        input_layout = QtWidgets.QHBoxLayout(input_group)
        self.file_path_input = QtWidgets.QLineEdit()
        self.file_path_input.setPlaceholderText("Select or paste MKV file path...")
        browse_btn = QtWidgets.QPushButton("Browse...")
        browse_btn.clicked.connect(self._select_file)
        input_layout.addWidget(self.file_path_input)
        input_layout.addWidget(browse_btn)
        top_layout.addWidget(input_group)

        analysis_group = QtWidgets.QGroupBox("Analysis Configuration")
        analysis_layout = QtWidgets.QFormLayout(analysis_group)
        self.analysis_mode_combo = QtWidgets.QComboBox()
        self.analysis_modes = ["Time-based Grouping", "Pattern Recognition", "Statistical Gap Analysis", "Shortest Chapter Analysis", "Manual Episode Count"]
        self.analysis_mode_combo.addItems(self.analysis_modes)
        self.analysis_mode_combo.currentTextChanged.connect(self._on_mode_changed)
        analysis_layout.addRow("Analysis Mode:", self.analysis_mode_combo)

        self.params_stack = QtWidgets.QStackedWidget()
        self.target_duration_input = QtWidgets.QDoubleSpinBox(); self.target_duration_input.setSuffix(" min"); self.target_duration_input.setRange(1, 240)
        self.min_duration_input = QtWidgets.QDoubleSpinBox(); self.min_duration_input.setSuffix(" min"); self.min_duration_input.setRange(1, 240)
        self.num_episodes_input = QtWidgets.QSpinBox(); self.num_episodes_input.setRange(2, 100)

        param_layout1 = QtWidgets.QFormLayout(); param_layout1.addRow("Target Episode Duration:", self.target_duration_input); w1 = QtWidgets.QWidget(); w1.setLayout(param_layout1)
        param_layout2 = QtWidgets.QFormLayout(); param_layout2.addRow("Min Content Duration:", self.min_duration_input); w2 = QtWidgets.QWidget(); w2.setLayout(param_layout2)
        param_layout3 = QtWidgets.QFormLayout(); param_layout3.addRow("Expected # of Episodes:", self.num_episodes_input); w3 = QtWidgets.QWidget(); w3.setLayout(param_layout3)

        self.params_stack.addWidget(w1); self.params_stack.addWidget(w2); self.params_stack.addWidget(w3)
        analysis_layout.addRow(self.params_stack)

        self.analyze_button = QtWidgets.QPushButton("Analyze File")
        self.analyze_button.clicked.connect(self.start_analysis)
        analysis_layout.addRow(self.analyze_button)
        top_layout.addWidget(analysis_group)
        main_splitter.addWidget(top_pane)

        # --- Bottom Pane (Results) ---
        bottom_pane = QtWidgets.QWidget()
        bottom_layout = QtWidgets.QVBoxLayout(bottom_pane)

        results_group = QtWidgets.QGroupBox("Results")
        results_layout = QtWidgets.QVBoxLayout(results_group)

        results_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)

        tracks_widget = QtWidgets.QWidget()
        tracks_layout = QtWidgets.QVBoxLayout(tracks_widget)
        tracks_layout.setContentsMargins(0,0,0,0)
        tracks_layout.addWidget(QtWidgets.QLabel("Detected Tracks:"))
        self.track_table = QtWidgets.QTableWidget()
        self.track_table.setColumnCount(4)
        self.track_table.setHorizontalHeaderLabels(["ID", "Type", "Codec", "Language"])
        self.track_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.track_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.track_table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.track_table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.track_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        tracks_layout.addWidget(self.track_table)
        results_splitter.addWidget(tracks_widget)

        log_widget = QtWidgets.QWidget()
        log_layout = QtWidgets.QVBoxLayout(log_widget)
        log_layout.setContentsMargins(0,0,0,0)
        log_layout.addWidget(QtWidgets.QLabel("Log / Output:"))
        self.log_output = QtWidgets.QPlainTextEdit(); self.log_output.setReadOnly(True)
        log_layout.addWidget(self.log_output)
        results_splitter.addWidget(log_widget)

        results_splitter.setSizes([300, 400])
        results_layout.addWidget(results_splitter)

        command_row = QtWidgets.QHBoxLayout()
        self.final_command_output = QtWidgets.QLineEdit(); self.final_command_output.setReadOnly(True)
        self.generate_btn = QtWidgets.QPushButton("Generate Command"); self.generate_btn.clicked.connect(self._generate_command)
        self.execute_btn = QtWidgets.QPushButton("Execute Command"); self.execute_btn.clicked.connect(self._execute_command)
        copy_btn = QtWidgets.QPushButton("Copy"); copy_btn.clicked.connect(self._copy_command)

        command_row.addWidget(self.final_command_output, 1)
        command_row.addWidget(self.generate_btn)
        command_row.addWidget(self.execute_btn)
        command_row.addWidget(copy_btn)

        form_layout = QtWidgets.QFormLayout()
        form_layout.addRow("Generated Command:", command_row)
        results_layout.addLayout(form_layout)

        bottom_layout.addWidget(results_group)
        main_splitter.addWidget(bottom_pane)

        layout.addWidget(main_splitter)
        self._on_mode_changed(self.analysis_modes[0])

    def _set_controls_enabled(self, enabled):
        """Helper to enable/disable controls during operations."""
        self.analyze_button.setEnabled(enabled)
        self.generate_btn.setEnabled(enabled)
        self.execute_btn.setEnabled(enabled)

    def _on_mode_changed(self, mode):
        if mode == "Time-based Grouping": self.params_stack.setCurrentIndex(0)
        elif mode == "Manual Episode Count": self.params_stack.setCurrentIndex(2)
        else: self.params_stack.setCurrentIndex(1)

    def _select_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select MKV File", "", "MKV Files (*.mkv)")
        if path: self.file_path_input.setText(path)

    def start_analysis(self):
        path = self.file_path_input.text()
        if not path or not os.path.exists(path):
            self.log_output.setPlainText("Error: Please select a valid MKV file.")
            return

        self.log_output.setPlainText("Analyzing, please wait...")
        self.final_command_output.clear()
        self.track_table.setRowCount(0)
        self._set_controls_enabled(False)

        self.analysis_worker = AnalysisWorker(
            path, self.min_duration_input.value(),
            self.num_episodes_input.value(),
            self.analysis_mode_combo.currentText(),
            self.target_duration_input.value()
        )
        self.analysis_worker.result.connect(self._on_analysis_result)
        self.analysis_worker.error.connect(self._on_analysis_error)
        self.analysis_worker.finished.connect(lambda: self._set_controls_enabled(True))
        self.analysis_worker.start()

    def _on_analysis_result(self, mkv_info, log, split_points):
        self.analysis_results = {'mkv_info': mkv_info, 'split_points': split_points}
        self.log_output.setPlainText(log)
        self._populate_track_table(mkv_info.get('tracks', []))
        self._generate_command()

    def _populate_track_table(self, tracks):
        self.track_table.setRowCount(0)
        for track in tracks:
            row = self.track_table.rowCount()
            self.track_table.insertRow(row)

            tid = track.get('id', -1)
            ttype = track.get('type', 'unknown').capitalize()
            codec = track.get('codec', 'N/A')
            lang = track.get('properties', {}).get('language', 'und')

            id_item = QtWidgets.QTableWidgetItem(str(tid)); id_item.setFlags(id_item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
            self.track_table.setItem(row, 0, id_item)
            type_item = QtWidgets.QTableWidgetItem(ttype); type_item.setFlags(type_item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
            self.track_table.setItem(row, 1, type_item)
            codec_item = QtWidgets.QTableWidgetItem(codec); codec_item.setFlags(codec_item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
            self.track_table.setItem(row, 2, codec_item)
            lang_item = QtWidgets.QTableWidgetItem(lang)
            self.track_table.setItem(row, 3, lang_item)

    def _on_analysis_error(self, error_msg):
        self.log_output.setPlainText(error_msg)

    def _generate_command(self):
        if not self.analysis_results: return
        track_mods = []
        original_tracks = self.analysis_results.get('mkv_info', {}).get('tracks', [])
        for row in range(self.track_table.rowCount()):
            try:
                tid = int(self.track_table.item(row, 0).text())
                new_lang = self.track_table.item(row, 3).text().strip()
                original_lang = next((t.get('properties', {}).get('language', 'und') for t in original_tracks if t.get('id') == tid), 'und')
                if new_lang != original_lang:
                    track_mods.append({'tid': tid, 'language': new_lang})
            except (ValueError, AttributeError): continue

        command = core.generate_mkvmerge_command(
            self.file_path_input.text(),
            self.analysis_results.get('split_points', []),
            track_mods
        )
        self.final_command_output.setText(command)

    def _execute_command(self):
        command = self.final_command_output.text()
        if not command:
            self.log_output.setPlainText("No command to execute. Please analyze a file and generate a command first.")
            return

        self.log_output.setPlainText(f"--- EXECUTING COMMAND ---\n{command}\n\n")
        self._set_controls_enabled(False)

        self.execution_worker = ExecutionWorker(command)
        self.execution_worker.line_ready.connect(lambda line: self.log_output.appendPlainText(line))
        self.execution_worker.finished.connect(self._on_execution_finished)
        self.execution_worker.start()

    def _on_execution_finished(self, return_code):
        self.log_output.appendPlainText(f"\n--- EXECUTION FINISHED (Exit Code: {return_code}) ---")
        self._set_controls_enabled(True)
        self.execution_worker = None

    def _copy_command(self):
        if self.final_command_output.text():
            QtWidgets.QApplication.clipboard().setText(self.final_command_output.text())

    def _load_settings(self):
        settings = self.app_manager.load_config(self.tool_name, config.DEFAULTS)
        self.file_path_input.setText(settings.get('file_path', ''))
        self.analysis_mode_combo.setCurrentText(settings.get('analysis_mode', config.DEFAULTS['analysis_mode']))
        self.target_duration_input.setValue(settings.get('target_duration', config.DEFAULTS['target_duration']))
        self.min_duration_input.setValue(settings.get('min_duration', config.DEFAULTS['min_duration']))
        self.num_episodes_input.setValue(settings.get('num_episodes', config.DEFAULTS['num_episodes']))

    def save_settings(self):
        settings = {
            'file_path': self.file_path_input.text(),
            'analysis_mode': self.analysis_mode_combo.currentText(),
            'target_duration': self.target_duration_input.value(),
            'min_duration': self.min_duration_input.value(),
            'num_episodes': self.num_episodes_input.value(),
        }
        self.app_manager.save_config(self.tool_name, settings)

    def shutdown(self):
        # Terminate any running workers on shutdown
        if self.analysis_worker and self.analysis_worker.isRunning():
            self.analysis_worker.terminate()
            self.analysis_worker.wait()
        if self.execution_worker and self.execution_worker.isRunning():
            self.execution_worker.terminate()
            self.execution_worker.wait()
