# remux_toolkit/tools/video_ab_comparator/gui/settings_dialog.py

from PyQt6 import QtWidgets, QtCore

class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("A/B Comparator Settings")
        self.settings = settings.copy()
        self.layout = QtWidgets.QFormLayout(self)
        self.controls = {}

        # --- General Settings ---
        self._add_slider("Analysis Chunk Count", "analysis_chunk_count", 3, 20, is_percent=False)
        self.controls['analysis_chunk_duration'] = self._add_spinbox("Analysis Chunk Duration (seconds)", "analysis_chunk_duration", 1.0, 10.0, 0.5)

        # --- Checkboxes for Global Detectors ---
        self.controls['enable_audio_analysis'] = self._add_checkbox("Enable Audio Analysis", "enable_audio_analysis")
        self.controls['enable_interlace_detection'] = self._add_checkbox("Enable Interlace Detection", "enable_interlace_detection")
        self.controls['enable_cadence_detection'] = self._add_checkbox("Enable Cadence Detection", "enable_cadence_detection")

        # --- Advanced Audio Alignment Settings ---
        self.layout.addRow(QtWidgets.QLabel("<b>Advanced Audio Alignment</b>"))
        self.controls['use_advanced_alignment'] = self._add_checkbox("Use Advanced SCC Alignment (more accurate)", "use_advanced_alignment")

        # Audio language selection
        lang_label = QtWidgets.QLabel("Audio Language:")
        lang_combo = QtWidgets.QComboBox()
        lang_combo.addItems(["Auto (first track)", "jpn (Japanese)", "eng (English)", "ger (German)", "fra (French)", "spa (Spanish)"])
        current_lang = self.settings.get('align_audio_lang', 'jpn')
        if current_lang is None or current_lang == '':
            lang_combo.setCurrentIndex(0)
        elif current_lang == 'jpn':
            lang_combo.setCurrentIndex(1)
        elif current_lang == 'eng':
            lang_combo.setCurrentIndex(2)
        elif current_lang == 'ger':
            lang_combo.setCurrentIndex(3)
        elif current_lang == 'fra':
            lang_combo.setCurrentIndex(4)
        elif current_lang == 'spa':
            lang_combo.setCurrentIndex(5)
        self.layout.addRow(lang_label, lang_combo)
        self.controls['align_audio_lang_combo'] = lang_combo

        self.controls['align_chunk_count'] = self._add_spinbox("Align Chunk Count", "align_chunk_count", 5, 50, 1)
        self.controls['align_chunk_duration'] = self._add_spinbox("Align Chunk Duration (s)", "align_chunk_duration", 10.0, 60.0, 5.0)
        self.controls['align_min_match_pct'] = self._add_spinbox("Min Match % to Accept", "align_min_match_pct", 5.0, 50.0, 5.0)
        self.controls['align_scan_start_pct'] = self._add_spinbox("Scan Start %", "align_scan_start_pct", 0.0, 50.0, 5.0)
        self.controls['align_scan_end_pct'] = self._add_spinbox("Scan End %", "align_scan_end_pct", 50.0, 100.0, 5.0)
        self.controls['align_use_soxr'] = self._add_checkbox("Use High-Quality SOXR Resampling", "align_use_soxr")
        self.controls['align_peak_fit'] = self._add_checkbox("Use Sub-Sample Peak Fitting", "align_peak_fit")
        self.controls['use_frame_mapper'] = self._add_checkbox("Use VideoTimestamps for Frame-Perfect Mapping", "use_frame_mapper")

        # Delay selection strategy
        delay_label = QtWidgets.QLabel("Delay Selection:")
        delay_combo = QtWidgets.QComboBox()
        delay_combo.addItems(["first (First accepted chunk)", "median (Median of chunks)", "mean (Average of chunks)"])
        current_delay = self.settings.get('align_delay_selection', 'first')
        if current_delay == 'first':
            delay_combo.setCurrentIndex(0)
        elif current_delay == 'median':
            delay_combo.setCurrentIndex(1)
        elif current_delay == 'mean':
            delay_combo.setCurrentIndex(2)
        self.layout.addRow(delay_label, delay_combo)
        self.controls['align_delay_selection_combo'] = delay_combo

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.layout.addRow(buttons)

    def _add_spinbox(self, label: str, setting_key: str, min_val: float, max_val: float, step: float) -> QtWidgets.QDoubleSpinBox:
        spinner = QtWidgets.QDoubleSpinBox()
        spinner.setRange(min_val, max_val)
        spinner.setSingleStep(step)
        spinner.setValue(self.settings.get(setting_key, 2.0))
        self.layout.addRow(label, spinner)
        return spinner

    def _add_checkbox(self, label: str, setting_key: str) -> QtWidgets.QCheckBox:
        checkbox = QtWidgets.QCheckBox(label)
        checkbox.setChecked(self.settings.get(setting_key, True))
        self.layout.addRow(checkbox)
        return checkbox

    def _add_slider(self, label: str, setting_key: str, min_val: int, max_val: int, is_percent: bool = True):
        slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        slider.setRange(min_val, max_val)
        default_val = 8 if "chunk" in setting_key else 75
        slider.setValue(int(self.settings.get(setting_key, default_val)))

        label_suffix = "" if "chunk" in setting_key else "%"
        label_val = QtWidgets.QLabel(f"{slider.value()}{label_suffix}")
        slider.valueChanged.connect(lambda v, lbl=label_val: lbl.setText(f"{v}{label_suffix}"))

        hbox = QtWidgets.QHBoxLayout()
        hbox.addWidget(slider)
        hbox.addWidget(label_val)

        self.layout.addRow(f"{label}:", hbox)
        self.controls[setting_key] = slider

    def get_settings(self) -> dict:
        for key, control in self.controls.items():
            if isinstance(control, QtWidgets.QSlider):
                self.settings[key] = control.value()
            elif isinstance(control, QtWidgets.QCheckBox):
                self.settings[key] = control.isChecked()
            elif isinstance(control, QtWidgets.QDoubleSpinBox):
                self.settings[key] = control.value()
            elif isinstance(control, QtWidgets.QComboBox):
                # Handle combo boxes
                if key == 'align_audio_lang_combo':
                    idx = control.currentIndex()
                    lang_map = {0: None, 1: 'jpn', 2: 'eng', 3: 'ger', 4: 'fra', 5: 'spa'}
                    self.settings['align_audio_lang'] = lang_map.get(idx, None)
                elif key == 'align_delay_selection_combo':
                    idx = control.currentIndex()
                    delay_map = {0: 'first', 1: 'median', 2: 'mean'}
                    self.settings['align_delay_selection'] = delay_map.get(idx, 'first')
        return self.settings
