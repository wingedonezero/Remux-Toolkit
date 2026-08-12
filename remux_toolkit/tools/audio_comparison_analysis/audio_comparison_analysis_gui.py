# remux_toolkit/tools/audio_comparison_analysis/audio_comparison_analysis_gui.py

from __future__ import annotations

import json
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
        self.delta_eq_labels: list[QtWidgets.QLabel] = []
        self.limiting_heatmap_labels: list[QtWidgets.QLabel] = []
        self.limiting_waveform_labels: list[QtWidgets.QLabel] = []
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
        self.export_button = QtWidgets.QPushButton("Export Results")
        self.export_button.clicked.connect(self._export_results)
        action_layout.addWidget(self.run_button)
        action_layout.addWidget(self.clear_button)
        action_layout.addWidget(self.export_button)
        action_layout.addStretch()
        panel_layout.addLayout(action_layout)

        analysis_tabs = QtWidgets.QTabWidget()
        analysis_tabs.addTab(self._create_results_panel(), "Results")
        analysis_tabs.addTab(self._create_spectrogram_panel(), "Spectrograms")
        analysis_tabs.addTab(self._create_forensic_panel(), "Forensic")
        analysis_tabs.addTab(self._create_log_panel(), "Log")
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
                "integrity": QtWidgets.QLabel("-"),
                "dr": QtWidgets.QLabel("-"),
                "dr_score": QtWidgets.QLabel("-"),
                "lra": QtWidgets.QLabel("-"),
                "balance": QtWidgets.QLabel("-"),
                "dialogue": QtWidgets.QLabel("-"),
                "mastering": QtWidgets.QLabel("-"),
                "eq": QtWidgets.QLabel("-"),
                "pitch": QtWidgets.QLabel("-"),
                "channel": QtWidgets.QLabel("-"),
                "alignment": QtWidgets.QLabel("-"),
                "scores": QtWidgets.QLabel("-"),
                "glitches": QtWidgets.QLabel("-"),
                "limiting": QtWidgets.QLabel("-"),
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
            labels["limiting"].setWordWrap(True)
            labels["limiting"].setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Preferred,
            )
            labels["integrity"].setWordWrap(True)
            labels["integrity"].setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Preferred,
            )
            card_layout.addRow("File Info", labels["name"])
            card_layout.addRow("Stream", labels["info"])
            card_layout.addRow("Decode Integrity", labels["integrity"])
            card_layout.addRow("Crest Factor", labels["dr"])
            card_layout.addRow("Dynamics Score", labels["dr_score"])
            card_layout.addRow("Loudness Range", labels["lra"])
            card_layout.addRow("Dialog Balance", labels["balance"])
            card_layout.addRow("Dialogue Score", labels["dialogue"])
            card_layout.addRow("Mastering Score", labels["mastering"])
            card_layout.addRow("EQ Delta", labels["eq"])
            card_layout.addRow("Pitch/Speed", labels["pitch"])
            card_layout.addRow("Channel Integrity", labels["channel"])
            card_layout.addRow("Alignment", labels["alignment"])
            card_layout.addRow("Score Breakdown", labels["scores"])
            card_layout.addRow("Pops/Crackles", labels["glitches"])
            card_layout.addRow("Limiting Hot Spots", labels["limiting"])
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

    def _create_log_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)

        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.log_box = QtWidgets.QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setPlaceholderText("Analysis log will appear here...")
        layout.addWidget(self.log_box, 1)

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

            delta_label = QtWidgets.QLabel("Delta EQ map unavailable")
            delta_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            delta_label.setMinimumHeight(180)
            delta_label.setFrameStyle(QtWidgets.QFrame.Shape.StyledPanel | QtWidgets.QFrame.Shadow.Sunken)

            limiting_label = QtWidgets.QLabel("Limiting heatmap unavailable")
            limiting_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            limiting_label.setMinimumHeight(120)
            limiting_label.setFrameStyle(QtWidgets.QFrame.Shape.StyledPanel | QtWidgets.QFrame.Shadow.Sunken)

            zoom_label = QtWidgets.QLabel("Waveform zoom unavailable")
            zoom_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            zoom_label.setMinimumHeight(120)
            zoom_label.setFrameStyle(QtWidgets.QFrame.Shape.StyledPanel | QtWidgets.QFrame.Shadow.Sunken)

            self.diff_spectrum_labels.append(diff_label)
            self.clipping_heatmap_labels.append(heat_label)
            self.delta_eq_labels.append(delta_label)
            self.limiting_heatmap_labels.append(limiting_label)
            self.limiting_waveform_labels.append(zoom_label)

            card_layout.addWidget(diff_label)
            card_layout.addWidget(delta_label)
            card_layout.addWidget(heat_label)
            card_layout.addWidget(limiting_label)
            card_layout.addWidget(zoom_label)
            forensic_layout.addWidget(card, idx // 2, idx % 2)

        content_layout.addWidget(forensic_group, 1)
        scroll_area.setWidget(content)

        return panel

    def _create_log_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)

        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.log_box = QtWidgets.QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setPlaceholderText("Analysis log will appear here...")
        layout.addWidget(self.log_box, 1)

        return panel

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(1200, 720)

    def minimumSizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(900, 600)

    def _create_settings_tab(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel_layout = QtWidgets.QVBoxLayout(panel)

        scroll_area = QtWidgets.QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setSizeAdjustPolicy(QtWidgets.QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
        scroll_area.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        panel_layout.addWidget(scroll_area, 1)

        content = QtWidgets.QWidget()
        layout = QtWidgets.QFormLayout(content)

        def set_tip(widget: QtWidgets.QWidget, text: str) -> None:
            widget.setToolTip(text)

        self.target_sample_rate = QtWidgets.QSpinBox()
        self.target_sample_rate.setRange(8000, 192000)
        set_tip(self.target_sample_rate, "Resample all inputs to this rate for analysis.")
        layout.addRow("Target Sample Rate (Hz)", self.target_sample_rate)

        self.fft_size = QtWidgets.QSpinBox()
        self.fft_size.setRange(512, 16384)
        self.fft_size.setSingleStep(512)
        set_tip(self.fft_size, "FFT size for spectral analysis. Higher = more frequency detail.")
        layout.addRow("FFT Size", self.fft_size)

        self.hop_length = QtWidgets.QSpinBox()
        self.hop_length.setRange(128, 4096)
        set_tip(self.hop_length, "Hop size between FFT frames. Lower = more time detail.")
        layout.addRow("Hop Length", self.hop_length)

        self.cutoff_db_below_peak = QtWidgets.QDoubleSpinBox()
        self.cutoff_db_below_peak.setRange(10.0, 120.0)
        set_tip(self.cutoff_db_below_peak, "Threshold below peak to estimate bandwidth cutoff.")
        layout.addRow("Cutoff dB Below Peak", self.cutoff_db_below_peak)

        self.shelf_low_hz = QtWidgets.QSpinBox()
        self.shelf_low_hz.setRange(8000, 22050)
        set_tip(self.shelf_low_hz, "Lower bound for high-shelf detection.")
        layout.addRow("Shelf Low Hz", self.shelf_low_hz)

        self.shelf_high_hz = QtWidgets.QSpinBox()
        self.shelf_high_hz.setRange(10000, 24000)
        set_tip(self.shelf_high_hz, "Upper bound for high-shelf detection.")
        layout.addRow("Shelf High Hz", self.shelf_high_hz)

        self.shelf_drop_db = QtWidgets.QDoubleSpinBox()
        self.shelf_drop_db.setRange(10.0, 60.0)
        set_tip(self.shelf_drop_db, "Drop in dB to flag a spectral shelf.")
        layout.addRow("Shelf Drop dB", self.shelf_drop_db)

        self.clip_threshold = QtWidgets.QDoubleSpinBox()
        self.clip_threshold.setRange(0.1, 1.0)
        self.clip_threshold.setSingleStep(0.001)
        set_tip(self.clip_threshold, "Amplitude threshold considered as clipping.")
        layout.addRow("Clip Threshold", self.clip_threshold)

        self.clip_ratio_warn = QtWidgets.QDoubleSpinBox()
        self.clip_ratio_warn.setRange(0.0, 0.1)
        self.clip_ratio_warn.setSingleStep(0.0001)
        set_tip(self.clip_ratio_warn, "Ratio of clipped samples to flag clipping.")
        layout.addRow("Clip Ratio Warn", self.clip_ratio_warn)

        self.phase_inversion_threshold = QtWidgets.QDoubleSpinBox()
        self.phase_inversion_threshold.setRange(-1.0, 0.0)
        self.phase_inversion_threshold.setSingleStep(0.05)
        set_tip(self.phase_inversion_threshold, "Correlation threshold to flag phase inversion.")
        layout.addRow("Phase Inversion Threshold", self.phase_inversion_threshold)

        self.bitrate_reference_kbps = QtWidgets.QSpinBox()
        self.bitrate_reference_kbps.setRange(64, 2000)
        set_tip(self.bitrate_reference_kbps, "Expected bitrate used for bloat detection.")
        layout.addRow("Bitrate Reference (kbps)", self.bitrate_reference_kbps)

        self.dr_reference_db = QtWidgets.QDoubleSpinBox()
        self.dr_reference_db.setRange(4.0, 40.0)
        set_tip(self.dr_reference_db, "Reference DR (legacy parameter; kept for compatibility).")
        layout.addRow("Crest Reference (dB)", self.dr_reference_db)

        self.dr_block_seconds = QtWidgets.QDoubleSpinBox()
        self.dr_block_seconds.setRange(1.0, 10.0)
        self.dr_block_seconds.setSingleStep(0.5)
        set_tip(self.dr_block_seconds, "Block duration for crest factor and loudness stats.")
        layout.addRow("DR Block Seconds", self.dr_block_seconds)

        self.dr_silence_db = QtWidgets.QDoubleSpinBox()
        self.dr_silence_db.setRange(-90.0, -10.0)
        set_tip(self.dr_silence_db, "Silence floor for block loudness calculations.")
        layout.addRow("DR Silence Floor (dB)", self.dr_silence_db)

        self.dr_top_percent = QtWidgets.QDoubleSpinBox()
        self.dr_top_percent.setRange(0.05, 0.5)
        self.dr_top_percent.setSingleStep(0.05)
        set_tip(self.dr_top_percent, "Top percent of blocks used for crest factor.")
        layout.addRow("DR Top Percent", self.dr_top_percent)

        self.lra_target_min = QtWidgets.QDoubleSpinBox()
        self.lra_target_min.setRange(1.0, 20.0)
        self.lra_target_min.setSingleStep(0.5)
        set_tip(self.lra_target_min, "Minimum acceptable loudness range.")
        layout.addRow("Loudness Range Min (dB)", self.lra_target_min)

        self.lra_target_max = QtWidgets.QDoubleSpinBox()
        self.lra_target_max.setRange(2.0, 30.0)
        self.lra_target_max.setSingleStep(0.5)
        set_tip(self.lra_target_max, "Maximum acceptable loudness range.")
        layout.addRow("Loudness Range Max (dB)", self.lra_target_max)

        self.lra_high_penalty_db = QtWidgets.QDoubleSpinBox()
        self.lra_high_penalty_db.setRange(0.5, 5.0)
        self.lra_high_penalty_db.setSingleStep(0.5)
        set_tip(self.lra_high_penalty_db, "Penalty per dB above the max LRA.")
        layout.addRow("Loudness Range High Penalty", self.lra_high_penalty_db)

        self.eq_muffle_drop_db = QtWidgets.QDoubleSpinBox()
        self.eq_muffle_drop_db.setRange(1.0, 12.0)
        self.eq_muffle_drop_db.setSingleStep(0.5)
        set_tip(self.eq_muffle_drop_db, "Drop in 2–7 kHz band to flag muffleness.")
        layout.addRow("EQ Muffle Drop (dB)", self.eq_muffle_drop_db)

        self.eq_boom_boost_db = QtWidgets.QDoubleSpinBox()
        self.eq_boom_boost_db.setRange(1.0, 12.0)
        self.eq_boom_boost_db.setSingleStep(0.5)
        set_tip(self.eq_boom_boost_db, "Boost around 120 Hz to flag boominess.")
        layout.addRow("EQ Boom Boost (dB)", self.eq_boom_boost_db)

        self.eq_muffle_low_hz = QtWidgets.QSpinBox()
        self.eq_muffle_low_hz.setRange(500, 8000)
        set_tip(self.eq_muffle_low_hz, "Low edge of muffleness band.")
        layout.addRow("EQ Muffle Low (Hz)", self.eq_muffle_low_hz)

        self.eq_muffle_high_hz = QtWidgets.QSpinBox()
        self.eq_muffle_high_hz.setRange(1000, 12000)
        set_tip(self.eq_muffle_high_hz, "High edge of muffleness band.")
        layout.addRow("EQ Muffle High (Hz)", self.eq_muffle_high_hz)

        self.eq_boom_center_hz = QtWidgets.QSpinBox()
        self.eq_boom_center_hz.setRange(40, 300)
        set_tip(self.eq_boom_center_hz, "Center frequency for boominess check.")
        layout.addRow("EQ Boom Center (Hz)", self.eq_boom_center_hz)

        self.eq_boom_band_hz = QtWidgets.QSpinBox()
        self.eq_boom_band_hz.setRange(10, 120)
        set_tip(self.eq_boom_band_hz, "Bandwidth around boominess center frequency.")
        layout.addRow("EQ Boom Band (Hz)", self.eq_boom_band_hz)

        self.f0_segment_seconds = QtWidgets.QDoubleSpinBox()
        self.f0_segment_seconds.setRange(2.0, 30.0)
        self.f0_segment_seconds.setSingleStep(1.0)
        set_tip(self.f0_segment_seconds, "Length of segment used for f0 estimation.")
        layout.addRow("F0 Segment (s)", self.f0_segment_seconds)

        self.f0_segment_offset_ratio = QtWidgets.QDoubleSpinBox()
        self.f0_segment_offset_ratio.setRange(0.0, 0.9)
        self.f0_segment_offset_ratio.setSingleStep(0.05)
        set_tip(self.f0_segment_offset_ratio, "Fraction into the file to sample f0.")
        layout.addRow("F0 Segment Offset", self.f0_segment_offset_ratio)

        self.pal_speed_ratio = QtWidgets.QDoubleSpinBox()
        self.pal_speed_ratio.setRange(1.0, 1.1)
        self.pal_speed_ratio.setSingleStep(0.001)
        set_tip(self.pal_speed_ratio, "Expected PAL speed-up ratio (≈1.0417).")
        layout.addRow("PAL Speed Ratio", self.pal_speed_ratio)

        self.pitch_semitone_shift = QtWidgets.QDoubleSpinBox()
        self.pitch_semitone_shift.setRange(0.1, 2.0)
        self.pitch_semitone_shift.setSingleStep(0.1)
        set_tip(self.pitch_semitone_shift, "Expected pitch shift in semitones.")
        layout.addRow("Pitch Shift (semitones)", self.pitch_semitone_shift)

        self.pitch_tolerance_ratio = QtWidgets.QDoubleSpinBox()
        self.pitch_tolerance_ratio.setRange(0.001, 0.02)
        self.pitch_tolerance_ratio.setSingleStep(0.001)
        set_tip(self.pitch_tolerance_ratio, "Tolerance ratio for pitch/speed detection.")
        layout.addRow("Pitch/Speed Tolerance", self.pitch_tolerance_ratio)

        self.scc_min_match_confidence = QtWidgets.QDoubleSpinBox()
        self.scc_min_match_confidence.setRange(0.5, 0.99)
        self.scc_min_match_confidence.setSingleStep(0.01)
        set_tip(
            self.scc_min_match_confidence,
            "Minimum SCC alignment confidence; below this uses unaligned analysis.",
        )
        layout.addRow("SCC Min Match Confidence", self.scc_min_match_confidence)

        self.channel_swap_corr_threshold = QtWidgets.QDoubleSpinBox()
        self.channel_swap_corr_threshold.setRange(0.5, 0.99)
        self.channel_swap_corr_threshold.setSingleStep(0.05)
        set_tip(self.channel_swap_corr_threshold, "Correlation threshold to flag channel swaps.")
        layout.addRow("Channel Swap Corr Threshold", self.channel_swap_corr_threshold)

        self.lfe_rolloff_hz = QtWidgets.QSpinBox()
        self.lfe_rolloff_hz.setRange(80, 200)
        set_tip(self.lfe_rolloff_hz, "Expected LFE roll-off frequency.")
        layout.addRow("LFE Rolloff Hz", self.lfe_rolloff_hz)

        self.lfe_high_ratio_db = QtWidgets.QDoubleSpinBox()
        self.lfe_high_ratio_db.setRange(-30.0, 0.0)
        self.lfe_high_ratio_db.setSingleStep(1.0)
        set_tip(self.lfe_high_ratio_db, "High/low LFE ratio threshold to flag errors.")
        layout.addRow("LFE High Ratio (dB)", self.lfe_high_ratio_db)

        self.limiting_window_ms = QtWidgets.QSpinBox()
        self.limiting_window_ms.setRange(50, 500)
        set_tip(self.limiting_window_ms, "Window size for limiting detection.")
        layout.addRow("Limiting Window (ms)", self.limiting_window_ms)

        self.limiting_ratio = QtWidgets.QDoubleSpinBox()
        self.limiting_ratio.setRange(0.0001, 0.01)
        self.limiting_ratio.setSingleStep(0.0001)
        set_tip(self.limiting_ratio, "Fraction of near-clip samples to flag limiting.")
        layout.addRow("Limiting Ratio", self.limiting_ratio)

        self.limiting_heatmap_block_seconds = QtWidgets.QDoubleSpinBox()
        self.limiting_heatmap_block_seconds.setRange(0.5, 5.0)
        self.limiting_heatmap_block_seconds.setSingleStep(0.5)
        set_tip(self.limiting_heatmap_block_seconds, "Block size for limiting heatmap aggregation.")
        layout.addRow("Limiting Heatmap Block (s)", self.limiting_heatmap_block_seconds)

        self.limiting_waveform_segments = QtWidgets.QSpinBox()
        self.limiting_waveform_segments.setRange(1, 6)
        set_tip(self.limiting_waveform_segments, "Number of limiting zoom plots to save.")
        layout.addRow("Limiting Zoom Segments", self.limiting_waveform_segments)

        self.nr_cutoff_hz = QtWidgets.QDoubleSpinBox()
        self.nr_cutoff_hz.setRange(1000.0, 8000.0)
        self.nr_cutoff_hz.setSingleStep(100.0)
        set_tip(self.nr_cutoff_hz, "Cutoff frequency for NR detection.")
        layout.addRow("NR Cutoff (Hz)", self.nr_cutoff_hz)

        self.nr_drop_db = QtWidgets.QDoubleSpinBox()
        self.nr_drop_db.setRange(1.0, 20.0)
        self.nr_drop_db.setSingleStep(0.5)
        set_tip(self.nr_drop_db, "High-band drop vs reference to flag NR.")
        layout.addRow("NR Drop vs Reference (dB)", self.nr_drop_db)

        self.nr_ratio_db = QtWidgets.QDoubleSpinBox()
        self.nr_ratio_db.setRange(3.0, 30.0)
        self.nr_ratio_db.setSingleStep(0.5)
        set_tip(self.nr_ratio_db, "Mid vs high-band ratio to flag NR.")
        layout.addRow("NR High/Mid Ratio (dB)", self.nr_ratio_db)

        self.glitch_diff_threshold = QtWidgets.QDoubleSpinBox()
        self.glitch_diff_threshold.setRange(0.1, 1.0)
        self.glitch_diff_threshold.setSingleStep(0.05)
        set_tip(self.glitch_diff_threshold, "Sample-to-sample jump threshold for pops/crackles.")
        layout.addRow("Glitch Diff Threshold", self.glitch_diff_threshold)

        self.glitch_max_count = QtWidgets.QSpinBox()
        self.glitch_max_count.setRange(1, 100)
        set_tip(self.glitch_max_count, "Max number of glitch timestamps to list.")
        layout.addRow("Glitch Max Count", self.glitch_max_count)

        self.clip_heatmap_block_seconds = QtWidgets.QDoubleSpinBox()
        self.clip_heatmap_block_seconds.setRange(1.0, 30.0)
        self.clip_heatmap_block_seconds.setSingleStep(1.0)
        set_tip(self.clip_heatmap_block_seconds, "Block size for clipping heatmap.")
        layout.addRow("Clipping Heatmap Block (s)", self.clip_heatmap_block_seconds)

        self.true_peak_dbfs = QtWidgets.QDoubleSpinBox()
        self.true_peak_dbfs.setRange(-3.0, 0.0)
        self.true_peak_dbfs.setSingleStep(0.1)
        set_tip(self.true_peak_dbfs, "True-peak threshold for clipping warnings.")
        layout.addRow("True Peak Threshold (dBFS)", self.true_peak_dbfs)

        self.brickwall_dr_db = QtWidgets.QDoubleSpinBox()
        self.brickwall_dr_db.setRange(2.0, 12.0)
        set_tip(self.brickwall_dr_db, "Crest factor below which audio is brickwalled.")
        layout.addRow("Brickwall DR (dB)", self.brickwall_dr_db)

        self.target_dr_min = QtWidgets.QDoubleSpinBox()
        self.target_dr_min.setRange(6.0, 16.0)
        set_tip(self.target_dr_min, "Minimum target crest factor for scoring.")
        layout.addRow("Target DR Min", self.target_dr_min)

        self.target_dr_max = QtWidgets.QDoubleSpinBox()
        self.target_dr_max.setRange(8.0, 20.0)
        set_tip(self.target_dr_max, "Maximum target crest factor for scoring.")
        layout.addRow("Target DR Max", self.target_dr_max)

        self.dialog_band_low_hz = QtWidgets.QSpinBox()
        self.dialog_band_low_hz.setRange(100, 1000)
        set_tip(self.dialog_band_low_hz, "Dialog band low edge.")
        layout.addRow("Dialog Band Low (Hz)", self.dialog_band_low_hz)

        self.dialog_band_high_hz = QtWidgets.QSpinBox()
        self.dialog_band_high_hz.setRange(1500, 6000)
        set_tip(self.dialog_band_high_hz, "Dialog band high edge.")
        layout.addRow("Dialog Band High (Hz)", self.dialog_band_high_hz)

        self.presence_band_low_hz = QtWidgets.QSpinBox()
        self.presence_band_low_hz.setRange(1500, 8000)
        set_tip(self.presence_band_low_hz, "Presence band low edge.")
        layout.addRow("Presence Band Low (Hz)", self.presence_band_low_hz)

        self.presence_band_high_hz = QtWidgets.QSpinBox()
        self.presence_band_high_hz.setRange(4000, 16000)
        set_tip(self.presence_band_high_hz, "Presence band high edge.")
        layout.addRow("Presence Band High (Hz)", self.presence_band_high_hz)

        self.dialog_balance_warn_db = QtWidgets.QDoubleSpinBox()
        self.dialog_balance_warn_db.setRange(2.0, 15.0)
        self.dialog_balance_warn_db.setSingleStep(0.5)
        set_tip(self.dialog_balance_warn_db, "Dialog/presence balance warning threshold.")
        layout.addRow("Dialog Balance Warn (dB)", self.dialog_balance_warn_db)

        self.loudness_diff_warn_db = QtWidgets.QDoubleSpinBox()
        self.loudness_diff_warn_db.setRange(0.5, 10.0)
        self.loudness_diff_warn_db.setSingleStep(0.5)
        set_tip(self.loudness_diff_warn_db, "Loudness offset warning threshold vs reference.")
        layout.addRow("Loudness Diff Warn (dB)", self.loudness_diff_warn_db)

        self.mastering_diff_penalty_db = QtWidgets.QDoubleSpinBox()
        self.mastering_diff_penalty_db.setRange(0.5, 10.0)
        self.mastering_diff_penalty_db.setSingleStep(0.5)
        set_tip(self.mastering_diff_penalty_db, "Penalty per dB of EQ difference vs reference.")
        layout.addRow("Mastering Diff Penalty", self.mastering_diff_penalty_db)

        self.dialogue_clarity_penalty = QtWidgets.QDoubleSpinBox()
        self.dialogue_clarity_penalty.setRange(5.0, 60.0)
        self.dialogue_clarity_penalty.setSingleStep(5.0)
        set_tip(self.dialogue_clarity_penalty, "Penalty for center-channel NR/clipping.")
        layout.addRow("Dialogue Clarity Penalty", self.dialogue_clarity_penalty)

        self.pair_corr_fake_threshold = QtWidgets.QDoubleSpinBox()
        self.pair_corr_fake_threshold.setRange(0.8, 0.999)
        self.pair_corr_fake_threshold.setSingleStep(0.005)
        self.pair_corr_fake_threshold.setDecimals(3)
        set_tip(
            self.pair_corr_fake_threshold,
            "Per-pair channel correlation (FL-FR, FC-FL, BL-BR) above which a "
            "multichannel track is judged a matrix upmix. True discrete mixes "
            "measure well under 0.9 on real content.",
        )
        layout.addRow("Fake Pair Corr Threshold", self.pair_corr_fake_threshold)

        self.lfe_dead_rms_db = QtWidgets.QDoubleSpinBox()
        self.lfe_dead_rms_db.setRange(-160.0, -40.0)
        self.lfe_dead_rms_db.setSingleStep(5.0)
        set_tip(
            self.lfe_dead_rms_db,
            "LFE RMS below this over the whole runtime = dead channel.",
        )
        layout.addRow("Dead LFE RMS (dB)", self.lfe_dead_rms_db)

        self.decode_error_limit = QtWidgets.QSpinBox()
        self.decode_error_limit.setRange(1, 100000)
        set_tip(
            self.decode_error_limit,
            "Decoder-error lines from ffmpeg at or above this count mark the "
            "stream as damaged (a couple of errors at stream start can be benign).",
        )
        layout.addRow("Decode Error Limit", self.decode_error_limit)

        self.decode_duration_tolerance_s = QtWidgets.QDoubleSpinBox()
        self.decode_duration_tolerance_s.setRange(0.05, 10.0)
        self.decode_duration_tolerance_s.setSingleStep(0.1)
        set_tip(
            self.decode_duration_tolerance_s,
            "Decoded audio shorter/longer than the container claims by more than "
            "this = dropped frames, even when ffmpeg reported no errors.",
        )
        layout.addRow("Decode Duration Tolerance (s)", self.decode_duration_tolerance_s)

        self.sync_step_warn_ms = QtWidgets.QDoubleSpinBox()
        self.sync_step_warn_ms.setRange(1.0, 500.0)
        self.sync_step_warn_ms.setSingleStep(1.0)
        set_tip(
            self.sync_step_warn_ms,
            "Warn when per-chunk alignment offsets disagree across the runtime "
            "by more than this (a splice/step breaks single-offset comparison).",
        )
        layout.addRow("Sync Step Warn (ms)", self.sync_step_warn_ms)

        self.mel_bins = QtWidgets.QSpinBox()
        self.mel_bins.setRange(32, 512)
        set_tip(self.mel_bins, "Number of mel bins for spectrogram rendering.")
        layout.addRow("Mel Bins", self.mel_bins)

        weights_group = QtWidgets.QGroupBox("Scoring Weights (%)")
        weights_layout = QtWidgets.QFormLayout(weights_group)
        self.weight_frequency = QtWidgets.QSpinBox()
        self.weight_frequency.setRange(0, 100)
        set_tip(self.weight_frequency, "Weight for bandwidth-related score.")
        weights_layout.addRow("Frequency", self.weight_frequency)
        self.weight_dynamic_range = QtWidgets.QSpinBox()
        self.weight_dynamic_range.setRange(0, 100)
        set_tip(self.weight_dynamic_range, "Weight for dynamics/crest factor score.")
        weights_layout.addRow("Dynamic Range", self.weight_dynamic_range)
        self.weight_cleanliness = QtWidgets.QSpinBox()
        self.weight_cleanliness.setRange(0, 100)
        set_tip(self.weight_cleanliness, "Weight for clipping/cleanliness score.")
        weights_layout.addRow("Cleanliness", self.weight_cleanliness)
        self.weight_efficiency = QtWidgets.QSpinBox()
        self.weight_efficiency.setRange(0, 100)
        set_tip(self.weight_efficiency, "Weight for size/efficiency score.")
        weights_layout.addRow("Efficiency", self.weight_efficiency)
        self.weight_format = QtWidgets.QSpinBox()
        self.weight_format.setRange(0, 100)
        set_tip(self.weight_format, "Weight for codec/format score.")
        weights_layout.addRow("Format", self.weight_format)
        self.weight_dialogue = QtWidgets.QSpinBox()
        self.weight_dialogue.setRange(0, 100)
        set_tip(self.weight_dialogue, "Weight for dialogue clarity score.")
        weights_layout.addRow("Dialogue", self.weight_dialogue)
        self.weight_mastering = QtWidgets.QSpinBox()
        self.weight_mastering.setRange(0, 100)
        set_tip(self.weight_mastering, "Weight for mastering accuracy score.")
        weights_layout.addRow("Mastering", self.weight_mastering)
        layout.addRow(weights_group)

        scroll_area.setWidget(content)

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
        self.eq_muffle_drop_db.setValue(settings.get("eq_muffle_drop_db", DEFAULTS["eq_muffle_drop_db"]))
        self.eq_boom_boost_db.setValue(settings.get("eq_boom_boost_db", DEFAULTS["eq_boom_boost_db"]))
        self.eq_muffle_low_hz.setValue(int(settings.get("eq_muffle_low_hz", DEFAULTS["eq_muffle_low_hz"])))
        self.eq_muffle_high_hz.setValue(
            int(settings.get("eq_muffle_high_hz", DEFAULTS["eq_muffle_high_hz"]))
        )
        self.eq_boom_center_hz.setValue(
            int(settings.get("eq_boom_center_hz", DEFAULTS["eq_boom_center_hz"]))
        )
        self.eq_boom_band_hz.setValue(int(settings.get("eq_boom_band_hz", DEFAULTS["eq_boom_band_hz"])))
        self.f0_segment_seconds.setValue(
            settings.get("f0_segment_seconds", DEFAULTS["f0_segment_seconds"])
        )
        self.f0_segment_offset_ratio.setValue(
            settings.get("f0_segment_offset_ratio", DEFAULTS["f0_segment_offset_ratio"])
        )
        self.pal_speed_ratio.setValue(settings.get("pal_speed_ratio", DEFAULTS["pal_speed_ratio"]))
        self.pitch_semitone_shift.setValue(
            settings.get("pitch_semitone_shift", DEFAULTS["pitch_semitone_shift"])
        )
        self.pitch_tolerance_ratio.setValue(
            settings.get("pitch_tolerance_ratio", DEFAULTS["pitch_tolerance_ratio"])
        )
        self.scc_min_match_confidence.setValue(
            settings.get("scc_min_match_confidence", DEFAULTS["scc_min_match_confidence"])
        )
        self.channel_swap_corr_threshold.setValue(
            settings.get("channel_swap_corr_threshold", DEFAULTS["channel_swap_corr_threshold"])
        )
        self.lfe_rolloff_hz.setValue(int(settings.get("lfe_rolloff_hz", DEFAULTS["lfe_rolloff_hz"])))
        self.lfe_high_ratio_db.setValue(settings.get("lfe_high_ratio_db", DEFAULTS["lfe_high_ratio_db"]))
        self.limiting_window_ms.setValue(
            int(settings.get("limiting_window_ms", DEFAULTS["limiting_window_ms"]))
        )
        self.limiting_ratio.setValue(settings.get("limiting_ratio", DEFAULTS["limiting_ratio"]))
        self.limiting_heatmap_block_seconds.setValue(
            settings.get("limiting_heatmap_block_seconds", DEFAULTS["limiting_heatmap_block_seconds"])
        )
        self.limiting_waveform_segments.setValue(
            int(settings.get("limiting_waveform_segments", DEFAULTS["limiting_waveform_segments"]))
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
        self.dialog_band_low_hz.setValue(
            int(settings.get("dialog_band_low_hz", DEFAULTS["dialog_band_low_hz"]))
        )
        self.dialog_band_high_hz.setValue(
            int(settings.get("dialog_band_high_hz", DEFAULTS["dialog_band_high_hz"]))
        )
        self.presence_band_low_hz.setValue(
            int(settings.get("presence_band_low_hz", DEFAULTS["presence_band_low_hz"]))
        )
        self.presence_band_high_hz.setValue(
            int(settings.get("presence_band_high_hz", DEFAULTS["presence_band_high_hz"]))
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
        self.pair_corr_fake_threshold.setValue(
            settings.get("pair_corr_fake_threshold", DEFAULTS["pair_corr_fake_threshold"])
        )
        self.lfe_dead_rms_db.setValue(
            settings.get("lfe_dead_rms_db", DEFAULTS["lfe_dead_rms_db"])
        )
        self.decode_error_limit.setValue(
            int(settings.get("decode_error_limit", DEFAULTS["decode_error_limit"]))
        )
        self.decode_duration_tolerance_s.setValue(
            settings.get(
                "decode_duration_tolerance_s", DEFAULTS["decode_duration_tolerance_s"]
            )
        )
        self.sync_step_warn_ms.setValue(
            settings.get("sync_step_warn_ms", DEFAULTS["sync_step_warn_ms"])
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
            "eq_muffle_drop_db": self.eq_muffle_drop_db.value(),
            "eq_boom_boost_db": self.eq_boom_boost_db.value(),
            "eq_muffle_low_hz": self.eq_muffle_low_hz.value(),
            "eq_muffle_high_hz": self.eq_muffle_high_hz.value(),
            "eq_boom_center_hz": self.eq_boom_center_hz.value(),
            "eq_boom_band_hz": self.eq_boom_band_hz.value(),
            "f0_segment_seconds": self.f0_segment_seconds.value(),
            "f0_segment_offset_ratio": self.f0_segment_offset_ratio.value(),
            "pal_speed_ratio": self.pal_speed_ratio.value(),
            "pitch_semitone_shift": self.pitch_semitone_shift.value(),
            "pitch_tolerance_ratio": self.pitch_tolerance_ratio.value(),
            "scc_min_match_confidence": self.scc_min_match_confidence.value(),
            "channel_swap_corr_threshold": self.channel_swap_corr_threshold.value(),
            "lfe_rolloff_hz": self.lfe_rolloff_hz.value(),
            "lfe_high_ratio_db": self.lfe_high_ratio_db.value(),
            "limiting_window_ms": self.limiting_window_ms.value(),
            "limiting_ratio": self.limiting_ratio.value(),
            "limiting_heatmap_block_seconds": self.limiting_heatmap_block_seconds.value(),
            "limiting_waveform_segments": self.limiting_waveform_segments.value(),
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
            "pair_corr_fake_threshold": self.pair_corr_fake_threshold.value(),
            "lfe_dead_rms_db": self.lfe_dead_rms_db.value(),
            "decode_error_limit": self.decode_error_limit.value(),
            "decode_duration_tolerance_s": self.decode_duration_tolerance_s.value(),
            "sync_step_warn_ms": self.sync_step_warn_ms.value(),
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
        self.log_box.clear()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self._set_reference_index(None)
        self._latest_results = []
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
        for label in self.delta_eq_labels:
            label.setText("Delta EQ map unavailable")
            label.setPixmap(QtGui.QPixmap())
        for label in self.limiting_heatmap_labels:
            label.setText("Limiting heatmap unavailable")
            label.setPixmap(QtGui.QPixmap())
        for label in self.limiting_waveform_labels:
            label.setText("Waveform zoom unavailable")
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
        self.log_box.append("Starting analysis...")
        self.progress_bar.setRange(0, 0)
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

    def _export_results(self):
        if not getattr(self, "_latest_results", None):
            QtWidgets.QMessageBox.information(
                self,
                "No results",
                "Run an analysis before exporting results.",
            )
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export Results",
            os.path.expanduser("~/audio_comparison_results.json"),
            "JSON (*.json)",
        )
        if not path:
            return
        export_payload = {
            "settings": self._gather_settings(),
            "results": self._latest_results,
        }
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(export_payload, handle, indent=2)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, "Export failed", str(exc))
            return
        self.log_box.append(f"Exported results to {path}")

    def _on_analysis_complete(self, results: list[dict]):
        self.run_button.setEnabled(True)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self.log_box.append("Analysis complete.")
        self._latest_results = results
        self._populate_results(results)
        self._update_summary(results)
        self._load_spectrograms(results)
        self._load_forensics(results)

    def _on_error(self, message: str):
        self.run_button.setEnabled(True)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.log_box.append(f"Error: {message}")
        self.summary_box.setText(f"Error: {message}")

    def _populate_results(self, results: list[dict]):
        for labels in self.file_cards:
            for label in labels.values():
                label.setText("-")

        for idx, result in enumerate(results[:4]):
            labels = self.file_cards[idx]
            flags = []
            if result.get("disqualified"):
                flags.append("DISQUALIFIED")
            if result.get("decode_damaged"):
                flags.append("Corrupt stream")
            if result.get("lfe_dead"):
                flags.append("Dead LFE")
            if result.get("sync_step_detected"):
                flags.append("Sync step")
            if result.get("reference_path") and result.get("path") == result.get("reference_path"):
                flags.append("Reference")
            if result.get("reencode_detected"):
                flags.append("RE-ENCODE DETECTED")
            if result.get("shelf_detected"):
                flags.append("Spectral shelf")
            if result.get("nr_filtered") or result.get("center_nr_filtered"):
                flags.append("Excessive NR/Filtered")
            if result.get("eq_warnings"):
                flags.append("EQ delta warning")
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
            if result.get("surround_swap_detected"):
                flags.append("Surround swap")
            if result.get("lfe_rolloff_error"):
                flags.append("LFE roll-off")
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
            if result.get("speed_shift_detected"):
                flags.append("PAL speed shift")
            if result.get("pitch_shift_detected"):
                flags.append("Pitch shift")
            if result.get("limiting_segments"):
                flags.append("Limiting hotspots")
            if result.get("alignment_failed"):
                flags.append("Alignment failed")

            labels["name"].setText(os.path.basename(result["path"]))
            codec_name = result.get("codec_name")
            codec_label = f" | {codec_name}" if codec_name else ""
            labels["info"].setText(
                f"{result['channels']}ch @ {result['sample_rate']} Hz | "
                f"{result.get('bitrate_kbps', 0):.0f} kbps{codec_label} | {result['file_size_mb']:.1f} MB"
                if result.get("bitrate_kbps")
                else f"{result['channels']}ch @ {result['sample_rate']} Hz{codec_label} | {result['file_size_mb']:.1f} MB"
            )
            if result.get("decode_damaged"):
                regions = result.get("damaged_regions") or []
                integrity = (
                    f"DAMAGED: {result.get('decode_errors', 0)} decoder errors, "
                    f"{abs(result.get('duration_mismatch_s', 0.0)):.1f}s missing"
                )
                if regions:
                    region_text = ", ".join(f"{s:.0f}-{e:.0f}s" for s, e in regions[:3])
                    integrity += f" (~{region_text})"
                labels["integrity"].setText(integrity)
            elif result.get("decode_errors"):
                labels["integrity"].setText(
                    f"OK ({result['decode_errors']} decoder errors, below limit)"
                )
            else:
                labels["integrity"].setText("OK")
            labels["dr"].setText(f"{result['dr_db']:.1f} dB (blocks {result['dr_blocks_used']})")
            labels["dr_score"].setText(f"{result['dr_score']:.1f}")
            labels["lra"].setText(f"{result.get('loudness_range_db', 0.0):.1f} dB")
            labels["balance"].setText(f"{result.get('dialog_balance_db', 0.0):.1f} dB")
            labels["dialogue"].setText(f"{result.get('dialogue_score', 0.0):.1f}")
            if result.get("reference_path"):
                labels["mastering"].setText(f"{result.get('mastering_score', 0.0):.1f}")
            else:
                labels["mastering"].setText("N/A")
            eq_text = "None"
            if result.get("eq_warnings"):
                eq_text = ", ".join(result.get("eq_warnings"))
            eq_text += f" (Δ2-7k {result.get('eq_muffle_db', 0.0):+.1f} dB, "
            eq_text += f"Δ120 {result.get('eq_boom_db', 0.0):+.1f} dB)"
            labels["eq"].setText(eq_text)
            pitch_ratio = result.get("pitch_ratio")
            if pitch_ratio:
                labels["pitch"].setText(f"{pitch_ratio:.4f}x")
            else:
                labels["pitch"].setText("N/A")
            channel_issues = []
            if result.get("fake_multichannel"):
                reasons = result.get("fake_reasons") or []
                channel_issues.append(
                    "FAKE MULTICHANNEL: " + "; ".join(reasons) if reasons else "FAKE MULTICHANNEL"
                )
            elif result.get("fake_reasons"):
                channel_issues.append("; ".join(result["fake_reasons"]))
            elif result.get("lfe_dead"):
                channel_issues.append("Dead LFE")
            if result.get("surround_swap_detected"):
                channel_issues.append("Surround swap")
            if result.get("lfe_rolloff_error"):
                channel_issues.append("LFE roll-off")
            labels["channel"].setText(", ".join(channel_issues) if channel_issues else "OK")
            if result.get("reference_path") and result.get("path") != result.get("reference_path"):
                alignment_text = (
                    f"{result.get('alignment_offset_s', 0.0):+.3f}s "
                    f"(conf {result.get('alignment_confidence', 0.0):.1f})"
                )
                if result.get("sync_step_detected"):
                    alignment_text += (
                        f" | STEP {result.get('sync_step_delta_ms', 0.0):+.1f} ms "
                        f"at ~{(result.get('sync_step_time_s') or 0.0):.0f}s"
                    )
                labels["alignment"].setText(alignment_text)
            else:
                labels["alignment"].setText("N/A")
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
            limiting_segments = result.get("limiting_segments") or []
            if limiting_segments:
                sample = ", ".join(f"{start:.1f}-{end:.1f}s" for start, end in limiting_segments[:3])
                more = "…" if len(limiting_segments) > 3 else ""
                labels["limiting"].setText(f"{sample}{more}")
            else:
                labels["limiting"].setText("None")
            labels["cutoff"].setText(f"{result['freq_cutoff_hz'] / 1000:.1f}")
            labels["grade"].setText(result.get("quality_grade", "-"))
            labels["score"].setText(f"{result['score']:.1f}")
            labels["flags"].setText(", ".join(flags) if flags else "None")

    def _update_summary(self, results: list[dict]):
        lines = []
        for rank, result in enumerate(results, start=1):
            prefix = "[DQ] " if result.get("disqualified") else ""
            lines.append(
                f"Ranked #{rank}: {prefix}{os.path.basename(result['path'])} - "
                f"{result['summary']} (Score {result['score']:.1f})"
            )
        self.summary_box.setText("\n".join(lines))
        if results:
            clean = [r for r in results if not r.get("disqualified")]
            disqualified = [r for r in results if r.get("disqualified")]
            if clean:
                best = clean[0]
                verdict = (
                    f"Best Copy: {os.path.basename(best['path'])}\n"
                    f"Reason: {best['summary']} (Grade {best.get('quality_grade', '-')}, "
                    f"Score {best['score']:.1f})"
                )
            else:
                verdict = "No usable track: every file was disqualified."
            for result in disqualified:
                reasons = "; ".join(result.get("disqualify_reasons") or [])
                verdict += f"\nDisqualified: {os.path.basename(result['path'])} - {reasons}"
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
        for label in self.delta_eq_labels:
            label.setText("Delta EQ map unavailable")
            label.setPixmap(QtGui.QPixmap())
        for label in self.limiting_heatmap_labels:
            label.setText("Limiting heatmap unavailable")
            label.setPixmap(QtGui.QPixmap())
        for label in self.limiting_waveform_labels:
            label.setText("Waveform zoom unavailable")
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

            delta_path = result.get("delta_eq_path")
            if delta_path and os.path.exists(delta_path):
                pixmap = QtGui.QPixmap(delta_path)
                if not pixmap.isNull():
                    scaled = pixmap.scaled(400, 200, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
                    self.delta_eq_labels[idx].setPixmap(scaled)
                    self.delta_eq_labels[idx].setText("")

            limiting_path = result.get("limiting_heatmap_path")
            if limiting_path and os.path.exists(limiting_path):
                pixmap = QtGui.QPixmap(limiting_path)
                if not pixmap.isNull():
                    scaled = pixmap.scaled(400, 140, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
                    self.limiting_heatmap_labels[idx].setPixmap(scaled)
                    self.limiting_heatmap_labels[idx].setText("")

            zoom_paths = result.get("limiting_waveform_paths") or []
            if zoom_paths:
                zoom_path = zoom_paths[0]
                if os.path.exists(zoom_path):
                    pixmap = QtGui.QPixmap(zoom_path)
                    if not pixmap.isNull():
                        scaled = pixmap.scaled(400, 140, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
                        self.limiting_waveform_labels[idx].setPixmap(scaled)
                        self.limiting_waveform_labels[idx].setText("")

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
