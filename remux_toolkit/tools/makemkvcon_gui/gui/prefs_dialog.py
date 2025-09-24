# remux_toolkit/tools/makemkvcon_gui/gui/prefs_dialog.py
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QSpinBox, QCheckBox, QFileDialog
)

class PrefsDialog(QDialog):
    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MakeMKVCon GUI Preferences")
        self.settings = settings
        self.setMinimumWidth(640)

        # --- NEWLY RESTORED WIDGETS ---
        self.out_edit = QLineEdit(self.settings["output_root"])
        btn_browse_out = QPushButton("Browse…")
        btn_browse_out.clicked.connect(self._browse_out)
        # --- END RESTORED WIDGETS ---

        self.mk_edit = QLineEdit(self.settings["makemkvcon_path"])
        btn_browse_mk = QPushButton("Browse…")
        btn_browse_mk.clicked.connect(self._browse_mk)

        self.min_spin = QSpinBox()
        self.min_spin.setRange(0, 99999)
        self.min_spin.setValue(int(self.settings["minlength"]))
        self.min_spin.setSuffix(" s minimum title length")

        self.prof_edit = QLineEdit(self.settings.get("profile_path", ""))
        btn_browse_prof = QPushButton("Browse…")
        btn_browse_prof.clicked.connect(self._browse_prof)

        self.extra_args = QLineEdit(self.settings.get("extra_args", ""))
        self.chk_human = QCheckBox("Human-friendly log")
        self.chk_human.setChecked(self.settings.get("human_log", True))
        self.chk_debugfile = QCheckBox("Write extra debug file (--debug)")
        self.chk_debugfile.setChecked(self.settings.get("enable_debugfile", False))
        self.chk_pct = QCheckBox("Show % progress while ripping")
        self.chk_pct.setChecked(self.settings.get("show_percent", True))
        self.chk_keep_raw = QCheckBox("Keep structured message file for debugging")
        self.chk_keep_raw.setChecked(self.settings.get("keep_structured_messages", False))

        form = QFormLayout()

        # --- NEWLY RESTORED LAYOUT ROW ---
        row_out = QHBoxLayout()
        row_out.addWidget(self.out_edit)
        row_out.addWidget(btn_browse_out)
        form.addRow("Output Root:", row_out)
        # --- END RESTORED ROW ---

        row_mk = QHBoxLayout()
        row_mk.addWidget(self.mk_edit)
        row_mk.addWidget(btn_browse_mk)
        form.addRow("makemkvcon path:", row_mk)

        form.addRow("Min title length:", self.min_spin)

        row_prof = QHBoxLayout()
        row_prof.addWidget(self.prof_edit)
        row_prof.addWidget(btn_browse_prof)
        form.addRow("Profile (optional):", row_prof)

        form.addRow("Extra makemkvcon args:", self.extra_args)
        form.addRow("", self.chk_human)
        form.addRow("", self.chk_debugfile)
        form.addRow("", self.chk_pct)
        form.addRow("", self.chk_keep_raw)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        from PyQt6.QtWidgets import QVBoxLayout
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    # --- NEWLY RESTORED BROWSE METHOD ---
    def _browse_out(self):
        d = QFileDialog.getExistingDirectory(self, "Choose output root", self.out_edit.text())
        if d: self.out_edit.setText(d)
    # --- END RESTORED METHOD ---

    def _browse_mk(self):
        f, _ = QFileDialog.getOpenFileName(self, "Locate makemkvcon", self.mk_edit.text() or "/usr/bin", "All (*)")
        if f: self.mk_edit.setText(f)

    def _browse_prof(self):
        f, _ = QFileDialog.getOpenFileName(self, "Choose MakeMKV profile (.xml)", "", "Profiles (*.xml *.mmcp *.mmcp.xml);;All files (*)")
        if f: self.prof_edit.setText(f)

    def get_values(self) -> dict:
        return {
            # --- NEWLY RESTORED SETTING ---
            "output_root": self.out_edit.text().strip(),
            # --- END RESTORED SETTING ---
            "makemkvcon_path": self.mk_edit.text().strip() or "makemkvcon",
            "minlength": int(self.min_spin.value()),
            "profile_path": self.prof_edit.text().strip(),
            "extra_args": self.extra_args.text().strip(),
            "human_log": self.chk_human.isChecked(),
            "enable_debugfile": self.chk_debugfile.isChecked(),
            "show_percent": self.chk_pct.isChecked(),
            "keep_structured_messages": self.chk_keep_raw.isChecked(),
        }
