# remux_toolkit/tools/ffmpeg_dvd_remuxer/gui/prefs_dialog.py
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSpinBox, QCheckBox, QDialogButtonBox, QFileDialog
)

class PrefsDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("FFmpeg DVD Remuxer Settings")
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)
        self.out_dir_edit = QLineEdit(self.config.get("default_output_directory", ""))
        self.out_dir_btn = QPushButton("Browse...")
        self.out_dir_btn.clicked.connect(self.browse_output_dir)
        out_dir_layout = QHBoxLayout()
        out_dir_layout.addWidget(self.out_dir_edit)
        out_dir_layout.addWidget(self.out_dir_btn)

        self.min_len_spin = QSpinBox()
        self.min_len_spin.setRange(0, 10000)
        self.min_len_spin.setSuffix(" seconds")
        self.min_len_spin.setValue(self.config.get("minimum_title_length", 120))
        min_len_layout = QHBoxLayout()
        min_len_layout.addWidget(QLabel("Minimum Title Length for 'Process All':"))
        min_len_layout.addWidget(self.min_len_spin)

        self.remove_eia_check = QCheckBox("Remove EIA-608 caption data from video stream")
        self.remove_eia_check.setChecked(self.config.get("remove_eia_608", True))

        self.ccextractor_check = QCheckBox("Run CCExtractor to create SRT subtitle track")
        self.ccextractor_check.setChecked(self.config.get("run_ccextractor", True))

        self.trim_padding_check = QCheckBox("Trim initial padding cells from DVD source (recommended)")
        self.trim_padding_check.setToolTip("Uses ffmpeg's -trim option to skip short filler cells at the start of a title.")
        self.trim_padding_check.setChecked(self.config.get("ffmpeg_trim_padding", True))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout.addWidget(QLabel("Default Output Directory:"))
        layout.addLayout(out_dir_layout)
        layout.addLayout(min_len_layout)
        layout.addWidget(self.remove_eia_check)
        layout.addWidget(self.ccextractor_check)
        layout.addWidget(self.trim_padding_check)
        layout.addWidget(buttons)

    def browse_output_dir(self):
        dir_name = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if dir_name: self.out_dir_edit.setText(dir_name)

    def get_values(self):
        return {
            "default_output_directory": self.out_dir_edit.text(),
            "minimum_title_length": self.min_len_spin.value(),
            "remove_eia_608": self.remove_eia_check.isChecked(),
            "run_ccextractor": self.ccextractor_check.isChecked(),
            "ffmpeg_trim_padding": self.trim_padding_check.isChecked(),
        }
