# remux_toolkit/tools/ffmpeg_dvd_gui/gui/prefs_dialog.py
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QVBoxLayout,
    QLineEdit, QPushButton, QSpinBox, QCheckBox, QFileDialog, QComboBox,
    QGroupBox, QLabel
)


class PrefsDialog(QDialog):
    """Preferences dialog for FFmpeg DVD Remuxer."""

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("FFmpeg DVD Remuxer Preferences")
        self.settings = settings
        self.setMinimumWidth(640)

        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        # Output settings group
        output_group = QGroupBox("Output Settings")
        output_form = QFormLayout()

        self.out_edit = QLineEdit(self.settings.get("output_root", ""))
        btn_browse_out = QPushButton("Browse...")
        btn_browse_out.clicked.connect(self._browse_out)
        row_out = QHBoxLayout()
        row_out.addWidget(self.out_edit)
        row_out.addWidget(btn_browse_out)
        output_form.addRow("Output Root:", row_out)

        self.min_spin = QSpinBox()
        self.min_spin.setRange(0, 99999)
        self.min_spin.setValue(int(self.settings.get("minlength", 60)))
        self.min_spin.setSuffix(" s minimum title length")
        output_form.addRow("Min title length:", self.min_spin)

        output_group.setLayout(output_form)
        layout.addWidget(output_group)

        # Tool paths group
        tools_group = QGroupBox("Tool Paths")
        tools_form = QFormLayout()

        self.ffmpeg_edit = QLineEdit(self.settings.get("ffmpeg_path", "ffmpeg"))
        btn_browse_ffmpeg = QPushButton("Browse...")
        btn_browse_ffmpeg.clicked.connect(self._browse_ffmpeg)
        row_ffmpeg = QHBoxLayout()
        row_ffmpeg.addWidget(self.ffmpeg_edit)
        row_ffmpeg.addWidget(btn_browse_ffmpeg)
        tools_form.addRow("ffmpeg path:", row_ffmpeg)

        self.ffprobe_edit = QLineEdit(self.settings.get("ffprobe_path", "ffprobe"))
        btn_browse_ffprobe = QPushButton("Browse...")
        btn_browse_ffprobe.clicked.connect(self._browse_ffprobe)
        row_ffprobe = QHBoxLayout()
        row_ffprobe.addWidget(self.ffprobe_edit)
        row_ffprobe.addWidget(btn_browse_ffprobe)
        tools_form.addRow("ffprobe path:", row_ffprobe)

        self.mkvpropedit_edit = QLineEdit(self.settings.get("mkvpropedit_path", "mkvpropedit"))
        btn_browse_mkvpropedit = QPushButton("Browse...")
        btn_browse_mkvpropedit.clicked.connect(self._browse_mkvpropedit)
        row_mkvpropedit = QHBoxLayout()
        row_mkvpropedit.addWidget(self.mkvpropedit_edit)
        row_mkvpropedit.addWidget(btn_browse_mkvpropedit)
        tools_form.addRow("mkvpropedit path:", row_mkvpropedit)

        tools_group.setLayout(tools_form)
        layout.addWidget(tools_group)

        # DVD options group
        dvd_group = QGroupBox("DVD Options")
        dvd_form = QFormLayout()

        self.chk_preindex = QCheckBox("Enable preindex (2-pass for accurate chapters)")
        self.chk_preindex.setChecked(self.settings.get("enable_preindex", True))
        dvd_form.addRow("", self.chk_preindex)

        self.chk_trim = QCheckBox("Trim padding cells at start")
        self.chk_trim.setChecked(self.settings.get("trim_padding", True))
        dvd_form.addRow("", self.chk_trim)

        self.region_spin = QSpinBox()
        self.region_spin.setRange(0, 8)
        self.region_spin.setValue(int(self.settings.get("default_region", 0)))
        self.region_spin.setSpecialValueText("Auto (0)")
        dvd_form.addRow("Region code:", self.region_spin)

        dvd_group.setLayout(dvd_form)
        layout.addWidget(dvd_group)

        # Chapter options group
        chapter_group = QGroupBox("Chapter Options")
        chapter_form = QFormLayout()

        self.chapter_combo = QComboBox()
        self.chapter_combo.addItem("Numbered (Chapter 1, Chapter 2, ...)", "numbered")
        self.chapter_combo.addItem("Unnamed (keep FFmpeg default)", "unnamed")
        current_naming = self.settings.get("chapter_naming", "numbered")
        idx = self.chapter_combo.findData(current_naming)
        if idx >= 0:
            self.chapter_combo.setCurrentIndex(idx)
        chapter_form.addRow("Chapter naming:", self.chapter_combo)

        note_label = QLabel("Note: Numbered chapters require mkvpropedit (from mkvtoolnix)")
        note_label.setStyleSheet("color: gray; font-size: 11px;")
        chapter_form.addRow("", note_label)

        chapter_group.setLayout(chapter_form)
        layout.addWidget(chapter_group)

        # Advanced options
        advanced_group = QGroupBox("Advanced")
        advanced_form = QFormLayout()

        self.extra_args = QLineEdit(self.settings.get("extra_args", ""))
        self.extra_args.setPlaceholderText("e.g. -threads 4")
        advanced_form.addRow("Extra ffmpeg args:", self.extra_args)

        advanced_group.setLayout(advanced_form)
        layout.addWidget(advanced_group)

        # Dialog buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse_out(self):
        d = QFileDialog.getExistingDirectory(self, "Choose output root", self.out_edit.text())
        if d:
            self.out_edit.setText(d)

    def _browse_ffmpeg(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Locate ffmpeg", self.ffmpeg_edit.text() or "/usr/bin", "All (*)"
        )
        if f:
            self.ffmpeg_edit.setText(f)

    def _browse_ffprobe(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Locate ffprobe", self.ffprobe_edit.text() or "/usr/bin", "All (*)"
        )
        if f:
            self.ffprobe_edit.setText(f)

    def _browse_mkvpropedit(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Locate mkvpropedit", self.mkvpropedit_edit.text() or "/usr/bin", "All (*)"
        )
        if f:
            self.mkvpropedit_edit.setText(f)

    def get_values(self) -> dict:
        return {
            "output_root": self.out_edit.text().strip(),
            "ffmpeg_path": self.ffmpeg_edit.text().strip() or "ffmpeg",
            "ffprobe_path": self.ffprobe_edit.text().strip() or "ffprobe",
            "mkvpropedit_path": self.mkvpropedit_edit.text().strip() or "mkvpropedit",
            "minlength": int(self.min_spin.value()),
            "enable_preindex": self.chk_preindex.isChecked(),
            "trim_padding": self.chk_trim.isChecked(),
            "default_region": int(self.region_spin.value()),
            "chapter_naming": self.chapter_combo.currentData(),
            "extra_args": self.extra_args.text().strip(),
        }
