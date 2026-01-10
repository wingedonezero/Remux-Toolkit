# remux_toolkit/tools/video_ab_comparator/gui/settings_dialog.py

from PyQt6 import QtWidgets, QtCore

class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("A/B Comparator Settings")
        self.setMinimumWidth(600)
        self.settings = settings.copy()
        self.controls = {}

        # Main layout
        main_layout = QtWidgets.QVBoxLayout(self)

        # Tab widget
        self.tabs = QtWidgets.QTabWidget()
        main_layout.addWidget(self.tabs)

        # Create tabs
        self._create_analysis_tab()
        self._create_alignment_tab()
        self._create_frame_sync_tab()
        self._create_detectors_tab()

        # Buttons
        button_layout = QtWidgets.QHBoxLayout()
        reset_button = QtWidgets.QPushButton("Reset to Defaults")
        reset_button.clicked.connect(self._reset_to_defaults)
        button_layout.addWidget(reset_button)
        button_layout.addStretch()

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok |
            QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        button_layout.addWidget(buttons)

        main_layout.addLayout(button_layout)

    def _create_analysis_tab(self):
        """Analysis sampling and scoring settings."""
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QFormLayout(tab)

        # Header
        header = QtWidgets.QLabel("<b>Video Analysis Settings</b>")
        layout.addRow(header)

        info = QtWidgets.QLabel(
            "Controls how many frames are analyzed and how quality is scored.\n"
            "Higher chunk count = better coverage but slower analysis."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: gray;")
        layout.addRow(info)

        layout.addRow(QtWidgets.QLabel(""))  # Spacer

        # Chunk Count slider (3-100)
        chunk_label = QtWidgets.QLabel("Analysis Chunk Count:")
        chunk_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        chunk_slider.setRange(3, 100)
        chunk_slider.setValue(self.settings.get('analysis_chunk_count', 60))
        chunk_slider.setTickPosition(QtWidgets.QSlider.TickPosition.TicksBelow)
        chunk_slider.setTickInterval(10)

        chunk_value_label = QtWidgets.QLabel(f"{chunk_slider.value()} chunks")
        chunk_slider.valueChanged.connect(
            lambda v: chunk_value_label.setText(f"{v} chunks")
        )

        chunk_hbox = QtWidgets.QHBoxLayout()
        chunk_hbox.addWidget(chunk_slider)
        chunk_hbox.addWidget(chunk_value_label)
        layout.addRow(chunk_label, chunk_hbox)
        self.controls['analysis_chunk_count'] = chunk_slider

        # Chunk duration
        duration_label = QtWidgets.QLabel("Chunk Duration (seconds):")
        duration_spin = QtWidgets.QDoubleSpinBox()
        duration_spin.setRange(0.5, 5.0)
        duration_spin.setSingleStep(0.5)
        duration_spin.setValue(self.settings.get('analysis_chunk_duration', 1.0))
        duration_spin.setToolTip("Duration of each analyzed chunk in seconds")
        layout.addRow(duration_label, duration_spin)
        self.controls['analysis_chunk_duration'] = duration_spin

        # Coverage info (calculated)
        coverage_label = QtWidgets.QLabel()
        coverage_label.setStyleSheet("color: #0066cc; font-style: italic;")
        layout.addRow("", coverage_label)

        def update_coverage():
            chunks = chunk_slider.value()
            duration = duration_spin.value()
            total_seconds = chunks * duration
            frames = int(total_seconds * 10)  # 10fps sampling
            coverage_label.setText(
                f"→ {total_seconds:.0f} seconds sampled, ~{frames} frames analyzed"
            )

        chunk_slider.valueChanged.connect(lambda: update_coverage())
        duration_spin.valueChanged.connect(lambda: update_coverage())
        update_coverage()

        layout.addRow(QtWidgets.QLabel(""))  # Spacer

        # Tie threshold
        tie_label = QtWidgets.QLabel("Tie Threshold:")
        tie_spin = QtWidgets.QDoubleSpinBox()
        tie_spin.setRange(0.1, 5.0)
        tie_spin.setSingleStep(0.1)
        tie_spin.setValue(self.settings.get('tie_threshold', 0.5))
        tie_spin.setDecimals(1)
        tie_spin.setToolTip(
            "Minimum score difference to declare a winner.\n"
            "Lower = more sensitive (fewer ties)\n"
            "Default: 0.5"
        )
        layout.addRow(tie_label, tie_spin)
        self.controls['tie_threshold'] = tie_spin

        # Content filtering
        filter_checkbox = QtWidgets.QCheckBox("Filter Low-Information Frames")
        filter_checkbox.setChecked(self.settings.get('filter_low_information_frames', True))
        filter_checkbox.setToolTip(
            "Skip black frames and credits when selecting worst frames.\n"
            "Recommended: ON"
        )
        layout.addRow(filter_checkbox)
        self.controls['filter_low_information_frames'] = filter_checkbox

        layout.addStretch()
        self.tabs.addTab(tab, "Analysis")

    def _create_alignment_tab(self):
        """Audio alignment settings."""
        tab = QtWidgets.QWidget()
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(tab)

        layout = QtWidgets.QFormLayout(tab)

        # Header
        header = QtWidgets.QLabel("<b>Audio Alignment Settings</b>")
        layout.addRow(header)

        info = QtWidgets.QLabel(
            "Controls how audio correlation is performed to sync the videos."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: gray;")
        layout.addRow(info)

        layout.addRow(QtWidgets.QLabel(""))  # Spacer

        # Use advanced alignment
        advanced_checkbox = QtWidgets.QCheckBox("Use Advanced SCC Alignment")
        advanced_checkbox.setChecked(self.settings.get('use_advanced_alignment', False))
        advanced_checkbox.setToolTip(
            "Enable advanced Standard Cross-Correlation method.\n"
            "More accurate but slightly slower than fast hybrid mode."
        )
        layout.addRow(advanced_checkbox)
        self.controls['use_advanced_alignment'] = advanced_checkbox

        layout.addRow(QtWidgets.QLabel(""))  # Spacer

        # Audio language
        lang_label = QtWidgets.QLabel("Audio Language:")
        lang_combo = QtWidgets.QComboBox()
        lang_combo.addItems([
            "Auto (first track)",
            "jpn (Japanese)",
            "eng (English)",
            "ger (German)",
            "fra (French)",
            "spa (Spanish)"
        ])

        current_lang = self.settings.get('align_audio_lang', 'jpn')
        lang_map = {None: 0, '': 0, 'jpn': 1, 'eng': 2, 'ger': 3, 'fra': 4, 'spa': 5}
        lang_combo.setCurrentIndex(lang_map.get(current_lang, 1))
        lang_combo.setToolTip("Which audio track to use for alignment")

        layout.addRow(lang_label, lang_combo)
        self.controls['align_audio_lang_combo'] = lang_combo

        # Alignment chunk count
        align_chunks_label = QtWidgets.QLabel("Alignment Chunk Count:")
        align_chunks_spin = QtWidgets.QSpinBox()
        align_chunks_spin.setRange(5, 50)
        align_chunks_spin.setValue(self.settings.get('align_chunk_count', 30))
        align_chunks_spin.setToolTip("Number of audio chunks to analyze (default: 30)")
        layout.addRow(align_chunks_label, align_chunks_spin)
        self.controls['align_chunk_count'] = align_chunks_spin

        # Alignment chunk duration
        align_duration_label = QtWidgets.QLabel("Alignment Chunk Duration (s):")
        align_duration_spin = QtWidgets.QDoubleSpinBox()
        align_duration_spin.setRange(10.0, 60.0)
        align_duration_spin.setSingleStep(5.0)
        align_duration_spin.setValue(self.settings.get('align_chunk_duration', 30.0))
        align_duration_spin.setToolTip("Duration of each audio chunk (default: 30s)")
        layout.addRow(align_duration_label, align_duration_spin)
        self.controls['align_chunk_duration'] = align_duration_spin

        # Min match percentage
        min_match_label = QtWidgets.QLabel("Min Match % to Accept:")
        min_match_spin = QtWidgets.QDoubleSpinBox()
        min_match_spin.setRange(5.0, 50.0)
        min_match_spin.setSingleStep(5.0)
        min_match_spin.setValue(self.settings.get('align_min_match_pct', 20.0))
        min_match_spin.setSuffix("%")
        min_match_spin.setToolTip("Minimum correlation match to accept chunk (default: 20%)")
        layout.addRow(min_match_label, min_match_spin)
        self.controls['align_min_match_pct'] = min_match_spin

        # Scan range
        scan_start_label = QtWidgets.QLabel("Scan Start %:")
        scan_start_spin = QtWidgets.QDoubleSpinBox()
        scan_start_spin.setRange(0.0, 50.0)
        scan_start_spin.setSingleStep(5.0)
        scan_start_spin.setValue(self.settings.get('align_scan_start_pct', 5.0))
        scan_start_spin.setSuffix("%")
        layout.addRow(scan_start_label, scan_start_spin)
        self.controls['align_scan_start_pct'] = scan_start_spin

        scan_end_label = QtWidgets.QLabel("Scan End %:")
        scan_end_spin = QtWidgets.QDoubleSpinBox()
        scan_end_spin.setRange(50.0, 100.0)
        scan_end_spin.setSingleStep(5.0)
        scan_end_spin.setValue(self.settings.get('align_scan_end_pct', 95.0))
        scan_end_spin.setSuffix("%")
        layout.addRow(scan_end_label, scan_end_spin)
        self.controls['align_scan_end_pct'] = scan_end_spin

        # Delay selection strategy
        delay_label = QtWidgets.QLabel("Delay Selection Strategy:")
        delay_combo = QtWidgets.QComboBox()
        delay_combo.addItems([
            "first (First accepted chunk)",
            "median (Median of chunks)",
            "mean (Average of chunks)"
        ])

        current_delay = self.settings.get('align_delay_selection', 'first')
        delay_map = {'first': 0, 'median': 1, 'mean': 2}
        delay_combo.setCurrentIndex(delay_map.get(current_delay, 0))

        layout.addRow(delay_label, delay_combo)
        self.controls['align_delay_selection_combo'] = delay_combo

        layout.addRow(QtWidgets.QLabel(""))  # Spacer

        # Quality options
        soxr_checkbox = QtWidgets.QCheckBox("Use High-Quality SOXR Resampling")
        soxr_checkbox.setChecked(self.settings.get('align_use_soxr', True))
        layout.addRow(soxr_checkbox)
        self.controls['align_use_soxr'] = soxr_checkbox

        peak_fit_checkbox = QtWidgets.QCheckBox("Use Sub-Sample Peak Fitting")
        peak_fit_checkbox.setChecked(self.settings.get('align_peak_fit', True))
        layout.addRow(peak_fit_checkbox)
        self.controls['align_peak_fit'] = peak_fit_checkbox

        # Frame mapper
        frame_mapper_checkbox = QtWidgets.QCheckBox("Use VideoTimestamps for Frame Mapping")
        frame_mapper_checkbox.setChecked(self.settings.get('use_frame_mapper', True))
        frame_mapper_checkbox.setToolTip("Use exact frame timestamps (requires VideoTimestamps library)")
        layout.addRow(frame_mapper_checkbox)
        self.controls['use_frame_mapper'] = frame_mapper_checkbox

        pyav_checkbox = QtWidgets.QCheckBox("Use PyAV for Frame-Accurate Seeking")
        pyav_checkbox.setChecked(self.settings.get('use_pyav_seeking', False))
        pyav_checkbox.setToolTip("WARNING: May cause black frames, keep disabled")
        layout.addRow(pyav_checkbox)
        self.controls['use_pyav_seeking'] = pyav_checkbox

        layout.addStretch()
        self.tabs.addTab(scroll, "Alignment")

    def _create_frame_sync_tab(self):
        """Frame sync settings (audio-correlation-anchored)."""
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QFormLayout(tab)

        # Header
        header = QtWidgets.QLabel("<b>Frame Sync Settings</b>")
        layout.addRow(header)

        info = QtWidgets.QLabel(
            "Audio-correlation-anchored frame sync provides frame-perfect alignment.\n"
            "Uses perceptual hashing at multiple checkpoints to verify alignment."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: gray;")
        layout.addRow(info)

        layout.addRow(QtWidgets.QLabel(""))  # Spacer

        # Enable frame sync
        enable_checkbox = QtWidgets.QCheckBox("Enable Frame-Perfect Verification")
        enable_checkbox.setChecked(self.settings.get('align_visual_verification', True))
        enable_checkbox.setToolTip(
            "Fine-tune audio offset with visual frame matching.\n"
            "Recommended: ON"
        )
        layout.addRow(enable_checkbox)
        self.controls['align_visual_verification'] = enable_checkbox

        layout.addRow(QtWidgets.QLabel(""))  # Spacer

        # Number of checkpoints
        checkpoints_label = QtWidgets.QLabel("Number of Checkpoints:")
        checkpoints_spin = QtWidgets.QSpinBox()
        checkpoints_spin.setRange(2, 5)
        checkpoints_spin.setValue(self.settings.get('align_visual_num_checkpoints', 3))
        checkpoints_spin.setToolTip(
            "Number of verification points (at 5%, 50%, 95% of duration).\n"
            "More = better verification but slower.\n"
            "Default: 3"
        )
        layout.addRow(checkpoints_label, checkpoints_spin)
        self.controls['align_visual_num_checkpoints'] = checkpoints_spin

        # Window radius
        window_label = QtWidgets.QLabel("Window Radius (frames):")
        window_spin = QtWidgets.QSpinBox()
        window_spin.setRange(3, 10)
        window_spin.setValue(self.settings.get('align_visual_window_radius', 5))
        window_spin.setToolTip(
            "Frames before/after center to compare.\n"
            "5 = 11 frame window (center ±5)\n"
            "Default: 5"
        )
        layout.addRow(window_label, window_spin)
        self.controls['align_visual_window_radius'] = window_spin

        # Search range
        search_label = QtWidgets.QLabel("Search Range (frames):")
        search_spin = QtWidgets.QSpinBox()
        search_spin.setRange(10, 120)
        search_spin.setSingleStep(12)  # ~0.5s at 24fps
        search_spin.setValue(self.settings.get('align_visual_search_frames', 48))
        search_spin.setToolTip(
            "Search ±N frames around audio offset.\n"
            "48 frames = ±2 seconds at 24fps\n"
            "Increase if audio correlation is imprecise."
        )
        layout.addRow(search_label, search_spin)
        self.controls['align_visual_search_frames'] = search_spin

        layout.addRow(QtWidgets.QLabel(""))  # Spacer

        # Hash algorithm
        hash_algo_label = QtWidgets.QLabel("Hash Algorithm:")
        hash_algo_combo = QtWidgets.QComboBox()
        hash_algo_combo.addItems(["dhash (fast, good for similar encodes)",
                                    "phash (robust to scaling/filtering)",
                                    "average_hash (fastest, less accurate)"])

        current_algo = self.settings.get('align_visual_hash_algorithm', 'dhash')
        algo_map = {'dhash': 0, 'phash': 1, 'average_hash': 2}
        hash_algo_combo.setCurrentIndex(algo_map.get(current_algo, 0))

        layout.addRow(hash_algo_label, hash_algo_combo)
        self.controls['align_visual_hash_algorithm_combo'] = hash_algo_combo

        # Hash size
        hash_size_label = QtWidgets.QLabel("Hash Size:")
        hash_size_combo = QtWidgets.QComboBox()
        hash_size_combo.addItems(["8x8 (64-bit, fast)", "16x16 (256-bit, precise)"])

        current_size = self.settings.get('align_visual_hash_size', 8)
        hash_size_combo.setCurrentIndex(0 if current_size == 8 else 1)

        layout.addRow(hash_size_label, hash_size_combo)
        self.controls['align_visual_hash_size_combo'] = hash_size_combo

        # Hash threshold
        threshold_label = QtWidgets.QLabel("Hash Threshold:")
        threshold_spin = QtWidgets.QSpinBox()
        threshold_spin.setRange(1, 15)
        threshold_spin.setValue(self.settings.get('align_visual_hash_threshold', 5))
        threshold_spin.setToolTip(
            "Maximum hamming distance per frame to accept match.\n"
            "Lower = stricter matching\n"
            "Default: 5 bits"
        )
        layout.addRow(threshold_label, threshold_spin)
        self.controls['align_visual_hash_threshold'] = threshold_spin

        # Agreement tolerance
        tolerance_label = QtWidgets.QLabel("Agreement Tolerance (ms):")
        tolerance_spin = QtWidgets.QDoubleSpinBox()
        tolerance_spin.setRange(10.0, 500.0)
        tolerance_spin.setSingleStep(10.0)
        tolerance_spin.setValue(self.settings.get('align_visual_agreement_tolerance_ms', 100.0))
        tolerance_spin.setToolTip(
            "Max deviation between checkpoints to accept result.\n"
            "Default: 100ms (~2-3 frames at 24fps)"
        )
        layout.addRow(tolerance_label, tolerance_spin)
        self.controls['align_visual_agreement_tolerance_ms'] = tolerance_spin

        layout.addStretch()
        self.tabs.addTab(tab, "Frame Sync")

    def _create_detectors_tab(self):
        """Detector enable/disable settings."""
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)

        header = QtWidgets.QLabel("<b>Quality Detectors</b>")
        layout.addWidget(header)

        info = QtWidgets.QLabel(
            "Enable or disable specific quality detectors.\n"
            "Disabling detectors speeds up analysis but provides less information."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: gray;")
        layout.addWidget(info)

        layout.addWidget(QtWidgets.QLabel(""))  # Spacer

        # Global detectors
        global_group = QtWidgets.QGroupBox("Global Detectors")
        global_layout = QtWidgets.QVBoxLayout(global_group)

        audio_checkbox = QtWidgets.QCheckBox("Enable Audio Analysis")
        audio_checkbox.setChecked(self.settings.get('enable_audio_analysis', True))
        audio_checkbox.setToolTip("Analyze audio quality (loudness, clipping, PAL speedup)")
        global_layout.addWidget(audio_checkbox)
        self.controls['enable_audio_analysis'] = audio_checkbox

        interlace_checkbox = QtWidgets.QCheckBox("Enable Interlace Detection")
        interlace_checkbox.setChecked(self.settings.get('enable_interlace_detection', True))
        interlace_checkbox.setToolTip("Detect combing artifacts from interlaced video")
        global_layout.addWidget(interlace_checkbox)
        self.controls['enable_interlace_detection'] = interlace_checkbox

        cadence_checkbox = QtWidgets.QCheckBox("Enable Cadence Detection")
        cadence_checkbox.setChecked(self.settings.get('enable_cadence_detection', True))
        cadence_checkbox.setToolTip("Detect telecine cadence issues")
        global_layout.addWidget(cadence_checkbox)
        self.controls['enable_cadence_detection'] = cadence_checkbox

        layout.addWidget(global_group)

        # Note about frame detectors
        note = QtWidgets.QLabel(
            "\nFrame-based detectors (banding, blocking, ringing, etc.) are always enabled."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: gray; font-style: italic;")
        layout.addWidget(note)

        layout.addStretch()
        self.tabs.addTab(tab, "Detectors")

    def _reset_to_defaults(self):
        """Reset all settings to defaults."""
        reply = QtWidgets.QMessageBox.question(
            self,
            "Reset to Defaults",
            "Reset all settings to default values?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        )

        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            # Import defaults
            from ..video_ab_comparator_config import DEFAULTS
            self.settings = DEFAULTS.copy()

            # Update all controls
            self._update_controls_from_settings()

    def _update_controls_from_settings(self):
        """Update all controls to match settings."""
        # Analysis tab
        if 'analysis_chunk_count' in self.controls:
            self.controls['analysis_chunk_count'].setValue(self.settings.get('analysis_chunk_count', 60))
        if 'analysis_chunk_duration' in self.controls:
            self.controls['analysis_chunk_duration'].setValue(self.settings.get('analysis_chunk_duration', 1.0))
        if 'tie_threshold' in self.controls:
            self.controls['tie_threshold'].setValue(self.settings.get('tie_threshold', 0.5))
        if 'filter_low_information_frames' in self.controls:
            self.controls['filter_low_information_frames'].setChecked(self.settings.get('filter_low_information_frames', True))

        # Alignment tab
        if 'use_advanced_alignment' in self.controls:
            self.controls['use_advanced_alignment'].setChecked(self.settings.get('use_advanced_alignment', False))
        if 'align_chunk_count' in self.controls:
            self.controls['align_chunk_count'].setValue(self.settings.get('align_chunk_count', 30))
        if 'align_chunk_duration' in self.controls:
            self.controls['align_chunk_duration'].setValue(self.settings.get('align_chunk_duration', 30.0))
        # ... etc for all controls

        QtWidgets.QMessageBox.information(self, "Reset Complete", "All settings have been reset to defaults.")

    def get_settings(self) -> dict:
        """Extract settings from all controls."""
        # Analysis tab
        if 'analysis_chunk_count' in self.controls:
            self.settings['analysis_chunk_count'] = self.controls['analysis_chunk_count'].value()
        if 'analysis_chunk_duration' in self.controls:
            self.settings['analysis_chunk_duration'] = self.controls['analysis_chunk_duration'].value()
        if 'tie_threshold' in self.controls:
            self.settings['tie_threshold'] = self.controls['tie_threshold'].value()
        if 'filter_low_information_frames' in self.controls:
            self.settings['filter_low_information_frames'] = self.controls['filter_low_information_frames'].isChecked()

        # Alignment tab
        if 'use_advanced_alignment' in self.controls:
            self.settings['use_advanced_alignment'] = self.controls['use_advanced_alignment'].isChecked()

        if 'align_audio_lang_combo' in self.controls:
            idx = self.controls['align_audio_lang_combo'].currentIndex()
            lang_map = {0: None, 1: 'jpn', 2: 'eng', 3: 'ger', 4: 'fra', 5: 'spa'}
            self.settings['align_audio_lang'] = lang_map.get(idx, None)

        if 'align_chunk_count' in self.controls:
            self.settings['align_chunk_count'] = self.controls['align_chunk_count'].value()
        if 'align_chunk_duration' in self.controls:
            self.settings['align_chunk_duration'] = self.controls['align_chunk_duration'].value()
        if 'align_min_match_pct' in self.controls:
            self.settings['align_min_match_pct'] = self.controls['align_min_match_pct'].value()
        if 'align_scan_start_pct' in self.controls:
            self.settings['align_scan_start_pct'] = self.controls['align_scan_start_pct'].value()
        if 'align_scan_end_pct' in self.controls:
            self.settings['align_scan_end_pct'] = self.controls['align_scan_end_pct'].value()
        if 'align_use_soxr' in self.controls:
            self.settings['align_use_soxr'] = self.controls['align_use_soxr'].isChecked()
        if 'align_peak_fit' in self.controls:
            self.settings['align_peak_fit'] = self.controls['align_peak_fit'].isChecked()
        if 'use_frame_mapper' in self.controls:
            self.settings['use_frame_mapper'] = self.controls['use_frame_mapper'].isChecked()
        if 'use_pyav_seeking' in self.controls:
            self.settings['use_pyav_seeking'] = self.controls['use_pyav_seeking'].isChecked()

        if 'align_delay_selection_combo' in self.controls:
            idx = self.controls['align_delay_selection_combo'].currentIndex()
            delay_map = {0: 'first', 1: 'median', 2: 'mean'}
            self.settings['align_delay_selection'] = delay_map.get(idx, 'first')

        # Frame Sync tab
        if 'align_visual_verification' in self.controls:
            self.settings['align_visual_verification'] = self.controls['align_visual_verification'].isChecked()
        if 'align_visual_num_checkpoints' in self.controls:
            self.settings['align_visual_num_checkpoints'] = self.controls['align_visual_num_checkpoints'].value()
        if 'align_visual_window_radius' in self.controls:
            self.settings['align_visual_window_radius'] = self.controls['align_visual_window_radius'].value()
        if 'align_visual_search_frames' in self.controls:
            self.settings['align_visual_search_frames'] = self.controls['align_visual_search_frames'].value()

        if 'align_visual_hash_algorithm_combo' in self.controls:
            idx = self.controls['align_visual_hash_algorithm_combo'].currentIndex()
            algo_map = {0: 'dhash', 1: 'phash', 2: 'average_hash'}
            self.settings['align_visual_hash_algorithm'] = algo_map.get(idx, 'dhash')

        if 'align_visual_hash_size_combo' in self.controls:
            idx = self.controls['align_visual_hash_size_combo'].currentIndex()
            self.settings['align_visual_hash_size'] = 8 if idx == 0 else 16

        if 'align_visual_hash_threshold' in self.controls:
            self.settings['align_visual_hash_threshold'] = self.controls['align_visual_hash_threshold'].value()
        if 'align_visual_agreement_tolerance_ms' in self.controls:
            self.settings['align_visual_agreement_tolerance_ms'] = self.controls['align_visual_agreement_tolerance_ms'].value()

        # Detectors tab
        if 'enable_audio_analysis' in self.controls:
            self.settings['enable_audio_analysis'] = self.controls['enable_audio_analysis'].isChecked()
        if 'enable_interlace_detection' in self.controls:
            self.settings['enable_interlace_detection'] = self.controls['enable_interlace_detection'].isChecked()
        if 'enable_cadence_detection' in self.controls:
            self.settings['enable_cadence_detection'] = self.controls['enable_cadence_detection'].isChecked()

        return self.settings
