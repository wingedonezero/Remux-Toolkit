# remux_toolkit/tools/crc_tool/crc_tool_gui.py

import os
import re
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QProgressBar, QTextEdit, QFileDialog,
    QHeaderView, QFrame, QAbstractItemView, QMessageBox, QCheckBox,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QColor, QFont, QDragEnterEvent, QDropEvent

from . import crc_tool_core as core
from .crc_tool_config import DEFAULTS

# ─────────────────────────────────────────────────────────────────────────────
# DROP ZONE WIDGET
# ─────────────────────────────────────────────────────────────────────────────

class DropZone(QFrame):
    files_dropped = pyqtSignal(list)

    def __init__(self, label: str, accept_folders=True, accept_files=True, parent=None):
        super().__init__(parent)
        self.accept_folders = accept_folders
        self.accept_files = accept_files
        self.setAcceptDrops(True)
        self.setMinimumHeight(90)
        self.setObjectName("DropZone")

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_lbl = QLabel("⬇")
        self.icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_lbl.setStyleSheet("font-size: 24px; color: #4a9eff;")
        self.text_lbl = QLabel(label)
        self.text_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text_lbl.setStyleSheet("color: #8899aa; font-size: 12px;")
        layout.addWidget(self.icon_lbl)
        layout.addWidget(self.text_lbl)

        self._set_idle()

    def _set_idle(self):
        self.setStyleSheet("""
            #DropZone {
                border: 2px dashed #2a3a4a;
                border-radius: 8px;
                background: #0d1520;
            }
            #DropZone:hover {
                border-color: #3a5a7a;
                background: #111d2a;
            }
        """)

    def _set_hover(self):
        self.setStyleSheet("""
            #DropZone {
                border: 2px dashed #4a9eff;
                border-radius: 8px;
                background: #0d1f30;
            }
        """)

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._set_hover()
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self._set_idle()

    def dropEvent(self, e: QDropEvent):
        self._set_idle()
        paths = []
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if os.path.isfile(p) and self.accept_files:
                paths.append(p)
            elif os.path.isdir(p) and self.accept_folders:
                paths.append(p)
        if paths:
            self.files_dropped.emit(paths)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            if self.accept_folders and self.accept_files:
                paths, _ = QFileDialog.getOpenFileNames(self, "Select Files")
            elif self.accept_folders:
                path = QFileDialog.getExistingDirectory(self, "Select Folder")
                paths = [path] if path else []
            else:
                paths, _ = QFileDialog.getOpenFileNames(self, "Select Files")
            if paths:
                self.files_dropped.emit(paths)


# ─────────────────────────────────────────────────────────────────────────────
# TAB 1 — CRC APPEND
# ─────────────────────────────────────────────────────────────────────────────

class CRCTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = None
        self._rows = []   # (row_idx, filepath)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        # Drop zone
        self.drop = DropZone(
            "Drop files here to compute CRC32  •  or click to browse",
            accept_folders=False, accept_files=True
        )
        self.drop.files_dropped.connect(self._add_files)
        layout.addWidget(self.drop)

        # Table
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Original Name", "New Name", "CRC32", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 110)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)

        # Options + buttons row
        opts = QHBoxLayout()

        self.rename_chk = QCheckBox("Rename files on disk")
        self.rename_chk.setChecked(True)
        self.rename_chk.setStyleSheet("color: #aabbcc;")

        self.strip_chk = QCheckBox("Strip existing CRC first")
        self.strip_chk.setChecked(True)
        self.strip_chk.setStyleSheet("color: #aabbcc;")

        opts.addWidget(self.rename_chk)
        opts.addWidget(self.strip_chk)
        opts.addStretch()

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setObjectName("secondary")
        self.clear_btn.clicked.connect(self._clear)

        self.run_btn = QPushButton("▶  Compute & Rename")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self._run)

        opts.addWidget(self.clear_btn)
        opts.addWidget(self.run_btn)
        layout.addLayout(opts)

        # Progress
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(5)
        self.progress.setStyleSheet("""
            QProgressBar { background: #1a2535; border: none; border-radius: 2px; }
            QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #1a6fff, stop:1 #00d4ff); border-radius: 2px; }
        """)
        self.progress.hide()
        layout.addWidget(self.progress)

    def _add_files(self, paths: list):
        for p in paths:
            if os.path.isfile(p):
                row = self.table.rowCount()
                self.table.insertRow(row)
                name = os.path.basename(p)
                self.table.setItem(row, 0, QTableWidgetItem(name))
                self.table.setItem(row, 1, QTableWidgetItem("—"))
                self.table.setItem(row, 2, QTableWidgetItem("—"))
                status_item = QTableWidgetItem("Pending")
                status_item.setForeground(QColor("#6688aa"))
                self.table.setItem(row, 3, status_item)
                self._rows.append((row, p))

    def _clear(self):
        self.table.setRowCount(0)
        self._rows.clear()
        self.progress.hide()

    def _run(self):
        if not self._rows:
            return
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self.run_btn.setText("▶  Compute & Rename")
            return

        self.progress.show()
        self.progress.setValue(0)
        self.run_btn.setText("■  Cancel")

        # Prepare rows — optionally strip old CRC from filename before computing
        work_rows = []
        for row, fp in self._rows:
            status = self.table.item(row, 3)
            if status and status.text() in ("✓ Done", "✗ Error"):
                continue
            work_rows.append((row, fp))

        self._worker = core.CRCWorker(work_rows, self.rename_chk.isChecked())
        self._worker.row_progress.connect(self._on_row_progress)
        self._worker.row_done.connect(self._on_row_done)
        self._worker.row_error.connect(self._on_row_error)
        self._worker.all_done.connect(self._on_all_done)
        self._worker.start()

    def _on_row_progress(self, row, pct):
        item = self.table.item(row, 3)
        if item:
            item.setText(f"Hashing {pct}%")
            item.setForeground(QColor("#4a9eff"))
        done = sum(1 for r, _ in self._rows if self.table.item(r, 3) and
                   self.table.item(r, 3).text() == "✓ Done")
        total = len(self._rows)
        if total:
            self.progress.setValue(int((done + pct / 100) / total * 100))

    def _on_row_done(self, row, old, new):
        self.table.item(row, 1).setText(new)
        crc_m = re.search(r'\(([0-9A-Fa-f]{8})\)', new)
        if crc_m:
            self.table.item(row, 2).setText(crc_m.group(1))
        item = QTableWidgetItem("✓ Done")
        item.setForeground(QColor("#00cc88"))
        self.table.setItem(row, 3, item)

    def _on_row_error(self, row, err):
        item = QTableWidgetItem("✗ Error")
        item.setForeground(QColor("#ff4466"))
        self.table.setItem(row, 3, item)
        self.table.item(row, 1).setText(err)

    def _on_all_done(self):
        self.run_btn.setText("▶  Compute & Rename")
        self.progress.setValue(100)
        QTimer.singleShot(2000, self.progress.hide)


# ─────────────────────────────────────────────────────────────────────────────
# TAB 2 — MD5 MANIFEST
# ─────────────────────────────────────────────────────────────────────────────

