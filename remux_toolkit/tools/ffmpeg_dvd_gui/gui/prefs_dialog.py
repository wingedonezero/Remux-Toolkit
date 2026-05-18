# remux_toolkit/tools/ffmpeg_dvd_gui/gui/prefs_dialog.py
import os
import subprocess

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
        self._check_all_tools()

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
        self.ffmpeg_edit.editingFinished.connect(self._on_ffmpeg_path_changed)
        btn_browse_ffmpeg = QPushButton("Browse...")
        btn_browse_ffmpeg.clicked.connect(self._browse_ffmpeg)
        row_ffmpeg = QHBoxLayout()
        row_ffmpeg.addWidget(self.ffmpeg_edit)
        row_ffmpeg.addWidget(btn_browse_ffmpeg)
        tools_form.addRow("ffmpeg path:", row_ffmpeg)

        self.ffmpeg_version_label = QLabel()
        self.ffmpeg_version_label.setStyleSheet("font-size: 11px;")
        self.ffmpeg_version_label.setWordWrap(True)
        tools_form.addRow("", self.ffmpeg_version_label)

        self.ffprobe_edit = QLineEdit(self.settings.get("ffprobe_path", "ffprobe"))
        self.ffprobe_edit.editingFinished.connect(self._on_ffprobe_path_changed)
        btn_browse_ffprobe = QPushButton("Browse...")
        btn_browse_ffprobe.clicked.connect(self._browse_ffprobe)
        row_ffprobe = QHBoxLayout()
        row_ffprobe.addWidget(self.ffprobe_edit)
        row_ffprobe.addWidget(btn_browse_ffprobe)
        tools_form.addRow("ffprobe path:", row_ffprobe)

        self.ffprobe_version_label = QLabel()
        self.ffprobe_version_label.setStyleSheet("font-size: 11px;")
        self.ffprobe_version_label.setWordWrap(True)
        tools_form.addRow("", self.ffprobe_version_label)

        self.mkvpropedit_edit = QLineEdit(self.settings.get("mkvpropedit_path", "mkvpropedit"))
        self.mkvpropedit_edit.editingFinished.connect(self._on_mkvpropedit_path_changed)
        btn_browse_mkvpropedit = QPushButton("Browse...")
        btn_browse_mkvpropedit.clicked.connect(self._browse_mkvpropedit)
        row_mkvpropedit = QHBoxLayout()
        row_mkvpropedit.addWidget(self.mkvpropedit_edit)
        row_mkvpropedit.addWidget(btn_browse_mkvpropedit)
        tools_form.addRow("mkvpropedit path:", row_mkvpropedit)

        self.mkvpropedit_version_label = QLabel()
        self.mkvpropedit_version_label.setStyleSheet("font-size: 11px;")
        self.mkvpropedit_version_label.setWordWrap(True)
        tools_form.addRow("", self.mkvpropedit_version_label)

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

        self.chk_max_mux_queue = QCheckBox("Increase muxer queue size (-max_muxing_queue_size 9999)")
        self.chk_max_mux_queue.setChecked(self.settings.get("max_muxing_queue_size", False))
        dvd_form.addRow("", self.chk_max_mux_queue)

        self.chk_avoid_neg_ts = QCheckBox("Shift timestamps to zero (-avoid_negative_ts make_zero)")
        self.chk_avoid_neg_ts.setChecked(self.settings.get("avoid_negative_ts", False))
        dvd_form.addRow("", self.chk_avoid_neg_ts)

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

        # Native Muxer panel — opt-in to libmkv_shim rip backend +
        # build/status controls.
        native_group = QGroupBox("Native Muxer (libmkv_shim)")
        native_form = QFormLayout()

        self.native_remux_check = QCheckBox(
            "Use native muxer instead of FFmpeg subprocess"
        )
        self.native_remux_check.setChecked(
            bool(self.settings.get("use_native_remux", False))
        )
        self.native_remux_check.setToolTip(
            "Routes rips through the libmkv_shim path (Python orchestration\n"
            "+ libebml/libmatroska direct writes). Opt-in until cross-\n"
            "validation against MakeMKV on the corpus is complete."
        )
        native_form.addRow("", self.native_remux_check)

        self.native_apply_trim_check = QCheckBox(
            "Apply cell-trim deciders (MakeMKV-style)"
        )
        self.native_apply_trim_check.setChecked(
            bool(self.settings.get("apply_trim", False))
        )
        self.native_apply_trim_check.setToolTip(
            "Runs the ported cell_validator + trim deciders on each title\n"
            "and skips non-content cells at the head/tail. Only effective\n"
            "with the native muxer."
        )
        native_form.addRow("", self.native_apply_trim_check)

        self.native_cc_check = QCheckBox(
            "Extract closed captions (EIA-608) when present"
        )
        self.native_cc_check.setChecked(
            bool(self.settings.get("include_closed_captions", True))
        )
        self.native_cc_check.setToolTip(
            "Uses ccextractor to extract line 21 CCs from the ripped MKV\n"
            "and merges them in as an SRT track via mkvmerge."
        )
        native_form.addRow("", self.native_cc_check)

        # Shim status + Rebuild
        self.shim_status_label = QLabel()
        self.shim_status_label.setStyleSheet("font-size: 11px;")
        self.shim_status_label.setWordWrap(True)
        self.shim_rebuild_btn = QPushButton("Rebuild Shim")
        self.shim_rebuild_btn.clicked.connect(self._on_rebuild_shim)
        shim_row = QHBoxLayout()
        shim_row.addWidget(self.shim_status_label, 1)
        shim_row.addWidget(self.shim_rebuild_btn)
        native_form.addRow("Shim status:", shim_row)
        self._refresh_shim_status()

        native_group.setLayout(native_form)
        layout.addWidget(native_group)

        # Dialog buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # -- Native shim controls --

    def _refresh_shim_status(self):
        """Probe libmkv_shim and update the status label."""
        try:
            from ..core.mux.native import get_shim_status, ShimStatus
        except Exception as e:
            self.shim_status_label.setText(f"shim module unavailable: {e}")
            self.shim_status_label.setStyleSheet("font-size: 11px; color: #cc3333;")
            return
        info = get_shim_status()
        if info.status == ShimStatus.OK:
            self.shim_status_label.setText(
                f"OK — {info.version or 'libmkv_shim'} ({info.so_path})"
            )
            self.shim_status_label.setStyleSheet(
                "font-size: 11px; color: #33aa33;"
            )
        elif info.status == ShimStatus.MISSING_HEADERS:
            self.shim_status_label.setText(
                "Missing dev headers (apt: libebml-dev libmatroska-dev). "
                f"{info.message or ''}"
            )
            self.shim_status_label.setStyleSheet(
                "font-size: 11px; color: #cc3333;"
            )
        elif info.status == ShimStatus.NOT_BUILT:
            self.shim_status_label.setText(
                "Not built — click Rebuild Shim to compile."
            )
            self.shim_status_label.setStyleSheet(
                "font-size: 11px; color: #cc8800;"
            )
        elif info.status == ShimStatus.STALE:
            self.shim_status_label.setText(
                "Stale — source is newer than the .so. Click Rebuild."
            )
            self.shim_status_label.setStyleSheet(
                "font-size: 11px; color: #cc8800;"
            )
        else:
            self.shim_status_label.setText(
                f"{info.status.value}: {info.message or ''}"
            )
            self.shim_status_label.setStyleSheet(
                "font-size: 11px; color: #cc3333;"
            )

    def _on_rebuild_shim(self):
        """Run the shim build script; show the result in-line."""
        try:
            from ..core.mux.native import build_shim
        except Exception as e:
            self.shim_status_label.setText(f"cannot import build_shim: {e}")
            self.shim_status_label.setStyleSheet(
                "font-size: 11px; color: #cc3333;"
            )
            return
        self.shim_rebuild_btn.setEnabled(False)
        self.shim_status_label.setText("Building...")
        # Capture output to display the tail in case of failure.
        lines: list[str] = []
        try:
            ok = build_shim(output_callback=lines.append)
        finally:
            self.shim_rebuild_btn.setEnabled(True)
        if ok:
            self._refresh_shim_status()
        else:
            tail = "\n".join(lines[-3:])
            self.shim_status_label.setText(f"Build failed: {tail or 'see log'}")
            self.shim_status_label.setStyleSheet(
                "font-size: 11px; color: #cc3333;"
            )

    # -- Version checking --

    def _get_tool_version(self, tool_path: str) -> tuple[bool, str]:
        """Get version string from a tool. Returns (found, version_string)."""
        for flag in ["-version", "--version"]:
            try:
                result = subprocess.run(
                    [tool_path, flag],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout:
                    return True, result.stdout.split('\n')[0].strip()
            except FileNotFoundError:
                return False, "Not found"
            except subprocess.TimeoutExpired:
                continue
            except Exception:
                continue
        return False, "Version check failed"

    def _check_dvdvideo_support(self, ffmpeg_path: str) -> bool:
        """Check if ffmpeg binary has dvdvideo demuxer support."""
        try:
            result = subprocess.run(
                [ffmpeg_path, "-hide_banner", "-demuxers"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return "dvdvideo" in result.stdout
        except Exception:
            pass
        return False

    def _set_version_label(self, label: QLabel, found: bool, version: str,
                           dvdvideo: bool | None = None):
        """Update a version label with color-coded status."""
        if not found:
            label.setText(f"Not found")
            label.setStyleSheet("font-size: 11px; color: #cc3333;")
            return

        parts = [version]
        if dvdvideo is True:
            parts.append("dvdvideo: yes")
        elif dvdvideo is False:
            parts.append("dvdvideo: NO (needs --enable-libdvdnav --enable-libdvdread)")

        label.setText(" | ".join(parts))
        if dvdvideo is False:
            label.setStyleSheet("font-size: 11px; color: #cc8800;")
        else:
            label.setStyleSheet("font-size: 11px; color: #33aa33;")

    def _check_ffmpeg_tool(self):
        """Check ffmpeg and update its version label."""
        path = self.ffmpeg_edit.text().strip() or "ffmpeg"
        found, version = self._get_tool_version(path)
        dvd = self._check_dvdvideo_support(path) if found else None
        self._set_version_label(self.ffmpeg_version_label, found, version, dvd)

    def _check_ffprobe_tool(self):
        """Check ffprobe and update its version label."""
        path = self.ffprobe_edit.text().strip() or "ffprobe"
        found, version = self._get_tool_version(path)
        self._set_version_label(self.ffprobe_version_label, found, version)

    def _check_mkvpropedit_tool(self):
        """Check mkvpropedit and update its version label."""
        path = self.mkvpropedit_edit.text().strip() or "mkvpropedit"
        found, version = self._get_tool_version(path)
        self._set_version_label(self.mkvpropedit_version_label, found, version)

    def _check_all_tools(self):
        """Check all tools and update version labels."""
        self._check_ffmpeg_tool()
        self._check_ffprobe_tool()
        self._check_mkvpropedit_tool()

    def _auto_detect_ffprobe(self, ffmpeg_path: str):
        """Try to find ffprobe next to the selected ffmpeg binary."""
        if not ffmpeg_path or ffmpeg_path == "ffmpeg":
            return
        ffmpeg_dir = os.path.dirname(ffmpeg_path)
        if not ffmpeg_dir:
            return
        ffprobe_candidate = os.path.join(ffmpeg_dir, "ffprobe")
        if os.path.isfile(ffprobe_candidate) and os.access(ffprobe_candidate, os.X_OK):
            self.ffprobe_edit.setText(ffprobe_candidate)
            self._check_ffprobe_tool()

    # -- Path change handlers --

    def _on_ffmpeg_path_changed(self):
        self._check_ffmpeg_tool()
        self._auto_detect_ffprobe(self.ffmpeg_edit.text().strip())

    def _on_ffprobe_path_changed(self):
        self._check_ffprobe_tool()

    def _on_mkvpropedit_path_changed(self):
        self._check_mkvpropedit_tool()

    # -- Browse buttons --

    def _browse_out(self):
        d = QFileDialog.getExistingDirectory(self, "Choose output root", self.out_edit.text())
        if d:
            self.out_edit.setText(d)

    def _browse_ffmpeg(self):
        start = os.path.dirname(self.ffmpeg_edit.text()) if self.ffmpeg_edit.text() not in ("", "ffmpeg") else "/usr/bin"
        f, _ = QFileDialog.getOpenFileName(
            self, "Locate ffmpeg", start, "All (*)"
        )
        if f:
            self.ffmpeg_edit.setText(f)
            self._check_ffmpeg_tool()
            self._auto_detect_ffprobe(f)

    def _browse_ffprobe(self):
        start = os.path.dirname(self.ffprobe_edit.text()) if self.ffprobe_edit.text() not in ("", "ffprobe") else "/usr/bin"
        f, _ = QFileDialog.getOpenFileName(
            self, "Locate ffprobe", start, "All (*)"
        )
        if f:
            self.ffprobe_edit.setText(f)
            self._check_ffprobe_tool()

    def _browse_mkvpropedit(self):
        start = os.path.dirname(self.mkvpropedit_edit.text()) if self.mkvpropedit_edit.text() not in ("", "mkvpropedit") else "/usr/bin"
        f, _ = QFileDialog.getOpenFileName(
            self, "Locate mkvpropedit", start, "All (*)"
        )
        if f:
            self.mkvpropedit_edit.setText(f)
            self._check_mkvpropedit_tool()

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
            "max_muxing_queue_size": self.chk_max_mux_queue.isChecked(),
            "avoid_negative_ts": self.chk_avoid_neg_ts.isChecked(),
            "chapter_naming": self.chapter_combo.currentData(),
            "extra_args": self.extra_args.text().strip(),
            # Native muxer settings
            "use_native_remux": self.native_remux_check.isChecked(),
            "apply_trim": self.native_apply_trim_check.isChecked(),
            "include_closed_captions": self.native_cc_check.isChecked(),
        }
