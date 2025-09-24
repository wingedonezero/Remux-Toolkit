# remux_toolkit/tools/media_comparator/media_comparator_gui.py
from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtCore import Q_ARG # Import Q_ARG
from . import media_comparator_core as core
from . import media_comparator_config as config

class MediaComparatorWidget(QtWidgets.QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'media_comparator'
        self.worker = None
        self.thread = None
        self._init_ui()
        self._check_dependencies()
        self._load_settings()
        self._setup_worker()

    def _init_ui(self):
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

    def _setup_worker(self):
        """Creates and starts the single, persistent worker thread."""
        self.thread = QtCore.QThread(self)
        self.worker = core.Worker()
        self.worker.moveToThread(self.thread)

        self.worker.progress_updated.connect(self.progress_bar.setValue)
        self.worker.report_ready.connect(lambda r: self.report_text.setText("\n".join(r)))
        self.worker.finished.connect(self.on_worker_finished)

        self.thread.start()

    def start_action(self):
        if self.thread is None or not self.thread.isRunning():
            self._setup_worker()

        self.set_controls_enabled(False)
        self.report_text.setText("Starting action...")
        params = self._gather_params()
        if not params:
            self.set_controls_enabled(True)
            return

        if params.get('action') == 'compare':
             self.progress_bar.setVisible(True)
        else:
             self.progress_bar.setVisible(False)

        # --- THIS IS THE FIX ---
        # Use Q_ARG to correctly wrap the dictionary for the slot.
        QtCore.QMetaObject.invokeMethod(
            self.worker, "start_job", QtCore.Qt.ConnectionType.QueuedConnection,
            Q_ARG(dict, params)
        )
        # --- END FIX ---

    def on_worker_finished(self):
        self.set_controls_enabled(True)
        self.progress_bar.setVisible(False)

    def shutdown(self):
        """A safe shutdown method for the single worker model."""
        if self.thread and self.thread.isRunning():
            if self.worker:
                self.worker.stop()
            self.thread.quit()
            if not self.thread.wait(3000):
                self.thread.terminate()
                self.thread.wait()
        self.thread = None
        self.worker = None

    def _gather_params(self) -> dict | None:
        """Gathers all UI parameters into a dictionary for the worker."""
        params = {}
        file1 = self.file1_input.text().strip()
        file2 = self.file2_input.text().strip()

        action_index = self.action_selector.currentIndex()
        if action_index == 0: # Compare Streams
            if not file1 or not file2:
                self.report_text.setText("Error: 'Compare Streams' requires both files."); return None
            params['action'] = 'compare'
            stream_type = self.compare_stream_type.currentText().lower()
            params['stream_type_filter'] = None if stream_type == "all" else stream_type
            method = self.compare_hash_method.currentText()
            params['method_name'] = method
            method_map = {
                "Stream Copy": core.get_stream_hash_copied, "Full Decode": core.get_stream_hash_decoded,
                "Streamhash Muxer": core.get_stream_hash_streamhash, "Raw In-Memory Hash": None
            }
            params['hash_function'] = method_map[method]

        elif action_index == 1: # Analyze Delay
            if not file1:
                self.report_text.setText("Error: 'Analyze' requires at least File 1."); return None
            params['action'] = 'analyze'
            params['stream_type_filter'] = self.analysis_stream_type.currentText().lower()

        elif action_index == 2: # Aligned Audio
            if not file1 or not file2:
                self.report_text.setText("Error: 'Aligned Audio' requires both files."); return None
            params['action'] = 'align_audio'
            params['align_apply_to'] = "file1" if self.align_apply_to.currentText() == "File 1" else "file2"
            params['align_auto'] = (self.align_auto.currentIndex() == 0)
            sr_txt = (self.align_norm_sr.text().strip() or ""); params['align_norm_sr'] = int(sr_txt) if sr_txt else 48000
            ch_txt = (self.align_norm_ch.text().strip() or ""); params['align_norm_ch'] = int(ch_txt) if ch_txt else None
            for key in ['align_offset_ms', 'align_win_ms', 'align_noise_db', 'align_min_gap_ms', 'align_idx1', 'align_idx2']:
                params[key] = getattr(self, key).value()

        params['file1_path'] = file1
        params['file2_path'] = file2
        return params

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

    def _load_settings(self):
        settings = self.app_manager.load_config(self.tool_name, config.DEFAULTS)
        self.align_idx1.setValue(settings.get("align_idx1", config.DEFAULTS["align_idx1"]))
        self.align_idx2.setValue(settings.get("align_idx2", config.DEFAULTS["align_idx2"]))
        self.align_offset_ms.setValue(settings.get("align_offset_ms", config.DEFAULTS["align_offset_ms"]))
        self.align_norm_sr.setText(str(settings.get("align_norm_sr", config.DEFAULTS["align_norm_sr"])))
        self.align_norm_ch.setText(str(settings.get("align_norm_ch", config.DEFAULTS["align_norm_ch"])))
        self.align_win_ms.setValue(settings.get("align_win_ms", config.DEFAULTS["align_win_ms"]))
        self.align_noise_db.setValue(settings.get("align_noise_db", config.DEFAULTS["align_noise_db"]))
        self.align_min_gap.setValue(settings.get("align_min_gap", config.DEFAULTS["align_min_gap"]))

    def save_settings(self):
        current_settings = {
            "align_idx1": self.align_idx1.value(), "align_idx2": self.align_idx2.value(),
            "align_offset_ms": self.align_offset_ms.value(), "align_norm_sr": self.align_norm_sr.text(),
            "align_norm_ch": self.align_norm_ch.text(), "align_win_ms": self.align_win_ms.value(),
            "align_noise_db": self.align_noise_db.value(), "align_min_gap": self.align_min_gap.value(),
        }
        self.app_manager.save_config(self.tool_name, current_settings)
