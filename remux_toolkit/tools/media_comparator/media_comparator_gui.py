# remux_toolkit/tools/media_comparator/media_comparator_gui.py
from PyQt6 import QtWidgets, QtCore, QtGui
from . import media_comparator_core as core
from . import media_comparator_config as config

class MediaComparatorWidget(QtWidgets.QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'media_comparator'
        self.thread = None
        self.worker = None
        self._init_ui()
        self._check_dependencies()
        self._load_settings()

    def _init_ui(self):
        # This part of the code is unchanged and is provided for completeness.
        self.layout = QtWidgets.QVBoxLayout(self)
        self._create_file_inputs()
        action_layout = QtWidgets.QHBoxLayout()
        action_layout.addWidget(QtWidgets.QLabel("Action Type:"))
        self.action_selector = QtWidgets.QComboBox()
        self.action_selector.addItems(["Compare Streams", "Analyze Container Delay", "Aligned Audio Comparison"])
        action_layout.addWidget(self.action_selector, 1)
        self.layout.addLayout(action_layout)
        self.options_stack = QtWidgets.QStackedWidget()
        self.options_stack.addWidget(self._create_compare_panel())
        self.options_stack.addWidget(self._create_analysis_panel())
        self.options_stack.addWidget(self._create_aligned_audio_panel())
        self.layout.addWidget(self.options_stack)
        self.action_selector.currentIndexChanged.connect(self.options_stack.setCurrentIndex)
        self.run_button = QtWidgets.QPushButton("Run Action")
        self.run_button.clicked.connect(self.start_action)
        self.layout.addWidget(self.run_button)
        self._create_progress_and_report()

    # --- NEW SLOTS FOR SAFER UI UPDATES ---
    def _update_progress(self, value):
        if self.progress_bar:
            self.progress_bar.setValue(value)

    def _update_report(self, report_lines):
        if self.report_text:
            self.report_text.setText("\n".join(report_lines))

    def _on_worker_finished(self):
        self.set_controls_enabled(True)
        if self.progress_bar:
            self.progress_bar.setVisible(False)

        # Clean up the thread and worker
        if self.thread:
            self.thread.quit()
            self.thread.wait()
        self.thread = None
        self.worker = None

    def start_action(self):
        if self.thread and self.thread.isRunning():
            return

        file1 = self.file1_input.text().strip()
        self.report_text.setText("Starting action...")
        self.progress_bar.setValue(0)
        self.set_controls_enabled(False)

        self.thread = QtCore.QThread()
        self.worker = core.Worker()

        action_index = self.action_selector.currentIndex()
        if action_index == 0:
            file2 = self.file2_input.text().strip()
            if not file1 or not file2:
                self.report_text.setText("Error: 'Compare Streams' requires both files."); self.set_controls_enabled(True); return
            self.progress_bar.setVisible(True)
            self._start_comparison_task(file1, file2)
        elif action_index == 1:
            if not file1:
                self.report_text.setText("Error: 'Analyze' requires at least File 1."); self.set_controls_enabled(True); return
            self.progress_bar.setVisible(False)
            self._start_analysis_task(file1)
        elif action_index == 2:
            file2 = self.file2_input.text().strip()
            if not file1 or not file2:
                self.report_text.setText("Error: 'Aligned Audio' requires both files."); self.set_controls_enabled(True); return
            self.progress_bar.setVisible(False)
            self._start_aligned_audio_task(file1, file2)

        self.worker.moveToThread(self.thread)

        # Connect signals to our new, safe slots
        self.worker.progress_updated.connect(self._update_progress)
        self.worker.report_ready.connect(self._update_report)
        self.worker.finished.connect(self._on_worker_finished)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)

        self.thread.start()

    def shutdown(self):
        """A safer shutdown method."""
        if self.worker and self.thread and self.thread.isRunning():
            # Disconnect signals to prevent updates to deleted widgets
            try:
                self.worker.progress_updated.disconnect(self._update_progress)
                self.worker.report_ready.disconnect(self._update_report)
            except (TypeError, RuntimeError): # Signals might already be disconnected
                pass

            # Ask the worker to stop its loops and quit the thread
            self.worker.stop()
            self.thread.quit()
            # Wait for a reasonable time for the thread to finish
            if not self.thread.wait(3000):
                print("Thread did not stop gracefully, terminating...")
                self.thread.terminate()
                self.thread.wait()

        self.thread = None
        self.worker = None

    # The rest of the file (_create_file_inputs, _create_compare_panel, _load_settings, etc.)
    # is unchanged from the version you already have and is provided here for completeness.
    # ... (all other methods remain the same) ...

    def _check_dependencies(self):
        ok, msg = core.check_dependencies()
        if not ok:
            self.report_text.setText(msg)
            self.run_button.setEnabled(False)
            self.action_selector.setEnabled(False)

    def set_controls_enabled(self, enabled):
        self.action_selector.setEnabled(enabled)
        self.options_stack.setEnabled(enabled)
        self.run_button.setEnabled(enabled)
        self.file1_input.setEnabled(enabled)
        self.file2_input.setEnabled(enabled)

    def _create_file_inputs(self):
        group = QtWidgets.QGroupBox("Input Files")
        layout = QtWidgets.QFormLayout(group)
        self.file1_input = QtWidgets.QLineEdit()
        self.file2_input = QtWidgets.QLineEdit()
        layout.addRow("File 1 Path:", self.file1_input)
        layout.addRow("File 2 Path:", self.file2_input)
        self.layout.addWidget(group)

    def _create_compare_panel(self):
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QFormLayout(panel)
        self.compare_stream_type = QtWidgets.QComboBox()
        self.compare_stream_type.addItems(["All", "Video", "Audio", "Subtitle"])
        self.compare_hash_method = QtWidgets.QComboBox()
        self.compare_hash_method.addItems(["Stream Copy", "Full Decode", "Streamhash Muxer", "Raw In-Memory Hash"])
        layout.addRow("Stream Type:", self.compare_stream_type)
        layout.addRow("Hashing Method:", self.compare_hash_method)
        return panel

    def _create_analysis_panel(self):
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QFormLayout(panel)
        self.analysis_stream_type = QtWidgets.QComboBox()
        self.analysis_stream_type.addItems(["Video", "Audio", "Subtitle"])
        layout.addRow("Stream Type:", self.analysis_stream_type)
        return panel

    def _create_aligned_audio_panel(self):
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        defaults = config.DEFAULTS
        r1 = QtWidgets.QHBoxLayout(); r1.addWidget(QtWidgets.QLabel("Audio stream index (File 1):")); self.align_idx1 = QtWidgets.QSpinBox(); self.align_idx1.setRange(0, 999); self.align_idx1.setValue(defaults['align_idx1']); r1.addWidget(self.align_idx1); r1.addWidget(QtWidgets.QLabel("Audio stream index (File 2):")); self.align_idx2 = QtWidgets.QSpinBox(); self.align_idx2.setRange(0, 999); self.align_idx2.setValue(defaults['align_idx2']); r1.addWidget(self.align_idx2); r1.addStretch()
        r2 = QtWidgets.QHBoxLayout(); self.align_auto = QtWidgets.QComboBox(); self.align_auto.addItems(["Auto-detect offset", "Manual offset"]); r2.addWidget(self.align_auto); r2.addWidget(QtWidgets.QLabel("Trim from:")); self.align_apply_to = QtWidgets.QComboBox(); self.align_apply_to.addItems(["File 1", "File 2"]); r2.addWidget(self.align_apply_to); r2.addWidget(QtWidgets.QLabel("Align Offset (ms):")); self.align_offset_ms = QtWidgets.QSpinBox(); self.align_offset_ms.setRange(0, 120000); self.align_offset_ms.setValue(defaults['align_offset_ms']); r2.addWidget(self.align_offset_ms)
        r3 = QtWidgets.QFormLayout(); self.align_norm_sr = QtWidgets.QLineEdit(); self.align_norm_sr.setText(defaults['align_norm_sr']); self.align_norm_ch = QtWidgets.QLineEdit(); self.align_norm_ch.setPlaceholderText("blank=keep"); self.align_norm_ch.setText(defaults['align_norm_ch']); r3.addRow("Normalize SR (Hz):", self.align_norm_sr); r3.addRow("Normalize Ch:", self.align_norm_ch)
        r4_group = QtWidgets.QGroupBox("Auto-Detect Settings"); r4 = QtWidgets.QFormLayout(r4_group); self.align_win_ms = QtWidgets.QSpinBox(); self.align_win_ms.setRange(200, 20000); self.align_win_ms.setValue(defaults['align_win_ms']); self.align_noise_db = QtWidgets.QSpinBox(); self.align_noise_db.setRange(-120, 0); self.align_noise_db.setValue(defaults['align_noise_db']); self.align_min_gap = QtWidgets.QSpinBox(); self.align_min_gap.setRange(0, 1000); self.align_min_gap.setValue(defaults['align_min_gap']); r4.addRow("Detection window (ms):", self.align_win_ms); r4.addRow("Threshold (dB):", self.align_noise_db); r4.addRow("Min-gap (ms):", self.align_min_gap)
        layout.addLayout(r1); layout.addLayout(r2); layout.addLayout(r3); layout.addWidget(r4_group)
        return panel

    def _create_progress_and_report(self):
        self.progress_bar = QtWidgets.QProgressBar(); self.progress_bar.setVisible(False)
        self.layout.addWidget(self.progress_bar)
        self.report_text = QtWidgets.QTextEdit(); self.report_text.setReadOnly(True); self.report_text.setText("Awaiting action...")
        self.layout.addWidget(self.report_text, 1)

    def _start_comparison_task(self, file1, file2):
        self.worker.file1_path = file1
        self.worker.file2_path = file2

        stream_type = self.compare_stream_type.currentText().lower()
        self.worker.stream_type_filter = None if stream_type == "all" else stream_type

        method = self.compare_hash_method.currentText()
        self.worker.method_name = method

        method_map = {
            "Stream Copy": core.get_stream_hash_copied,
            "Full Decode": core.get_stream_hash_decoded,
            "Streamhash Muxer": core.get_stream_hash_streamhash,
            "Raw In-Memory Hash": None # Special case handled in worker
        }
        self.worker.hash_function = method_map[method]
        self.thread.started.connect(self.worker.run_full_comparison)