class MD5Tab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = None
        self._folder = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        # Drop zone
        self.drop = DropZone(
            "Drop a FOLDER here to generate recursive MD5 manifest  •  or click to browse",
            accept_folders=True, accept_files=False
        )
        self.drop.files_dropped.connect(self._set_folder)
        layout.addWidget(self.drop)

        # Folder display
        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Folder:"))
        self.folder_lbl = QLabel("None selected")
        self.folder_lbl.setStyleSheet("color: #4a9eff; font-family: monospace;")
        self.folder_lbl.setWordWrap(True)
        folder_row.addWidget(self.folder_lbl, 1)
        layout.addLayout(folder_row)

        # Log
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Courier New", 10))
        self.log.setStyleSheet("""
            QTextEdit {
                background: #080f18;
                color: #7aa8cc;
                border: 1px solid #1a2535;
                border-radius: 6px;
            }
        """)
        layout.addWidget(self.log, 1)

        # Progress
        self.file_lbl = QLabel("")
        self.file_lbl.setStyleSheet("color: #6688aa; font-size: 11px; font-family: monospace;")
        layout.addWidget(self.file_lbl)

        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFixedHeight(18)
        self.progress.setStyleSheet("""
            QProgressBar { background: #1a2535; border: none; border-radius: 3px;
                           color: #aabbcc; font-size: 10px; text-align: center; }
            QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #1a6fff, stop:1 #00d4ff); border-radius: 3px; }
        """)
        self.progress.hide()
        layout.addWidget(self.progress)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setObjectName("secondary")
        self.clear_btn.clicked.connect(self._clear)
        self.run_btn = QPushButton("▶  Generate MD5 Manifest")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self._run)
        btn_row.addWidget(self.clear_btn)
        btn_row.addWidget(self.run_btn)
        layout.addLayout(btn_row)

    def _set_folder(self, paths: list):
        dirs = [p for p in paths if os.path.isdir(p)]
        if dirs:
            self._folder = dirs[0]
            self.folder_lbl.setText(self._folder)
            self.log.clear()
            self._log(f"📁 Selected: {self._folder}", "#aabbcc")
            # count files
            count = sum(1 for _ in Path(self._folder).rglob('*') if _.is_file())
            self._log(f"   Found {count} files to hash", "#6688aa")

    def _log(self, msg: str, color="#7aa8cc"):
        self.log.append(f'<span style="color:{color};">{msg}</span>')

    def _clear(self):
        self._folder = None
        self.folder_lbl.setText("None selected")
        self.log.clear()
        self.progress.hide()
        self.file_lbl.setText("")

    def _run(self):
        if not self._folder:
            QMessageBox.warning(self, "No Folder", "Please drop or select a folder first.")
            return
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self.run_btn.setText("▶  Generate MD5 Manifest")
            return

        self.progress.show()
        self.progress.setValue(0)
        self.run_btn.setText("■  Cancel")
        self.log.clear()
        self._log(f"🔄 Hashing all files in: {self._folder}", "#4a9eff")

        self._worker = core.MD5Worker(self._folder)
        self._worker.file_progress.connect(self._on_file_progress)
        self._worker.all_done.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_file_progress(self, idx, pct, fname):
        self.file_lbl.setText(f"[{idx+1}] {fname}  {pct}%")
        self.progress.setValue(pct)

    def _on_done(self, manifest_path, total_files, total_bytes):
        self.run_btn.setText("▶  Generate MD5 Manifest")
        self.progress.setValue(100)
        self._log(f"", "")
        self._log(f"✅ Manifest created: {manifest_path}", "#00cc88")
        self._log(f"   Files: {total_files}  |  Total: {core.format_size(total_bytes)}", "#6688aa")
        self.file_lbl.setText("")
        QTimer.singleShot(3000, self.progress.hide)

    def _on_error(self, err):
        self.run_btn.setText("▶  Generate MD5 Manifest")
        self._log(f"❌ Error: {err}", "#ff4466")
        self.progress.hide()


# ─────────────────────────────────────────────────────────────────────────────
# TAB 3 — VERIFY
# ─────────────────────────────────────────────────────────────────────────────

STATUS_COLORS = {
    "Pending":  "#6688aa",
    "Hashing":  "#4a9eff",
    "✓ Pass":   "#00cc88",
    "✗ Fail":   "#ff4466",
    "⚠ Missing":"#ffaa00",
    "⚠ No CRC": "#ffaa00",
    "✗ Error":  "#ff4466",
}


class VerifyTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = None
        self._tasks = []   # mixed CRC files + MD5 manifests
        self._pass_count = 0
        self._fail_count = 0
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        # Drop zone
        self.drop = DropZone(
            "Drop files with CRC in name  •  Drop .md5 manifest files  •  Drop folders\nor click to browse",
            accept_folders=True, accept_files=True
        )
        self.drop.files_dropped.connect(self._add_items)
        layout.addWidget(self.drop)

        # Summary row
        sum_row = QHBoxLayout()
        self.total_lbl  = self._badge("0 items", "#2a3a4a", "#aabbcc")
        self.pass_lbl   = self._badge("0 pass",  "#0a2a1a", "#00cc88")
        self.fail_lbl   = self._badge("0 fail",  "#2a0a1a", "#ff4466")
        sum_row.addWidget(self.total_lbl)
        sum_row.addWidget(self.pass_lbl)
        sum_row.addWidget(self.fail_lbl)
        sum_row.addStretch()
        layout.addLayout(sum_row)

        # Table
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["File / Path", "Type", "Detail", "Result"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 60)
        self.table.setColumnWidth(3, 90)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table, 1)

        # Progress
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFixedHeight(18)
        self.progress.setFormat("%v / %m files")
        self.progress.setStyleSheet("""
            QProgressBar { background: #1a2535; border: none; border-radius: 3px;
                           color: #aabbcc; font-size: 10px; text-align: center; }
            QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #1a6fff, stop:1 #00d4ff); border-radius: 3px; }
        """)
        self.progress.hide()
        layout.addWidget(self.progress)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setObjectName("secondary")
        self.clear_btn.clicked.connect(self._clear)
        self.run_btn = QPushButton("▶  Verify All")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self._run)
        btn_row.addWidget(self.clear_btn)
        btn_row.addWidget(self.run_btn)
        layout.addLayout(btn_row)

    def _badge(self, text, bg, fg):
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setFixedHeight(26)
        lbl.setMinimumWidth(80)
        lbl.setStyleSheet(f"""
            QLabel {{
                background: {bg}; color: {fg};
                border: 1px solid {fg}44;
                border-radius: 4px;
                font-size: 12px; font-weight: bold;
                padding: 0 10px;
            }}
        """)
        return lbl

    def _add_items(self, paths: list):
        for p in paths:
            if os.path.isdir(p):
                # look for .md5 manifest inside
                found = False
                for root, dirs, files in os.walk(p):
                    for f in files:
                        if f.lower().endswith('.md5'):
                            fp = os.path.join(root, f)
                            self._add_manifest(fp, root)
                            found = True
                    # Check files with CRC
                    for f in files:
                        if core.CRC32_PATTERN.search(f):
                            fp = os.path.join(root, f)
                            self._add_crc_file(fp)
                            found = True
            elif p.lower().endswith('.md5'):
                self._add_manifest(p, os.path.dirname(p))
            elif os.path.isfile(p):
                if core.CRC32_PATTERN.search(os.path.basename(p)):
                    self._add_crc_file(p)
                else:
                    # add as unknown / no crc
                    self._add_crc_file(p)
        self._update_counts()

    def _add_crc_file(self, fp: str):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(fp))
        type_item = QTableWidgetItem("CRC")
        type_item.setForeground(QColor("#4a9eff"))
        self.table.setItem(row, 1, type_item)
        self.table.setItem(row, 2, QTableWidgetItem("—"))
        s = QTableWidgetItem("Pending")
        s.setForeground(QColor("#6688aa"))
        self.table.setItem(row, 3, s)
        self._tasks.append({'type': 'crc', 'path': fp, 'row': row})

    def _add_manifest(self, manifest: str, base_dir: str):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(manifest))
        type_item = QTableWidgetItem("MD5")
        type_item.setForeground(QColor("#ffaa44"))
        self.table.setItem(row, 1, type_item)
        self.table.setItem(row, 2, QTableWidgetItem(f"base: {base_dir}"))
        s = QTableWidgetItem("Pending")
        s.setForeground(QColor("#6688aa"))
        self.table.setItem(row, 3, s)
        self._tasks.append({'type': 'md5', 'manifest': manifest,
                            'base_dir': base_dir, 'row': row})

    def _update_counts(self):
        self.total_lbl.setText(f"{len(self._tasks)} items")

    def _clear(self):
        self.table.setRowCount(0)
        self._tasks.clear()
        self._update_counts()
        self.progress.hide()
        self.pass_lbl.setText("0 pass")
        self.fail_lbl.setText("0 fail")

    def _run(self):
        if not self._tasks:
            return
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self.run_btn.setText("▶  Verify All")
            return

        # Reset results in table
        for task in self._tasks:
            row = task['row']
            s = QTableWidgetItem("Pending")
            s.setForeground(QColor("#6688aa"))
            self.table.setItem(row, 3, s)

        self.progress.show()
        self.progress.setValue(0)
        self.run_btn.setText("■  Cancel")
        self._pass_count = 0
        self._fail_count = 0

        # Build worker tasks (without 'row' key - worker doesn't need it)
        worker_tasks = []
        for t in self._tasks:
            if t['type'] == 'crc':
                worker_tasks.append({'type': 'crc', 'path': t['path']})
            else:
                worker_tasks.append({'type': 'md5', 'manifest': t['manifest'],
                                     'base_dir': t['base_dir']})

        self._worker = core.VerifyWorker(worker_tasks)
        self._worker.item_result.connect(self._on_item_result)
        self._worker.progress.connect(self._on_progress)
        self._worker.all_done.connect(self._on_all_done)
        self._worker.start()

    def _on_item_result(self, filepath: str, ok: bool, detail: str):
        # Find matching task row
        for task in self._tasks:
            fp = task.get('path', task.get('manifest', ''))
            # For md5, filepath is the actual file being checked
            if task['type'] == 'md5':
                # update the manifest row result cumulatively
                row = task['row']
                if ok:
                    self._pass_count += 1
                else:
                    self._fail_count += 1
                # Update detail with last checked file
                name = os.path.basename(filepath)
                self.table.item(row, 2).setText(f"Last: {name} — {detail}")
                status = "✓ Pass" if self._pass_count > 0 and self._fail_count == 0 else "✗ Fail" if self._fail_count > 0 else "Pending"
                s = QTableWidgetItem(status)
                s.setForeground(QColor(STATUS_COLORS.get(status, "#aabbcc")))
                self.table.setItem(row, 3, s)
                break
            elif task['type'] == 'crc' and task['path'] == filepath:
                row = task['row']
                if ok:
                    self._pass_count += 1
                    status = "✓ Pass"
                else:
                    self._fail_count += 1
                    status = "✗ Fail" if "No CRC" not in detail else "⚠ No CRC"
                self.table.item(row, 2).setText(detail)
                s = QTableWidgetItem(status)
                s.setForeground(QColor(STATUS_COLORS.get(status, "#aabbcc")))
                self.table.setItem(row, 3, s)
                break
        self.pass_lbl.setText(f"{self._pass_count} pass")
        self.fail_lbl.setText(f"{self._fail_count} fail")

    def _on_progress(self, done, total):
        self.progress.setMaximum(total)
        self.progress.setValue(done)

    def _on_all_done(self, passed, failed):
        self.run_btn.setText("▶  Verify All")
        self._pass_count = passed
        self._fail_count = failed
        self.pass_lbl.setText(f"{passed} pass")
        self.fail_lbl.setText(f"{failed} fail")
        QTimer.singleShot(5000, self.progress.hide)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN WIDGET
