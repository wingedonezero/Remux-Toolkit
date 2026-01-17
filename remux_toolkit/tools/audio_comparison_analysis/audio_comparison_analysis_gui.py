# remux_toolkit/tools/audio_comparison_analysis/audio_comparison_analysis_gui.py

from __future__ import annotations

import os
from typing import Iterable

from PyQt6 import QtCore, QtGui, QtWidgets

from . import audio_comparison_analysis_core as core
from .audio_comparison_analysis_config import DEFAULTS


class Worker(QtCore.QObject):
    analysis_complete = QtCore.pyqtSignal(list)
    error = QtCore.pyqtSignal(str)

    @QtCore.pyqtSlot(list, dict, str, str)
    def run(self, file_paths: list[str], settings: dict, output_dir: str, reference_path: str):
        try:
            settings_obj = core.AnalysisSettings(**settings)
            reference = reference_path or None
            results = core.analyze_files(file_paths, settings_obj, output_dir, reference)
            self.analysis_complete.emit([r.to_dict() for r in results])
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))


class AudioComparisonAnalysisWidget(QtWidgets.QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = "audio_comparison_analysis"
        self.thread = None
        self.worker = None
        self.file_inputs: list[QtWidgets.QLineEdit] = []
        self.spectrogram_labels: list[QtWidgets.QLabel] = []
        self.diff_spectrum_labels: list[QtWidgets.QLabel] = []
        self.clipping_heatmap_labels: list[QtWidgets.QLabel] = []
        self.file_cards: list[dict[str, QtWidgets.QLabel]] = []
        self.reference_index: int | None = None
        self.setAcceptDrops(True)
        self._init_ui()
        self._load_settings()
        self._setup_worker()
        self._check_dependencies()

    def _init_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._create_analysis_tab(), "Analysis")
        self.tabs.addTab(self._create_settings_tab(), "Settings")
        layout.addWidget(self.tabs)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self.setMinimumSize(0, 0)

    def _create_analysis_tab(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel_layout = QtWidgets.QVBoxLayout(panel)
        panel.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )

        file_group = QtWidgets.QGroupBox("Input Audio Files (up to 4)")
        file_layout = QtWidgets.QGridLayout(file_group)
        for idx in range(4):
            label = QtWidgets.QLabel(f"File {idx + 1}:")
            line_edit = QtWidgets.QLineEdit()
            line_edit.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            line_edit.customContextMenuRequested.connect(
                lambda pos, i=idx: self._show_reference_menu(i, pos)
            )
            browse_btn = QtWidgets.QPushButton("Browse")
            browse_btn.clicked.connect(lambda _, i=idx: self._browse_file(i))
            self.file_inputs.append(line_edit)
            file_layout.addWidget(label, idx, 0)
            file_layout.addWidget(line_edit, idx, 1)
            file_layout.addWidget(browse_btn, idx, 2)
        self.reference_label = QtWidgets.QLabel("Reference: None")
        file_layout.addWidget(self.reference_label, 4, 0, 1, 3)
        panel_layout.addWidget(file_group)

        action_layout = QtWidgets.QHBoxLayout()
        self.run_button = QtWidgets.QPushButton("Run Analysis")
        self.run_button.clicked.connect(self._run_analysis)
        self.clear_button = QtWidgets.QPushButton("Clear")
        self.clear_button.clicked.connect(self._clear_inputs)
        action_layout.addWidget(self.run_button)
        action_layout.addWidget(self.clear_button)
        action_layout.addStretch()
        panel_layout.addLayout(action_layout)

        analysis_tabs = QtWidgets.QTabWidget()
        analysis_tabs.addTab(self._create_results_panel(), "Results")
        analysis_tabs.addTab(self._create_spectrogram_panel(), "Spectrograms")
        analysis_tabs.addTab(self._create_forensic_panel(), "Forensic")
        analysis_tabs.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        panel_layout.addWidget(analysis_tabs, 1)

        return panel

    def _create_results_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel_layout = QtWidgets.QVBoxLayout(panel)
        panel.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )

        scroll_area = QtWidgets.QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setSizeAdjustPolicy(QtWidgets.QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
        scroll_area.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        panel_layout.addWidget(scroll_area, 1)

        content = QtWidgets.QWidget()
        content_layout = QtWidgets.QVBoxLayout(content)
        content.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        content.setMinimumHeight(0)

        cards_group = QtWidgets.QGroupBox("File Comparison")
        cards_layout = QtWidgets.QGridLayout(cards_group)
        cards_group.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        for idx in range(4):
            card = QtWidgets.QGroupBox(f"File {idx + 1}")
            card.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Expanding,
            )
            card_layout = QtWidgets.QFormLayout(card)
            card_layout.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
            card_layout.setFieldGrowthPolicy(
                QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
            )
            labels = {
                "name": QtWidgets.QLabel("-"),
                "info": QtWidgets.QLabel("-"),
                "dr": QtWidgets.QLabel("-"),
                "dr_score": QtWidgets.QLabel("-"),
                "lra": QtWidgets.QLabel("-"),
                "balance": QtWidgets.QLabel("-"),
                "dialogue": QtWidgets.QLabel("-"),
                "mastering": QtWidgets.QLabel("-"),
                "scores": QtWidgets.QLabel("-"),
                "glitches": QtWidgets.QLabel("-"),
                "cutoff": QtWidgets.QLabel("-"),
                "grade": QtWidgets.QLabel("-"),
                "score": QtWidgets.QLabel("-"),
                "flags": QtWidgets.QLabel("-"),
            }
            labels["flags"].setWordWrap(True)
            labels["flags"].setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Preferred,
            )
            labels["scores"].setWordWrap(True)
            labels["scores"].setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Preferred,
            )
            labels["glitches"].setWordWrap(True)
            labels["glitches"].setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Preferred,
            )
            card_layout.addRow("File Info", labels["name"])
            card_layout.addRow("Stream", labels["info"])
            card_layout.addRow("True DR14", labels["dr"])
            card_layout.addRow("Dynamics Score", labels["dr_score"])
            card_layout.addRow("Loudness Range", labels["lra"])
            card_layout.addRow("Dialog Balance", labels["balance"])
            card_layout.addRow("Dialogue Score", labels["dialogue"])
            card_layout.addRow("Mastering Score", labels["mastering"])
            card_layout.addRow("Score Breakdown", labels["scores"])
            card_layout.addRow("Pops/Crackles", labels["glitches"])
            card_layout.addRow("Cutoff (kHz)", labels["cutoff"])
            card_layout.addRow("Quality Grade", labels["grade"])
            card_layout.addRow("Score", labels["score"])
            card_layout.addRow("Flags", labels["flags"])
            self.file_cards.append(labels)
            cards_layout.addWidget(card, 0, idx)
            cards_layout.setColumnStretch(idx, 1)
        cards_layout.setRowStretch(0, 1)
        content_layout.addWidget(cards_group, 1)

        self.verdict_box = QtWidgets.QTextEdit()
        self.verdict_box.setReadOnly(True)
        self.verdict_box.setPlaceholderText("Verdict summary will appear here...")
        self.verdict_box.setFixedHeight(120)
        self.verdict_box.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        content_layout.addWidget(self.verdict_box)

        self.summary_box = QtWidgets.QTextEdit()
        self.summary_box.setReadOnly(True)
        self.summary_box.setPlaceholderText("Detailed analysis will appear here...")
        self.summary_box.setFixedHeight(240)
        self.summary_box.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        content_layout.addWidget(self.summary_box)

        content_layout.addStretch()

        scroll_area.setWidget(content)

        return panel

    def _create_spectrogram_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel_layout = QtWidgets.QVBoxLayout(panel)
        panel.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )

        scroll_area = QtWidgets.QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setSizeAdjustPolicy(QtWidgets.QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
        scroll_area.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        panel_layout.addWidget(scroll_area, 1)

        content = QtWidgets.QWidget()
        content_layout = QtWidgets.QVBoxLayout(content)
        content.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        content.setMinimumHeight(0)

        spectrogram_group = QtWidgets.QGroupBox("Spectrogram Comparison")
        spectrogram_layout = QtWidgets.QGridLayout(spectrogram_group)
        spectrogram_group.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        for idx in range(4):
            label = QtWidgets.QLabel("No spectrogram")
            label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            label.setMinimumHeight(220)
            label.setFrameStyle(QtWidgets.QFrame.Shape.StyledPanel | QtWidgets.QFrame.Shadow.Sunken)
            self.spectrogram_labels.append(label)
            spectrogram_layout.addWidget(label, idx // 2, idx % 2)
        content_layout.addWidget(spectrogram_group, 1)

        scroll_area.setWidget(content)

        return panel

    def _create_forensic_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel_layout = QtWidgets.QVBoxLayout(panel)
        panel.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )

        scroll_area = QtWidgets.QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setSizeAdjustPolicy(QtWidgets.QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
        scroll_area.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        panel_layout.addWidget(scroll_area, 1)

        content = QtWidgets.QWidget()
        content_layout = QtWidgets.QVBoxLayout(content)
        content.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        content.setMinimumHeight(0)

        forensic_group = QtWidgets.QGroupBox("Forensic Comparison")
        forensic_layout = QtWidgets.QGridLayout(forensic_group)
        forensic_group.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )

        for idx in range(4):
            card = QtWidgets.QGroupBox(f"File {idx + 1}")
            card_layout = QtWidgets.QVBoxLayout(card)

            diff_label = QtWidgets.QLabel("Difference spectrum unavailable")
            diff_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            diff_label.setMinimumHeight(180)
            diff_label.setFrameStyle(QtWidgets.QFrame.Shape.StyledPanel | QtWidgets.QFrame.Shadow.Sunken)
            heat_label = QtWidgets.QLabel("Clipping heatmap unavailable")
            heat_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            heat_label.setMinimumHeight(120)
            heat_label.setFrameStyle(QtWidgets.QFrame.Shape.StyledPanel | QtWidgets.QFrame.Shadow.Sunken)

            self.diff_spectrum_labels.append(diff_label)
            self.clipping_heatmap_labels.append(heat_label)

            card_layout.addWidget(diff_label)
            card_layout.addWidget(heat_label)
            forensic_layout.addWidget(card, idx // 2, idx % 2)

        content_layout.addWidget(forensic_group, 1)
        scroll_area.setWidget(content)

        return panel

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(1200, 720)

    def minimumSizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(900, 600)

    def _create_settings_tab(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QFormLayout(panel)

        self.target_sample_rate = QtWidgets.QSpinBox()
        self.target_sample_rate.setRange(8000, 192000)
        layout.addRow("Target Sample Rate (Hz)", self.target_sample_rate)

        self.fft_size = QtWidgets.QSpinBox()
        self.fft_size.setRange(512, 16384)
        self.fft_size.setSingleStep(512)
        layout.addRow("FFT Size", self.fft_size)

        self.hop_length = QtWidgets.QSpinBox()
        self.hop_length.setRange(128, 4096)
        layout.addRow("Hop Length", self.hop_length)

        self.cutoff_db_below_peak = QtWidgets.QDoubleSpinBox()
        self.cutoff_db_below_peak.setRange(10.0, 120.0)
        layout.addRow("Cutoff dB Below Peak", self.cutoff_db_below_peak)

        self.shelf_low_hz = QtWidgets.QSpinBox()
        self.shelf_low_hz.setRange(8000, 22050)
        layout.addRow("Shelf Low Hz", self.shelf_low_hz)

        self.shelf_high_hz = QtWidgets.QSpinBox()
        self.shelf_high_hz.setRange(10000, 24000)
        layout.addRow("Shelf High Hz", self.shelf_high_hz)

        self.shelf_drop_db = QtWidgets.QDoubleSpinBox()
        self.shelf_drop_db.setRange(10.0, 60.0)
        layout.addRow("Shelf Drop dB", self.shelf_drop_db)

        self.clip_threshold = QtWidgets.QDoubleSpinBox()
        self.clip_threshold.setRange(0.1, 1.0)
        self.clip_threshold.setSingleStep(0.001)
        layout.addRow("Clip Threshold", self.clip_threshold)

        self.clip_ratio_warn = QtWidgets.QDoubleSpinBox()
        self.clip_ratio_warn.setRange(0.0, 0.1)
        self.clip_ratio_warn.setSingleStep(0.0001)
        layout.addRow("Clip Ratio Warn", self.clip_ratio_warn)

        self.phase_inversion_threshold = QtWidgets.QDoubleSpinBox()
        self.phase_inversion_threshold.setRange(-1.0, 0.0)
        self.phase_inversion_threshold.setSingleStep(0.05)
        layout.addRow("Phase Inversion Threshold", self.phase_inversion_threshold)

        self.bitrate_reference_kbps = QtWidgets.QSpinBox()
        self.bitrate_reference_kbps.setRange(64, 2000)
        layout.addRow("Bitrate Reference (kbps)", self.bitrate_reference_kbps)

        self.dr_reference_db = QtWidgets.QDoubleSpinBox()
        self.dr_reference_db.setRange(4.0, 40.0)
        layout.addRow("DR Reference (dB)", self.dr_reference_db)

        self.dr_block_seconds = QtWidgets.QDoubleSpinBox()
        self.dr_block_seconds.setRange(1.0, 10.0)
        self.dr_block_seconds.setSingleStep(0.5)
        layout.addRow("DR Block Seconds", self.dr_block_seconds)

        self.dr_silence_db = QtWidgets.QDoubleSpinBox()
        self.dr_silence_db.setRange(-90.0, -10.0)
        layout.addRow("DR Silence Floor (dB)", self.dr_silence_db)

        self.dr_top_percent = QtWidgets.QDoubleSpinBox()
        self.dr_top_percent.setRange(0.05, 0.5)
        self.dr_top_percent.setSingleStep(0.05)
        layout.addRow("DR Top Percent", self.dr_top_percent)

        self.lra_target_min = QtWidgets.QDoubleSpinBox()
        self.lra_target_min.setRange(1.0, 20.0)
        self.lra_target_min.setSingleStep(0.5)
        layout.addRow("Loudness Range Min (dB)", self.lra_target_min)

        self.lra_target_max = QtWidgets.QDoubleSpinBox()
        self.lra_target_max.setRange(2.0, 30.0)
        self.lra_target_max.setSingleStep(0.5)
        layout.addRow("Loudness Range Max (dB)", self.lra_target_max)

        self.lra_high_penalty_db = QtWidgets.QDoubleSpinBox()
        self.lra_high_penalty_db.setRange(0.5, 5.0)
        self.lra_high_penalty_db.setSingleStep(0.5)
        layout.addRow("Loudness Range High Penalty", self.lra_high_penalty_db)

        self.nr_cutoff_hz = QtWidgets.QDoubleSpinBox()
        self.nr_cutoff_hz.setRange(1000.0, 8000.0)
        self.nr_cutoff_hz.setSingleStep(100.0)
        layout.addRow("NR Cutoff (Hz)", self.nr_cutoff_hz)

        self.nr_drop_db = QtWidgets.QDoubleSpinBox()
        self.nr_drop_db.setRange(1.0, 20.0)
        self.nr_drop_db.setSingleStep(0.5)
        layout.addRow("NR Drop vs Reference (dB)", self.nr_drop_db)

        self.nr_ratio_db = QtWidgets.QDoubleSpinBox()
        self.nr_ratio_db.setRange(3.0, 30.0)
        self.nr_ratio_db.setSingleStep(0.5)
        layout.addRow("NR High/Mid Ratio (dB)", self.nr_ratio_db)

        self.glitch_diff_threshold = QtWidgets.QDoubleSpinBox()
        self.glitch_diff_threshold.setRange(0.1, 1.0)
        self.glitch_diff_threshold.setSingleStep(0.05)
        layout.addRow("Glitch Diff Threshold", self.glitch_diff_threshold)

        self.glitch_max_count = QtWidgets.QSpinBox()
        self.glitch_max_count.setRange(1, 100)
        layout.addRow("Glitch Max Count", self.glitch_max_count)

        self.clip_heatmap_block_seconds = QtWidgets.QDoubleSpinBox()
        self.clip_heatmap_block_seconds.setRange(1.0, 30.0)
        self.clip_heatmap_block_seconds.setSingleStep(1.0)
        layout.addRow("Clipping Heatmap Block (s)", self.clip_heatmap_block_seconds)

        self.true_peak_dbfs = QtWidgets.QDoubleSpinBox()
        self.true_peak_dbfs.setRange(-3.0, 0.0)
        self.true_peak_dbfs.setSingleStep(0.1)
        layout.addRow("True Peak Threshold (dBFS)", self.true_peak_dbfs)

        self.brickwall_dr_db = QtWidgets.QDoubleSpinBox()
        self.brickwall_dr_db.setRange(2.0, 12.0)
        layout.addRow("Brickwall DR (dB)", self.brickwall_dr_db)

        self.target_dr_min = QtWidgets.QDoubleSpinBox()
        self.target_dr_min.setRange(6.0, 16.0)
        layout.addRow("Target DR Min", self.target_dr_min)

        self.target_dr_max = QtWidgets.QDoubleSpinBox()
        self.target_dr_max.setRange(8.0, 20.0)
        layout.addRow("Target DR Max", self.target_dr_max)

        self.dialog_band_low_hz = QtWidgets.QSpinBox()
        self.dialog_band_low_hz.setRange(100, 1000)
        layout.addRow("Dialog Band Low (Hz)", self.dialog_band_low_hz)

        self.dialog_band_high_hz = QtWidgets.QSpinBox()
        self.dialog_band_high_hz.setRange(1500, 6000)
        layout.addRow("Dialog Band High (Hz)", self.dialog_band_high_hz)

        self.presence_band_low_hz = QtWidgets.QSpinBox()
        self.presence_band_low_hz.setRange(1500, 8000)
        layout.addRow("Presence Band Low (Hz)", self.presence_band_low_hz)

        self.presence_band_high_hz = QtWidgets.QSpinBox()
        self.presence_band_high_hz.setRange(4000, 16000)
        layout.addRow("Presence Band High (Hz)", self.presence_band_high_hz)

        self.dialog_balance_warn_db = QtWidgets.QDoubleSpinBox()
        self.dialog_balance_warn_db.setRange(2.0, 15.0)
        self.dialog_balance_warn_db.setSingleStep(0.5)
        layout.addRow("Dialog Balance Warn (dB)", self.dialog_balance_warn_db)

        self.loudness_diff_warn_db = QtWidgets.QDoubleSpinBox()
        self.loudness_diff_warn_db.setRange(0.5, 10.0)
        self.loudness_diff_warn_db.setSingleStep(0.5)
        layout.addRow("Loudness Diff Warn (dB)", self.loudness_diff_warn_db)

        self.mastering_diff_penalty_db = QtWidgets.QDoubleSpinBox()
        self.mastering_diff_penalty_db.setRange(0.5, 10.0)
        self.mastering_diff_penalty_db.setSingleStep(0.5)
        layout.addRow("Mastering Diff Penalty", self.mastering_diff_penalty_db)

        self.dialogue_clarity_penalty = QtWidgets.QDoubleSpinBox()
        self.dialogue_clarity_penalty.setRange(5.0, 60.0)
        self.dialogue_clarity_penalty.setSingleStep(5.0)
        layout.addRow("Dialogue Clarity Penalty", self.dialogue_clarity_penalty)

        self.fake_multichannel_corr_threshold = QtWidgets.QDoubleSpinBox()
        self.fake_multichannel_corr_threshold.setRange(0.7, 0.999)
        self.fake_multichannel_corr_threshold.setSingleStep(0.01)
        layout.addRow("Fake Multi Corr Threshold", self.fake_multichannel_corr_threshold)

        self.fake_multichannel_energy_variance_db = QtWidgets.QDoubleSpinBox()
        self.fake_multichannel_energy_variance_db.setRange(1.0, 20.0)
        self.fake_multichannel_energy_variance_db.setSingleStep(0.5)
        layout.addRow("Fake Multi Energy Spread (dB)", self.fake_multichannel_energy_variance_db)

        self.mel_bins = QtWidgets.QSpinBox()
        self.mel_bins.setRange(32, 512)
        layout.addRow("Mel Bins", self.mel_bins)

        weights_group = QtWidgets.QGroupBox("Scoring Weights (%)")
        weights_layout = QtWidgets.QFormLayout(weights_group)
        self.weight_frequency = QtWidgets.QSpinBox()
        self.weight_frequency.setRange(0, 100)
        weights_layout.addRow("Frequency", self.weight_frequency)
        self.weight_dynamic_range = QtWidgets.QSpinBox()
        self.weight_dynamic_range.setRange(0, 100)
        weights_layout.addRow("Dynamic Range", self.weight_dynamic_range)
        self.weight_cleanliness = QtWidgets.QSpinBox()
        self.weight_cleanliness.setRange(0, 100)
        weights_layout.addRow("Cleanliness", self.weight_cleanliness)
        self.weight_efficiency = QtWidgets.QSpinBox()
        self.weight_efficiency.setRange(0, 100)
        weights_layout.addRow("Efficiency", self.weight_efficiency)
        self.weight_format = QtWidgets.QSpinBox()
        self.weight_format.setRange(0, 100)
        weights_layout.addRow("Format", self.weight_format)
        self.weight_dialogue = QtWidgets.QSpinBox()
        self.weight_dialogue.setRange(0, 100)
        weights_layout.addRow("Dialogue", self.weight_dialogue)
        self.weight_mastering = QtWidgets.QSpinBox()
        self.weight_mastering.setRange(0, 100)
        weights_layout.addRow("Mastering", self.weight_mastering)
        layout.addRow(weights_group)

        return panel

    def _setup_worker(self):
        self.thread = QtCore.QThread(self)
        self.worker = Worker()
        self.worker.moveToThread(self.thread)
        self.worker.analysis_complete.connect(self._on_analysis_complete)
        self.worker.error.connect(self._on_error)
        self.thread.start()

    def _check_dependencies(self):
        ok, msg = core.check_dependencies()
        if not ok:
            self.summary_box.setText(msg)
            self.run_button.setEnabled(False)

    def _load_settings(self):
        settings = self.app_manager.load_config(self.tool_name, DEFAULTS)
        self.target_sample_rate.setValue(settings.get("target_sample_rate", DEFAULTS["target_sample_rate"]))
        self.fft_size.setValue(settings.get("fft_size", DEFAULTS["fft_size"]))
        self.hop_length.setValue(settings.get("hop_length", DEFAULTS["hop_length"]))
        self.cutoff_db_below_peak.setValue(settings.get("cutoff_db_below_peak", DEFAULTS["cutoff_db_below_peak"]))
        self.shelf_low_hz.setValue(settings.get("shelf_low_hz", DEFAULTS["shelf_low_hz"]))
        self.shelf_high_hz.setValue(settings.get("shelf_high_hz", DEFAULTS["shelf_high_hz"]))
        self.shelf_drop_db.setValue(settings.get("shelf_drop_db", DEFAULTS["shelf_drop_db"]))
        self.clip_threshold.setValue(settings.get("clip_threshold", DEFAULTS["clip_threshold"]))
        self.clip_ratio_warn.setValue(settings.get("clip_ratio_warn", DEFAULTS["clip_ratio_warn"]))
        self.phase_inversion_threshold.setValue(
            settings.get("phase_inversion_threshold", DEFAULTS["phase_inversion_threshold"])
        )
        self.bitrate_reference_kbps.setValue(
            settings.get("bitrate_reference_kbps", DEFAULTS["bitrate_reference_kbps"])
        )
        self.dr_reference_db.setValue(settings.get("dr_reference_db", DEFAULTS["dr_reference_db"]))
        self.dr_block_seconds.setValue(settings.get("dr_block_seconds", DEFAULTS["dr_block_seconds"]))
        self.dr_silence_db.setValue(settings.get("dr_silence_db", DEFAULTS["dr_silence_db"]))
        self.dr_top_percent.setValue(settings.get("dr_top_percent", DEFAULTS["dr_top_percent"]))
        self.lra_target_min.setValue(settings.get("lra_target_min", DEFAULTS["lra_target_min"]))
        self.lra_target_max.setValue(settings.get("lra_target_max", DEFAULTS["lra_target_max"]))
        self.lra_high_penalty_db.setValue(
            settings.get("lra_high_penalty_db", DEFAULTS["lra_high_penalty_db"])
        )
        self.nr_cutoff_hz.setValue(settings.get("nr_cutoff_hz", DEFAULTS["nr_cutoff_hz"]))
        self.nr_drop_db.setValue(settings.get("nr_drop_db", DEFAULTS["nr_drop_db"]))
        self.nr_ratio_db.setValue(settings.get("nr_ratio_db", DEFAULTS["nr_ratio_db"]))
        self.glitch_diff_threshold.setValue(
            settings.get("glitch_diff_threshold", DEFAULTS["glitch_diff_threshold"])
        )
        self.glitch_max_count.setValue(settings.get("glitch_max_count", DEFAULTS["glitch_max_count"]))
        self.clip_heatmap_block_seconds.setValue(
            settings.get("clip_heatmap_block_seconds", DEFAULTS["clip_heatmap_block_seconds"])
        )
        self.true_peak_dbfs.setValue(settings.get("true_peak_dbfs", DEFAULTS["true_peak_dbfs"]))
        self.brickwall_dr_db.setValue(settings.get("brickwall_dr_db", DEFAULTS["brickwall_dr_db"]))
        self.target_dr_min.setValue(settings.get("target_dr_min", DEFAULTS["target_dr_min"]))
        self.target_dr_max.setValue(settings.get("target_dr_max", DEFAULTS["target_dr_max"]))
        self.dialog_band_low_hz.setValue(settings.get("dialog_band_low_hz", DEFAULTS["dialog_band_low_hz"]))
        self.dialog_band_high_hz.setValue(
            settings.get("dialog_band_high_hz", DEFAULTS["dialog_band_high_hz"])
        )
        self.presence_band_low_hz.setValue(
            settings.get("presence_band_low_hz", DEFAULTS["presence_band_low_hz"])
        )
        self.presence_band_high_hz.setValue(
            settings.get("presence_band_high_hz", DEFAULTS["presence_band_high_hz"])
        )
        self.dialog_balance_warn_db.setValue(
            settings.get("dialog_balance_warn_db", DEFAULTS["dialog_balance_warn_db"])
        )
        self.loudness_diff_warn_db.setValue(
            settings.get("loudness_diff_warn_db", DEFAULTS["loudness_diff_warn_db"])
        )
        self.mastering_diff_penalty_db.setValue(
            settings.get("mastering_diff_penalty_db", DEFAULTS["mastering_diff_penalty_db"])
        )
        self.dialogue_clarity_penalty.setValue(
            settings.get("dialogue_clarity_penalty", DEFAULTS["dialogue_clarity_penalty"])
        )
        self.fake_multichannel_corr_threshold.setValue(
            settings.get("fake_multichannel_corr_threshold", DEFAULTS["fake_multichannel_corr_threshold"])
        )
        self.fake_multichannel_energy_variance_db.setValue(
            settings.get(
                "fake_multichannel_energy_variance_db",
                DEFAULTS["fake_multichannel_energy_variance_db"],
            )
        )
        self.mel_bins.setValue(settings.get("mel_bins", DEFAULTS["mel_bins"]))
        self.weight_frequency.setValue(settings.get("weight_frequency", DEFAULTS["weight_frequency"]))
        self.weight_dynamic_range.setValue(
            settings.get("weight_dynamic_range", DEFAULTS["weight_dynamic_range"])
        )
        self.weight_cleanliness.setValue(settings.get("weight_cleanliness", DEFAULTS["weight_cleanliness"]))
        self.weight_efficiency.setValue(settings.get("weight_efficiency", DEFAULTS["weight_efficiency"]))
        self.weight_format.setValue(settings.get("weight_format", DEFAULTS["weight_format"]))
        self.weight_dialogue.setValue(settings.get("weight_dialogue", DEFAULTS["weight_dialogue"]))
        self.weight_mastering.setValue(settings.get("weight_mastering", DEFAULTS["weight_mastering"]))

    def save_settings(self):
        self.app_manager.save_config(self.tool_name, self._gather_settings())

    def _gather_settings(self) -> dict:
        return {
            "target_sample_rate": self.target_sample_rate.value(),
            "fft_size": self.fft_size.value(),
            "hop_length": self.hop_length.value(),
            "cutoff_db_below_peak": self.cutoff_db_below_peak.value(),
            "shelf_low_hz": self.shelf_low_hz.value(),
            "shelf_high_hz": self.shelf_high_hz.value(),
            "shelf_drop_db": self.shelf_drop_db.value(),
            "clip_threshold": self.clip_threshold.value(),
            "clip_ratio_warn": self.clip_ratio_warn.value(),
            "phase_inversion_threshold": self.phase_inversion_threshold.value(),
            "bitrate_reference_kbps": self.bitrate_reference_kbps.value(),
            "dr_reference_db": self.dr_reference_db.value(),
            "dr_block_seconds": self.dr_block_seconds.value(),
            "dr_silence_db": self.dr_silence_db.value(),
            "dr_top_percent": self.dr_top_percent.value(),
            "lra_target_min": self.lra_target_min.value(),
            "lra_target_max": self.lra_target_max.value(),
            "lra_high_penalty_db": self.lra_high_penalty_db.value(),
            "nr_cutoff_hz": self.nr_cutoff_hz.value(),
            "nr_drop_db": self.nr_drop_db.value(),
            "nr_ratio_db": self.nr_ratio_db.value(),
            "glitch_diff_threshold": self.glitch_diff_threshold.value(),
            "glitch_max_count": self.glitch_max_count.value(),
            "clip_heatmap_block_seconds": self.clip_heatmap_block_seconds.value(),
            "true_peak_dbfs": self.true_peak_dbfs.value(),
            "brickwall_dr_db": self.brickwall_dr_db.value(),
            "target_dr_min": self.target_dr_min.value(),
            "target_dr_max": self.target_dr_max.value(),
            "dialog_band_low_hz": self.dialog_band_low_hz.value(),
            "dialog_band_high_hz": self.dialog_band_high_hz.value(),
            "presence_band_low_hz": self.presence_band_low_hz.value(),
            "presence_band_high_hz": self.presence_band_high_hz.value(),
            "dialog_balance_warn_db": self.dialog_balance_warn_db.value(),
            "loudness_diff_warn_db": self.loudness_diff_warn_db.value(),
            "mastering_diff_penalty_db": self.mastering_diff_penalty_db.value(),
            "dialogue_clarity_penalty": self.dialogue_clarity_penalty.value(),
            "fake_multichannel_corr_threshold": self.fake_multichannel_corr_threshold.value(),
            "fake_multichannel_energy_variance_db": self.fake_multichannel_energy_variance_db.value(),
            "mel_bins": self.mel_bins.value(),
            "weight_frequency": self.weight_frequency.value(),
            "weight_dynamic_range": self.weight_dynamic_range.value(),
            "weight_cleanliness": self.weight_cleanliness.value(),
            "weight_efficiency": self.weight_efficiency.value(),
            "weight_format": self.weight_format.value(),
            "weight_dialogue": self.weight_dialogue.value(),
            "weight_mastering": self.weight_mastering.value(),
        }

    def _browse_file(self, index: int):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select Audio File",
            os.path.expanduser("~"),
            "Audio/Video Files (*.flac *.wav *.mka *.mkv *.mp4 *.m4a *.aac *.ac3 *.eac3 *.dts *.ogg);;All (*)",
        )
        if path:
            self.file_inputs[index].setText(path)

    def _show_reference_menu(self, index: int, pos: QtCore.QPoint) -> None:
        menu = QtWidgets.QMenu(self)
        set_action = menu.addAction("Set as Reference")
        clear_action = menu.addAction("Clear Reference")
        action = menu.exec(self.file_inputs[index].mapToGlobal(pos))
        if action == set_action:
            self._set_reference_index(index)
        elif action == clear_action:
            self._set_reference_index(None)

    def _set_reference_index(self, index: int | None) -> None:
        self.reference_index = index
        if index is None:
            self.reference_label.setText("Reference: None")
        else:
            name = os.path.basename(self.file_inputs[index].text().strip()) or f"File {index + 1}"
            self.reference_label.setText(f"Reference: {name}")

    def _clear_inputs(self):
        for line_edit in self.file_inputs:
            line_edit.clear()
        self.summary_box.clear()
        self.verdict_box.clear()
        self._set_reference_index(None)
        for labels in self.file_cards:
            for label in labels.values():
                label.setText("-")
        for label in self.spectrogram_labels:
            label.setText("No spectrogram")
            label.setPixmap(QtGui.QPixmap())
        for label in self.diff_spectrum_labels:
            label.setText("Difference spectrum unavailable")
            label.setPixmap(QtGui.QPixmap())
        for label in self.clipping_heatmap_labels:
            label.setText("Clipping heatmap unavailable")
            label.setPixmap(QtGui.QPixmap())

    def _collect_files(self) -> list[str]:
        paths = [edit.text().strip() for edit in self.file_inputs if edit.text().strip()]
        return paths[:4]

    def _run_analysis(self):
        file_paths = self._collect_files()
        if not file_paths:
            QtWidgets.QMessageBox.warning(self, "No files", "Please select up to 4 audio files.")
            return
        self.run_button.setEnabled(False)
        self.summary_box.setText("Analyzing audio files...")
        temp_dir = self.app_manager.get_temp_dir(self.tool_name)
        reference_path = None
        if self.reference_index is not None and self.reference_index < len(self.file_inputs):
            reference_path = self.file_inputs[self.reference_index].text().strip() or None
        QtCore.QMetaObject.invokeMethod(
            self.worker,
            "run",
            QtCore.Qt.ConnectionType.QueuedConnection,
            QtCore.Q_ARG(list, file_paths),
            QtCore.Q_ARG(dict, self._gather_settings()),
            QtCore.Q_ARG(str, temp_dir),
            QtCore.Q_ARG(str, reference_path or ""),
        )

    def _on_analysis_complete(self, results: list[dict]):
        self.run_button.setEnabled(True)
        self._populate_results(results)
        self._update_summary(results)
        self._load_spectrograms(results)
        self._load_forensics(results)

    def _on_error(self, message: str):
        self.run_button.setEnabled(True)
        self.summary_box.setText(f"Error: {message}")

    def _populate_results(self, results: list[dict]):
        for labels in self.file_cards:
            for label in labels.values():
                label.setText("-")

        for idx, result in enumerate(results[:4]):
            labels = self.file_cards[idx]
            flags = []
            if result.get("reference_path") and result.get("path") == result.get("reference_path"):
                flags.append("Reference")
            if result.get("reencode_detected"):
                flags.append("RE-ENCODE DETECTED")
            if result.get("shelf_detected"):
                flags.append("Spectral shelf")
            if result.get("nr_filtered") or result.get("center_nr_filtered"):
                flags.append("Excessive NR/Filtered")
            if abs(result.get("dialog_balance_db", 0.0)) >= self.dialog_balance_warn_db.value():
                if result.get("dialog_balance_db", 0.0) < 0:
                    flags.append("Presence boost")
                else:
                    flags.append("Dialog-heavy")
            if result.get("clipping_detected") or result.get("true_peak_db", -120.0) >= -0.1:
                flags.append("True-peak clipping risk")
            if result.get("phase_inversion"):
                flags.append("Phase inversion")
            if result.get("fake_multichannel"):
                flags.append("Fake multichannel")
            if result.get("bitrate_bloat"):
                flags.append("Bitrate bloat")
            if result.get("is_lossless") is True:
                flags.append("Lossless")
            elif result.get("is_lossless") is False:
                flags.append("Lossy")
            if abs(result.get("loudness_offset_db", 0.0)) >= self.loudness_diff_warn_db.value():
                flags.append("Loudness offset")
            if result.get("glitch_timestamps"):
                flags.append("Transient spikes")

            labels["name"].setText(os.path.basename(result["path"]))
            codec_name = result.get("codec_name")
            codec_label = f" | {codec_name}" if codec_name else ""
            labels["info"].setText(
                f"{result['channels']}ch @ {result['sample_rate']} Hz | "
                f"{result.get('bitrate_kbps', 0):.0f} kbps{codec_label} | {result['file_size_mb']:.1f} MB"
                if result.get("bitrate_kbps")
                else f"{result['channels']}ch @ {result['sample_rate']} Hz{codec_label} | {result['file_size_mb']:.1f} MB"
            )
            labels["dr"].setText(f"{result['dr_db']:.1f} dB (blocks {result['dr_blocks_used']})")
            labels["dr_score"].setText(f"{result['dr_score']:.1f}")
            labels["lra"].setText(f"{result.get('loudness_range_db', 0.0):.1f} dB")
            labels["balance"].setText(f"{result.get('dialog_balance_db', 0.0):.1f} dB")
            labels["dialogue"].setText(f"{result.get('dialogue_score', 0.0):.1f}")
            if result.get("reference_path"):
                labels["mastering"].setText(f"{result.get('mastering_score', 0.0):.1f}")
            else:
                labels["mastering"].setText("N/A")
            breakdown = (
                f"Freq {result.get('freq_score', 0.0):.1f} | "
                f"Clean {result.get('cleanliness_score', 0.0):.1f} | "
                f"Eff {result.get('efficiency_score', 0.0):.1f} | "
                f"Fmt {result.get('format_score', 0.0):.1f}"
            )
            labels["scores"].setText(breakdown)
            glitches = result.get("glitch_timestamps") or []
            if glitches:
                sample = ", ".join(f"{ts:.2f}s" for ts in glitches[:6])
                more = "…" if len(glitches) > 6 else ""
                labels["glitches"].setText(f"{sample}{more}")
            else:
                labels["glitches"].setText("None")
            labels["cutoff"].setText(f"{result['freq_cutoff_hz'] / 1000:.1f}")
            labels["grade"].setText(result.get("quality_grade", "-"))
            labels["score"].setText(f"{result['score']:.1f}")
            labels["flags"].setText(", ".join(flags) if flags else "None")

    def _update_summary(self, results: list[dict]):
        lines = []
        for rank, result in enumerate(results, start=1):
            lines.append(
                f"Ranked #{rank}: {os.path.basename(result['path'])} - {result['summary']} (Score {result['score']:.1f})"
            )
        self.summary_box.setText("\n".join(lines))
        if results:
            best = results[0]
            verdict = (
                f"Best Copy: {os.path.basename(best['path'])}\n"
                f"Reason: {best['summary']} (Grade {best.get('quality_grade', '-')}, "
                f"Score {best['score']:.1f})"
            )
            self.verdict_box.setText(verdict)
        else:
            self.verdict_box.clear()

    def _load_spectrograms(self, results: list[dict]):
        for label in self.spectrogram_labels:
            label.setText("No spectrogram")
            label.setPixmap(QtGui.QPixmap())
        for idx, result in enumerate(results[:4]):
            path = result.get("spectrogram_path")
            if not path or not os.path.exists(path):
                continue
            pixmap = QtGui.QPixmap(path)
            if pixmap.isNull():
                continue
            scaled = pixmap.scaled(400, 200, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
            self.spectrogram_labels[idx].setPixmap(scaled)
            self.spectrogram_labels[idx].setText("")

    def _load_forensics(self, results: list[dict]):
        for label in self.diff_spectrum_labels:
            label.setText("Difference spectrum unavailable")
            label.setPixmap(QtGui.QPixmap())
        for label in self.clipping_heatmap_labels:
            label.setText("Clipping heatmap unavailable")
            label.setPixmap(QtGui.QPixmap())

        for idx, result in enumerate(results[:4]):
            diff_path = result.get("diff_spectrum_path")
            if diff_path and os.path.exists(diff_path):
                pixmap = QtGui.QPixmap(diff_path)
                if not pixmap.isNull():
                    scaled = pixmap.scaled(400, 200, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
                    self.diff_spectrum_labels[idx].setPixmap(scaled)
                    self.diff_spectrum_labels[idx].setText("")
            elif result.get("reference_path") and result.get("path") == result.get("reference_path"):
                self.diff_spectrum_labels[idx].setText("Reference file")

            heat_path = result.get("clipping_heatmap_path")
            if heat_path and os.path.exists(heat_path):
                pixmap = QtGui.QPixmap(heat_path)
                if not pixmap.isNull():
                    scaled = pixmap.scaled(400, 140, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
                    self.clipping_heatmap_labels[idx].setPixmap(scaled)
                    self.clipping_heatmap_labels[idx].setText("")

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        urls = event.mimeData().urls()
        if not urls:
            return
        paths = [url.toLocalFile() for url in urls if url.toLocalFile()]
        self._fill_files(paths)
        event.acceptProposedAction()

    def _fill_files(self, paths: Iterable[str]):
        for path in paths:
            if not path:
                continue
            for edit in self.file_inputs:
                if not edit.text().strip():
                    edit.setText(path)
                    break

    def shutdown(self):
        if self.thread and self.thread.isRunning():
            self.thread.quit()
            self.thread.wait(2000)
        self.thread = None
        self.worker = None
