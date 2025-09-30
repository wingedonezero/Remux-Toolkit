# remux_toolkit/tools/video_ab_comparator/gui/settings_dialog.py

from PyQt6 import QtWidgets, QtCore

class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("A/B Comparator Settings")
        self.settings = settings.copy() # Work on a copy
        self.layout = QtWidgets.QFormLayout(self)
        self.controls = {}

        # Add controls for each detector's sensitivity
        self._add_slider("Upscaled Video", "upscale_detector_threshold", 0, 100)
        self._add_slider("Interlace Combing", "combing_detector_threshold", 0, 100)
        self._add_slider("Compression Blocking", "blocking_detector_threshold", 0, 100)
        self._add_slider("Color Banding", "banding_detector_threshold", 0, 100)
        self._add_slider("Ringing / Halos", "ringing_detector_threshold", 0, 100)
        self._add_slider("Chroma Shift", "chroma_shift_detector_threshold", 0, 100)
        self._add_slider("Rainbowing", "rainbowing_detector_threshold", 0, 100)
        self._add_slider("Over-DNR", "dnr_detector_threshold", 0, 100)
        self._add_slider("Excessive Sharpening", "sharpening_detector_threshold", 0, 100)


        # OK and Cancel buttons
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.layout.addRow(buttons)

    def _add_slider(self, label: str, setting_key: str, min_val: int, max_val: int):
        slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        slider.setRange(min_val, max_val)
        slider.setValue(int(self.settings.get(setting_key, 75)))

        label_val = QtWidgets.QLabel(f"{slider.value()}%")
        slider.valueChanged.connect(lambda v, lbl=label_val: lbl.setText(f"{v}%"))

        hbox = QtWidgets.QHBoxLayout()
        hbox.addWidget(slider)
        hbox.addWidget(label_val)

        self.layout.addRow(f"{label} Threshold:", hbox)
        self.controls[setting_key] = slider

    def get_settings(self) -> dict:
        """Returns the updated settings dictionary."""
        for key, slider in self.controls.items():
            self.settings[key] = slider.value()
        return self.settings