# ─────────────────────────────────────────────────────────────────────────────

# Scoped to this tool's widget tree only (set on CRCToolWidget), so the dark
# theme does not leak into the rest of the toolkit.
STYLESHEET = """
QWidget {
    background-color: #0a1420;
    color: #c8d8e8;
    font-family: "Segoe UI", "SF Pro Display", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
}
QTabWidget::pane {
    border: 1px solid #1a2535;
    border-radius: 6px;
    background: #0d1a28;
    top: -1px;
}
QTabBar::tab {
    background: #0a1420;
    color: #6688aa;
    padding: 8px 22px;
    border: 1px solid transparent;
    border-bottom: none;
    border-radius: 5px 5px 0 0;
    margin-right: 2px;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.5px;
}
QTabBar::tab:selected {
    background: #0d1a28;
    color: #4a9eff;
    border-color: #1a2535;
    border-bottom: 1px solid #0d1a28;
}
QTabBar::tab:hover:!selected {
    color: #aabbcc;
    background: #0d1520;
}
QTableWidget {
    background: #080f18;
    alternate-background-color: #0a1420;
    border: 1px solid #1a2535;
    border-radius: 6px;
    gridline-color: #111d2a;
    selection-background-color: #1a3050;
    selection-color: #e0eeff;
}
QHeaderView::section {
    background: #0f1e30;
    color: #6688aa;
    padding: 6px;
    border: none;
    border-right: 1px solid #1a2535;
    border-bottom: 1px solid #1a2535;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.8px;
    text-transform: uppercase;
}
QTableWidget::item {
    padding: 4px 8px;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 11px;
}
QPushButton {
    background: #1a2535;
    color: #aabbcc;
    border: 1px solid #2a3a50;
    border-radius: 5px;
    padding: 7px 18px;
    font-size: 12px;
    font-weight: 600;
}
QPushButton:hover {
    background: #1e2e44;
    color: #d0e8ff;
    border-color: #3a5a7a;
}
QPushButton#primary {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #1a4fff, stop:1 #0088cc);
    color: #ffffff;
    border: none;
    padding: 7px 22px;
}
QPushButton#primary:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #2a5fff, stop:1 #00aaff);
}
QPushButton#primary:pressed {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #1030cc, stop:1 #0066aa);
}
QPushButton#secondary {
    background: transparent;
    color: #6688aa;
    border: 1px solid #1a2535;
}
QPushButton#secondary:hover {
    color: #aabbcc;
    border-color: #2a3a50;
}
QScrollBar:vertical {
    background: #080f18; width: 8px; margin: 0;
}
QScrollBar::handle:vertical {
    background: #2a3a50; border-radius: 4px; min-height: 20px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal {
    background: #080f18; height: 8px; margin: 0;
}
QScrollBar::handle:horizontal {
    background: #2a3a50; border-radius: 4px; min-width: 20px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QCheckBox { color: #aabbcc; spacing: 6px; }
QCheckBox::indicator {
    width: 14px; height: 14px;
    background: #1a2535; border: 1px solid #2a3a50; border-radius: 3px;
}
QCheckBox::indicator:checked {
    background: #1a6fff; border-color: #1a6fff;
    image: none;
}
QLabel { color: #c8d8e8; }
"""


