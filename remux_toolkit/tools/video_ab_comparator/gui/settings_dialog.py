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
        return self.settings
