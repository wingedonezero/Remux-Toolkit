# remux_toolkit/tools/mkv_splitter/mkv_splitter_gui.py

import os
from PyQt6 import QtWidgets, QtCore
from . import mkv_splitter_core as core
from . import mkv_splitter_config as config

class Worker(QtCore.QThread):
    result = QtCore.pyqtSignal(str, str)
    error = QtCore.pyqtSignal(str)

    def __init__(self, file_path, min_duration, num_episodes, analysis_mode, target_duration):
        super().__init__()
        self.file_path = file_path; self.min_duration = min_duration; self.num_episodes = num_episodes
        self.analysis_mode = analysis_mode; self.target_duration = target_duration

    def run(self):
        try:
            mkv_info, error = core.get_chapter_info(self.file_path)
            if error: raise RuntimeError(error)
            log, command = core.analyze_chapters(mkv_info, self.min_duration, self.num_episodes, self.analysis_mode, self.target_duration, self.file_path)
            self.result.emit(log, command)
        except Exception as e:
            self.error.emit(str(e))

class MKVSplitterWidget(QtWidgets.QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'mkv_splitter'
        self.worker = None
        self._init_ui()
        self._load_settings()

    def _init_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        # --- Input Group ---
        input_group = QtWidgets.QGroupBox("Input File")
        input_layout = QtWidgets.QHBoxLayout(input_group)
        self.file_path_input = QtWidgets.QLineEdit()
        self.file_path_input.setPlaceholderText("Select or paste MKV file path...")
        browse_btn = QtWidgets.QPushButton("Browse...")
        browse_btn.clicked.connect(self._select_file)
        input_layout.addWidget(self.file_path_input)
        input_layout.addWidget(browse_btn)
        layout.addWidget(input_group)

        # --- Analysis Group ---
        analysis_group = QtWidgets.QGroupBox("Analysis Configuration")
        analysis_layout = QtWidgets.QVBoxLayout(analysis_group)
        mode_layout = QtWidgets.QHBoxLayout()
        self.analysis_mode_combo = QtWidgets.QComboBox()
        self.analysis_modes = ["Time-based Grouping", "Pattern Recognition", "Statistical Gap Analysis", "Shortest Chapter Analysis", "Manual Episode Count"]
        self.analysis_mode_combo.addItems(self.analysis_modes)
        self.analysis_mode_combo.currentTextChanged.connect(self._on_mode_changed)
        mode_layout.addWidget(QtWidgets.QLabel("Analysis Mode:"))
        mode_layout.addWidget(self.analysis_mode_combo)
        analysis_layout.addLayout(mode_layout)

        self.params_stack = QtWidgets.QStackedWidget()
        self.target_duration_input = QtWidgets.QDoubleSpinBox(); self.target_duration_input.setSuffix(" min"); self.target_duration_input.setRange(1, 240)
        self.min_duration_input = QtWidgets.QDoubleSpinBox(); self.min_duration_input.setSuffix(" min"); self.min_duration_input.setRange(1, 240)
        self.num_episodes_input = QtWidgets.QSpinBox(); self.num_episodes_input.setRange(2, 100)

        param_layout1 = QtWidgets.QFormLayout(); param_layout1.addRow("Target Episode Duration:", self.target_duration_input); w1 = QtWidgets.QWidget(); w1.setLayout(param_layout1)
        param_layout2 = QtWidgets.QFormLayout(); param_layout2.addRow("Min Content Duration:", self.min_duration_input); w2 = QtWidgets.QWidget(); w2.setLayout(param_layout2)
        param_layout3 = QtWidgets.QFormLayout(); param_layout3.addRow("Expected # of Episodes:", self.num_episodes_input); w3 = QtWidgets.QWidget(); w3.setLayout(param_layout3)

        self.params_stack.addWidget(w1); self.params_stack.addWidget(w2); self.params_stack.addWidget(w3)
        analysis_layout.addWidget(self.params_stack)
        layout.addWidget(analysis_group)

        # --- Results Group ---
        results_group = QtWidgets.QGroupBox("Results")
        results_layout = QtWidgets.QFormLayout(results_group)
        self.analyze_button = QtWidgets.QPushButton("Analyze File")
        self.analyze_button.clicked.connect(self.start_analysis)
        self.analysis_log_output = QtWidgets.QTextEdit(); self.analysis_log_output.setReadOnly(True)
        self.final_command_output = QtWidgets.QLineEdit(); self.final_command_output.setReadOnly(True)

        command_row = QtWidgets.QHBoxLayout()
        copy_btn = QtWidgets.QPushButton("Copy"); copy_btn.clicked.connect(self._copy_command)
        execute_btn = QtWidgets.QPushButton("Execute"); execute_btn.clicked.connect(self._execute_command)
        command_row.addWidget(self.final_command_output); command_row.addWidget(copy_btn); command_row.addWidget(execute_btn)

        results_layout.addRow(self.analyze_button)
        results_layout.addRow("Analysis Log:", self.analysis_log_output)
        results_layout.addRow("Generated Command:", command_row)
        layout.addWidget(results_group)

        self._on_mode_changed(self.analysis_modes[0])

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
            self.analysis_log_output.setText("Error: Please select a valid MKV file.")
            return

        self.analysis_log_output.setText("Analyzing, please wait...")
        self.final_command_output.clear()
        self.analyze_button.setEnabled(False)

        self.worker = Worker(path, self.min_duration_input.value(), self.num_episodes_input.value(), self.analysis_mode_combo.currentText(), self.target_duration_input.value())
        self.worker.result.connect(self._on_analysis_result)
        self.worker.error.connect(self._on_analysis_error)
        self.worker.finished.connect(lambda: self.analyze_button.setEnabled(True))
        self.worker.start()

    def _on_analysis_result(self, log, command):
        self.analysis_log_output.setText(log)
        self.final_command_output.setText(command)

    def _on_analysis_error(self, error_msg):
        self.analysis_log_output.setText(error_msg)

    def _copy_command(self):
        if self.final_command_output.text():
            QtWidgets.QApplication.clipboard().setText(self.final_command_output.text())

    def _execute_command(self):
        command = self.final_command_output.text()
        if not command:
            self.analysis_log_output.append("\nNo command to execute.")
            return

        # For safety, we'll just log that we would run it. A real implementation would use subprocess.
        self.analysis_log_output.append(f"\n--- EXECUTION STUB ---\nWould run command:\n{command}")
        QtWidgets.QMessageBox.information(self, "Execution", "Command execution is a planned feature. For now, the command has been logged. Please run it manually.")

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
        if self.worker and self.worker.isRunning():
            self.worker.quit()
            self.worker.wait()