class CRCToolWidget(QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'crc_tool'

        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 4)
        layout.setSpacing(6)

        # Header
        header = QLabel("CRC · MD5  INTEGRITY")
        header.setStyleSheet("""
            font-size: 15px; font-weight: 700; color: #4a9eff;
            letter-spacing: 3px; padding: 4px 0;
        """)
        layout.addWidget(header)

        # Tabs
        self.tabs = QTabWidget()
        self.tab_crc    = CRCTab()
        self.tab_md5    = MD5Tab()
        self.tab_verify = VerifyTab()
        self.tabs.addTab(self.tab_crc,    "① CRC Append")
        self.tabs.addTab(self.tab_md5,    "② MD5 Manifest")
        self.tabs.addTab(self.tab_verify, "③ Verify")
        layout.addWidget(self.tabs, 1)

        # Bottom hint
        hint = QLabel("Drag & drop files or folders onto each tab  •  CRC format: filename (ABCD1234).ext")
        hint.setStyleSheet("color: #2a3a50; font-size: 10px; padding: 2px 4px;")
        layout.addWidget(hint)

        self._load_settings()

    def _load_settings(self):
        settings = self.app_manager.load_config(self.tool_name, DEFAULTS)
        self.tab_crc.rename_chk.setChecked(settings.get("rename_files", DEFAULTS["rename_files"]))
        self.tab_crc.strip_chk.setChecked(settings.get("strip_existing_crc", DEFAULTS["strip_existing_crc"]))

    def save_settings(self):
        self.app_manager.save_config(self.tool_name, {
            "rename_files": self.tab_crc.rename_chk.isChecked(),
            "strip_existing_crc": self.tab_crc.strip_chk.isChecked(),
        })

    def shutdown(self):
        for tab in (self.tab_crc, self.tab_md5, self.tab_verify):
            worker = tab._worker
            if worker and worker.isRunning():
                worker.cancel()
                if not worker.wait(3000):
                    worker.terminate()
                    worker.wait()
            tab._worker = None
