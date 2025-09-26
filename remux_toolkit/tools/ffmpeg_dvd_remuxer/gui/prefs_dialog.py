# remux_toolkit/tools/ffmpeg_dvd_remuxer/gui/prefs_dialog.py
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSpinBox, QCheckBox, QDialogButtonBox, QFileDialog, QComboBox,
    QSlider, QGroupBox
)
from PyQt6.QtCore import Qt

class PrefsDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("FFmpeg DVD Remuxer Settings")
        self.setMinimumWidth(600)

        layout = QVBoxLayout(self)

        # Output directory
        self.out_dir_edit = QLineEdit(self.config.get("default_output_directory", ""))
        self.out_dir_btn = QPushButton("Browse...")
        self.out_dir_btn.clicked.connect(self.browse_output_dir)
        out_dir_layout = QHBoxLayout()
        out_dir_layout.addWidget(self.out_dir_edit)
        out_dir_layout.addWidget(self.out_dir_btn)

        # Minimum title length
        self.min_len_spin = QSpinBox()
        self.min_len_spin.setRange(0, 10000)
        self.min_len_spin.setSuffix(" seconds")
        self.min_len_spin.setValue(self.config.get("minimum_title_length", 120))
        min_len_layout = QHBoxLayout()
        min_len_layout.addWidget(QLabel("Minimum Title Length for 'Process All':"))
        min_len_layout.addWidget(self.min_len_spin)

        # Processing options group
        processing_group = QGroupBox("Processing Options")
        processing_layout = QVBoxLayout()

        self.remove_eia_check = QCheckBox("Remove EIA-608 caption data from video stream")
        self.remove_eia_check.setChecked(self.config.get("remove_eia_608", True))

        self.ccextractor_check = QCheckBox("Run CCExtractor to create SRT subtitle track")
        self.ccextractor_check.setChecked(self.config.get("run_ccextractor", True))

        self.trim_padding_check = QCheckBox("Trim initial padding cells from DVD source (recommended)")
        self.trim_padding_check.setToolTip("Uses ffmpeg's -trim option to skip short filler cells at the start of a title.")
        self.trim_padding_check.setChecked(self.config.get("ffmpeg_trim_padding", True))

        self.keep_metadata_check = QCheckBox("Keep metadata JSON files after processing")
        self.keep_metadata_check.setToolTip("Useful for debugging timing issues")
        self.keep_metadata_check.setChecked(self.config.get("keep_metadata_json", False))

        processing_layout.addWidget(self.remove_eia_check)
        processing_layout.addWidget(self.ccextractor_check)
        processing_layout.addWidget(self.trim_padding_check)
        processing_layout.addWidget(self.keep_metadata_check)
        processing_group.setLayout(processing_layout)

        # Timing Method Group
        timing_group = QGroupBox("Stream Timing & Synchronization")
        timing_layout = QVBoxLayout()

        timing_method_layout = QHBoxLayout()
        timing_method_layout.addWidget(QLabel("Timing Method:"))
        self.timing_method = QComboBox()
        self.timing_method.addItems([
            "Auto (Best Available)",
            "PGC (Program Chain)",
            "PTS (Presentation Timestamps)",
            "FFprobe (Start Times)"
        ])
        timing_map = {
            "auto": 0,
            "pgc": 1,
            "pts": 2,
            "ffprobe": 3
        }
        current_timing = self.config.get("timing_method", "auto")
        self.timing_method.setCurrentIndex(timing_map.get(current_timing, 0))
        timing_method_layout.addWidget(self.timing_method)
        timing_method_layout.addStretch()

        timing_help = QLabel(
            "Auto mode tries PGC first (most accurate for DVDs),\n"
            "then PTS, then FFprobe. Each method has automatic\n"
            "fallback if the selected method fails."
        )
        timing_help.setStyleSheet("QLabel { color: #888; font-size: 11px; }")

        timing_layout.addLayout(timing_method_layout)
        timing_layout.addWidget(timing_help)
        timing_group.setLayout(timing_layout)

        # Track Naming Group
        naming_group = QGroupBox("Track Naming in Output")
        naming_layout = QVBoxLayout()

        self.audio_names_check = QCheckBox("Include descriptive names for audio tracks")
        self.audio_names_check.setToolTip("e.g., 'English AC3 5.1 (Director's Commentary)'")
        self.audio_names_check.setChecked(self.config.get("audio_track_names", True))

        self.subtitle_names_check = QCheckBox("Include descriptive names for subtitle tracks")
        self.subtitle_names_check.setToolTip("e.g., 'English [Forced]' or 'Spanish [CC]'")
        self.subtitle_names_check.setChecked(self.config.get("subtitle_track_names", True))

        self.cc_names_check = QCheckBox("Include descriptive name for extracted CC track")
        self.cc_names_check.setToolTip("Names the CCExtractor output as 'Closed Captions (EIA-608)'")
        self.cc_names_check.setChecked(self.config.get("cc_track_names", True))

        naming_layout.addWidget(self.audio_names_check)
        naming_layout.addWidget(self.subtitle_names_check)
        naming_layout.addWidget(self.cc_names_check)
        naming_group.setLayout(naming_layout)

        # Timing Method Group
        timing_group = QGroupBox("Stream Timing & Synchronization")
        timing_layout = QVBoxLayout()

        timing_method_layout = QHBoxLayout()
        timing_method_layout.addWidget(QLabel("Timing Method:"))
        self.timing_method = QComboBox()
        self.timing_method.addItems([
            "Auto (Best Available)",
            "PGC (Program Chain)",
            "PTS (Presentation Timestamps)",
            "FFprobe (Start Times)"
        ])
        timing_map = {
            "auto": 0,
            "pgc": 1,
            "pts": 2,
            "ffprobe": 3
        }
        current_timing = self.config.get("timing_method", "auto")
        self.timing_method.setCurrentIndex(timing_map.get(current_timing, 0))
        timing_method_layout.addWidget(self.timing_method)
        timing_method_layout.addStretch()

        timing_help = QLabel(
            "Auto mode tries PGC first (most accurate for DVDs),\n"
            "then PTS, then FFprobe. Each method has automatic\n"
            "fallback if the selected method fails."
        )
        timing_help.setStyleSheet("QLabel { color: #888; font-size: 11px; }")

        timing_layout.addLayout(timing_method_layout)
        timing_layout.addWidget(timing_help)
        timing_group.setLayout(timing_layout)

        # Telecine Detection Group
        telecine_group = QGroupBox("Telecine Detection (Film on Video)")
        telecine_layout = QVBoxLayout()

        # Mode selection
        mode_layout = QHBoxLayout()
        mode_layout.addWidget(QLabel("Detection Mode:"))
        self.telecine_mode = QComboBox()
        self.telecine_mode.addItems([
            "Disabled",
            "Auto-detect",
            "Force Progressive",
            "Force Interlaced"
        ])
        # Map config value to combo index
        mode_map = {
            "disabled": 0,
            "auto": 1,
            "force_progressive": 2,
            "force_interlaced": 3
        }
        current_mode = self.config.get("telecine_detection_mode", "disabled")
        self.telecine_mode.setCurrentIndex(mode_map.get(current_mode, 0))
        self.telecine_mode.currentIndexChanged.connect(self.on_telecine_mode_changed)
        mode_layout.addWidget(self.telecine_mode)
        mode_layout.addStretch()

        # Threshold slider
        threshold_layout = QHBoxLayout()
        threshold_layout.addWidget(QLabel("Progressive Threshold:"))
        self.threshold_slider = QSlider(Qt.Orientation.Horizontal)
        self.threshold_slider.setRange(60, 99)
        self.threshold_slider.setValue(self.config.get("telecine_threshold", 85))
        self.threshold_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.threshold_slider.setTickInterval(5)
        self.threshold_label = QLabel(f"{self.threshold_slider.value()}%")
        self.threshold_slider.valueChanged.connect(
            lambda v: self.threshold_label.setText(f"{v}%")
        )
        threshold_layout.addWidget(self.threshold_slider)
        threshold_layout.addWidget(self.threshold_label)

        # Sample duration
        sample_layout = QHBoxLayout()
        sample_layout.addWidget(QLabel("Sample Duration:"))
        self.sample_duration = QSpinBox()
        self.sample_duration.setRange(10, 300)
        self.sample_duration.setSuffix(" seconds")
        self.sample_duration.setValue(self.config.get("telecine_sample_duration", 60))
        self.sample_duration.setToolTip("How many seconds of video to analyze for telecine detection")
        sample_layout.addWidget(self.sample_duration)
        sample_layout.addStretch()

        # Help text
        help_label = QLabel(
            "Auto-detect will analyze video for 3:2 pulldown patterns.\n"
            "If progressive frames exceed the threshold, the video will be\n"
            "flagged as progressive for optimal playback on all devices."
        )
        help_label.setStyleSheet("QLabel { color: #888; font-size: 11px; }")

        telecine_layout.addLayout(mode_layout)
        telecine_layout.addLayout(threshold_layout)
        telecine_layout.addLayout(sample_layout)
        telecine_layout.addWidget(help_label)
        telecine_group.setLayout(telecine_layout)

        # Enable/disable threshold and duration based on mode
        self.on_telecine_mode_changed(self.telecine_mode.currentIndex())

        # Dialog buttons
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        # Add all to main layout
        layout.addWidget(QLabel("Default Output Directory:"))
        layout.addLayout(out_dir_layout)
        layout.addLayout(min_len_layout)
        layout.addWidget(processing_group)
        layout.addWidget(naming_group)
        layout.addWidget(timing_group)
        layout.addWidget(telecine_group)
        layout.addWidget(buttons)

    def on_telecine_mode_changed(self, index):
        """Enable/disable controls based on telecine mode."""
        # Only enable threshold and duration for auto-detect mode
        is_auto = (index == 1)
        self.threshold_slider.setEnabled(is_auto)
        self.threshold_label.setEnabled(is_auto)
        self.sample_duration.setEnabled(is_auto)

    def browse_output_dir(self):
        dir_name = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if dir_name: self.out_dir_edit.setText(dir_name)

    def get_values(self):
        # Map combo indices to config values
        mode_map = {
            0: "disabled",
            1: "auto",
            2: "force_progressive",
            3: "force_interlaced"
        }
        timing_map = {
            0: "auto",
            1: "pgc",
            2: "pts",
            3: "ffprobe"
        }
        return {
            "default_output_directory": self.out_dir_edit.text(),
            "minimum_title_length": self.min_len_spin.value(),
            "remove_eia_608": self.remove_eia_check.isChecked(),
            "run_ccextractor": self.ccextractor_check.isChecked(),
            "ffmpeg_trim_padding": self.trim_padding_check.isChecked(),
            "keep_metadata_json": self.keep_metadata_check.isChecked(),
            "audio_track_names": self.audio_names_check.isChecked(),
            "subtitle_track_names": self.subtitle_names_check.isChecked(),
            "cc_track_names": self.cc_names_check.isChecked(),
            "timing_method": timing_map[self.timing_method.currentIndex()],
            "telecine_detection_mode": mode_map[self.telecine_mode.currentIndex()],
            "telecine_threshold": self.threshold_slider.value(),
            "telecine_sample_duration": self.sample_duration.value(),
        }
