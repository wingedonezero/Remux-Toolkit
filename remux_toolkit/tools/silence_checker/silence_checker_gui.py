# remux_toolkit/tools/silence_checker/silence_checker_gui.py

import os
import re

from PyQt6 import QtCore, QtGui, QtWidgets

from . import silence_checker_core as core
# We only import DEFAULTS now, to create the first config file
from .silence_checker_config import DEFAULTS


class SilenceCheckerWidget(QtWidgets.QWidget):
    # The widget now requires the app_manager to be passed in
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'silence_checker'

        self.setAcceptDrops(True)

        # --- UI Setup (This part is unchanged) ---
        v_layout = QtWidgets.QVBoxLayout(self)
        path_layout = QtWidgets.QHBoxLayout()
        self.path_edit = QtWidgets.QLineEdit()
        self.path_edit.setPlaceholderText("Drop files/folders here or paste paths (separate with ';' or newlines)…")
        self.path_edit.setAcceptDrops(True)
        browse_btn = QtWidgets.QPushButton("Browse…")
        clear_btn = QtWidgets.QPushButton("Clear")
        path_layout.addWidget(QtWidgets.QLabel("Inputs:"))
        path_layout.addWidget(self.path_edit, 1)
        path_layout.addWidget(browse_btn)
        path_layout.addWidget(clear_btn)
        v_layout.addLayout(path_layout)
        ctrl_layout = QtWidgets.QHBoxLayout()
        self.neg_delay_ms = QtWidgets.QSpinBox(); self.neg_delay_ms.setRange(0, 120000); self.neg_delay_ms.setSuffix(" ms (optional compare delay)")
        self.window_ms = QtWidgets.QSpinBox(); self.window_ms.setRange(50, 60000); self.window_ms.setSuffix(" ms scan window")
        self.noise_db = QtWidgets.QSpinBox(); self.noise_db.setRange(-120, 0); self.noise_db.setSuffix(" dB threshold")
        self.min_sil_ms = QtWidgets.QSpinBox(); self.min_sil_ms.setRange(0, 10000); self.min_sil_ms.setSuffix(" ms min-gap")
        self.tolerance_ms = QtWidgets.QSpinBox(); self.tolerance_ms.setRange(0, 1000); self.tolerance_ms.setSuffix(" ms tolerance")
        for w in (self.neg_delay_ms, self.window_ms, self.noise_db, self.min_sil_ms, self.tolerance_ms):
            ctrl_layout.addWidget(w)
        v_layout.addLayout(ctrl_layout)
        btn_layout = QtWidgets.QHBoxLayout()
        self.probe_btn = QtWidgets.QPushButton("Probe Tracks (all files)")
        self.scan_btn = QtWidgets.QPushButton("Scan Leading Silence"); self.scan_btn.setEnabled(False)
        btn_layout.addWidget(self.probe_btn); btn_layout.addWidget(self.scan_btn)
        v_layout.addLayout(btn_layout)
        self.table = QtWidgets.QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(["Select", "File", "StreamIdx", "Codec", "Lang", "Title", "Channels", "Rate", "LeadingSilence(ms)", "Verdict"])
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows); self.table.setColumnWidth(0, 80)
        v_layout.addWidget(self.table, 1)
        self.log = QtWidgets.QPlainTextEdit(); self.log.setReadOnly(True); self.log.setPlaceholderText("Logs will appear here…")
        v_layout.addWidget(self.log, 1)
        # --- End of UI Setup ---

        self._target_ms = 0; self._tol_ms = 0
        self.thread = QtCore.QThread(self); self.worker = core.Worker(); self.worker.moveToThread(self.thread); self.thread.start()

        # Connect signals
        browse_btn.clicked.connect(self.browse); clear_btn.clicked.connect(self.clear_paths)
        self.probe_btn.clicked.connect(self.on_probe); self.scan_btn.clicked.connect(self.on_scan)
        self.worker.resultReady.connect(self._on_worker_result); self.worker.error.connect(lambda msg: self.append_log(f"Error: {msg}"))

        # Load settings from the manager
        self._load_settings()

    def _load_settings(self):
        """Loads settings from JSON file and applies them to the UI."""
        print(f"Loading settings for {self.tool_name}...")
        settings = self.app_manager.load_config(self.tool_name, DEFAULTS)
        self.neg_delay_ms.setValue(settings.get("neg_delay_ms", DEFAULTS["neg_delay_ms"]))
        self.window_ms.setValue(settings.get("window_ms", DEFAULTS["window_ms"]))
        self.noise_db.setValue(settings.get("noise_db", DEFAULTS["noise_db"]))
        self.min_sil_ms.setValue(settings.get("min_sil_ms", DEFAULTS["min_sil_ms"]))
        self.tolerance_ms.setValue(settings.get("tolerance_ms", DEFAULTS["tolerance_ms"]))

    def _gather_current_settings(self) -> dict:
        """Gathers current values from UI widgets into a dictionary."""
        return {
            "neg_delay_ms": self.neg_delay_ms.value(),
            "window_ms": self.window_ms.value(),
            "noise_db": self.noise_db.value(),
            "min_sil_ms": self.min_sil_ms.value(),
            "tolerance_ms": self.tolerance_ms.value(),
        }

    def save_settings(self):
        """Public method to save settings, called by the main window."""
        current_settings = self._gather_current_settings()
        self.app_manager.save_config(self.tool_name, current_settings)

    # --- All other methods (dragEnterEvent, on_probe, etc.) are unchanged ---

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        if event.mimeData().hasUrls(): event.acceptProposedAction()
        else: event.ignore()
    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        urls = event.mimeData().urls();
        if not urls: return
        existing = [p.strip() for p in re.split(r"[;\n]", self.path_edit.text()) if p.strip()]; added = 0
        for u in urls:
            p = u.toLocalFile();
            if not p: continue
            if p not in existing: existing.append(p); added += 1
        if added: self.path_edit.setText("; ".join(existing))
        event.acceptProposedAction()
    def browse(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Add media files", os.path.expanduser("~"), "Media (*.mkv *.mka *.mp4 *.m4a *.ts *.m2ts *.flac *.wav);;All (*)");
        if not files: return
        existing = [p.strip() for p in re.split(r"[;\n]", self.path_edit.text()) if p.strip()]
        for f in files:
            if f not in existing: existing.append(f)
        self.path_edit.setText("; ".join(existing))
    def clear_paths(self): self.path_edit.clear(); self.table.setRowCount(0); self.scan_btn.setEnabled(False); self.append_log("Cleared inputs and table.")
    def append_log(self, text: str): self.log.appendPlainText(text); self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
    def on_probe(self):
        typed = self.path_edit.text().strip(); files: list[str] = []
        if typed:
            parts: list[str] = [];
            for chunk in re.split(r"[;\n]", typed):
                p = chunk.strip();
                if not p: continue
                if os.path.isdir(p):
                    for root, _, names in os.walk(p):
                        for n in names:
                            if n.lower().endswith((".mkv", ".mka", ".mp4", ".m4a", ".ts", ".m2ts", ".wav", ".flac")): parts.append(os.path.join(root, n))
                elif os.path.isfile(p): parts.append(p)
            seen = set();
            for p in parts:
                if p not in seen: files.append(p); seen.add(p)
        if not files: QtWidgets.QMessageBox.warning(self, "No files", "Add files or folders, then click Probe."); return
        self.table.setRowCount(0); total = 0
        for fpath in files:
            try: streams = core.ffprobe_audio_streams(fpath)
            except Exception as e: self.append_log(f"Probe failed for {fpath}: {e}"); continue
            for st in streams:
                row = self.table.rowCount(); self.table.insertRow(row); cb = QtWidgets.QCheckBox("scan"); cb.setChecked(True); self.table.setCellWidget(row, 0, cb)
                def add(col: int, val, store_path: bool = False):
                    it = QtWidgets.QTableWidgetItem("" if val is None else str(val)); it.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable)
                    if store_path: it.setData(QtCore.Qt.ItemDataRole.UserRole, fpath)
                    self.table.setItem(row, col, it)
                add(1, os.path.basename(fpath), store_path=True); add(2, st.index); add(3, st.codec_name); add(4, st.language); add(5, st.title); add(6, st.channels); add(7, st.sample_rate); add(8, "-"); add(9, "n/a"); total += 1
        self.scan_btn.setEnabled(self.table.rowCount() > 0); self.append_log(f"Probed {total} audio streams across {len(files)} file(s).")
    @QtCore.pyqtSlot()
    def on_scan(self):
        target_ms = self.neg_delay_ms.value(); window_ms = max(target_ms + 200, self.window_ms.value()); noise_db = self.noise_db.value(); min_sil = self.min_sil_ms.value(); tol_ms = self.tolerance_ms.value()
        rows_to_scan = [r for r in range(self.table.rowCount()) if self.table.cellWidget(r, 0).isChecked()]
        if not rows_to_scan: QtWidgets.QMessageBox.information(self, "Nothing selected", "Check 'scan' for at least one audio stream."); return
        self._target_ms = target_ms; self._tol_ms = tol_ms
        for r in rows_to_scan:
            fitem = self.table.item(r, 1); fpath = fitem.data(QtCore.Qt.ItemDataRole.UserRole) if fitem else None
            if not fpath: continue
            try: stream_index = int(self.table.item(r, 2).text())
            except (TypeError, ValueError): continue
            QtCore.QMetaObject.invokeMethod(self.worker, "run", QtCore.Qt.ConnectionType.QueuedConnection, QtCore.Q_ARG(int, r), QtCore.Q_ARG(str, fpath), QtCore.Q_ARG(int, stream_index), QtCore.Q_ARG(int, window_ms), QtCore.Q_ARG(int, noise_db), QtCore.Q_ARG(int, min_sil))
    def _on_worker_result(self, row: int, res: core.SilenceResult): self._apply_result(row, res, self._target_ms, self._tol_ms)
    def _apply_result(self, row: int, res: core.SilenceResult, target_ms: int, tol_ms: int):
        leading = int(round(res.leading_silence_ms)); self.table.item(row, 8).setText(str(leading)); verdict = "n/a" if target_ms <= 0 else ("SAFE" if leading + tol_ms >= target_ms else "RISK"); self.table.item(row, 9).setText(verdict)
        snippet = "\n".join(res.details.splitlines()[-20:]); self.append_log(f"Row {row}: leading={leading} ms, target={target_ms} ms -> {verdict}\n{snippet}\n")
    def shutdown(self): self.thread.quit(); self.thread.wait(2000)
