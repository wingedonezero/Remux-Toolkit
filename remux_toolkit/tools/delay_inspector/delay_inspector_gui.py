# remux_toolkit/tools/delay_inspector/delay_inspector_gui.py

import os
import sys
from typing import Dict, List

from PyQt6 import QtWidgets, QtCore, QtGui
from . import delay_inspector_core as core

# ------------------------------ UI ------------------------------ #

class FileTable(QtWidgets.QTableWidget):
    filesDropped = QtCore.pyqtSignal(list)

    def __init__(self):
        super().__init__(0, 3)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.DropOnly)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setHorizontalHeaderLabels(["File", "Status", "Video Start (s)"])
        self.horizontalHeader().setStretchLastSection(True)

    def _has_uris(self, e) -> bool:
        md = e.mimeData()
        return md and md.hasUrls()

    def dragEnterEvent(self, e):
        if self._has_uris(e): e.acceptProposedAction()
        else: e.ignore()

    def dragMoveEvent(self, e):
        if self._has_uris(e): e.acceptProposedAction()
        else: e.ignore()

    def dropEvent(self, e):
        if not self._has_uris(e):
            e.ignore(); return
        urls = e.mimeData().urls()
        raw = [u.toLocalFile() for u in urls if u.isLocalFile()]
        paths = core.collect_video_paths(raw)
        if paths:
            self.filesDropped.emit(paths)
            e.acceptProposedAction()
        else:
            e.ignore()

class DelayInspectorWidget(QtWidgets.QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.setAcceptDrops(True)

        if core.which("ffprobe") is None:
            QtWidgets.QMessageBox.critical(self, "Missing ffprobe", "ffprobe not found in PATH. Install FFmpeg (ffprobe).")

        # Layout
        main_layout = QtWidgets.QVBoxLayout(self)
        splitter = QtWidgets.QSplitter()
        left = QtWidgets.QWidget(); left_layout = QtWidgets.QVBoxLayout(left)

        self.table = FileTable()
        self.table.filesDropped.connect(self.enqueue_files)
        self.table.itemSelectionChanged.connect(self.update_detail_from_selection)

        btn_row = QtWidgets.QWidget(); hl = QtWidgets.QHBoxLayout(btn_row); hl.setContentsMargins(0,0,0,0)
        b_add = QtWidgets.QPushButton("Add Files"); b_add.clicked.connect(self.add_files); hl.addWidget(b_add)
        b_sel = QtWidgets.QPushButton("Analyze Selected"); b_sel.clicked.connect(self.analyze_selected); hl.addWidget(b_sel)
        b_all = QtWidgets.QPushButton("Analyze All"); b_all.clicked.connect(self.analyze_all); hl.addWidget(b_all)
        b_clear = QtWidgets.QPushButton("Clear"); b_clear.clicked.connect(self.clear_all); hl.addWidget(b_clear)
        b_export = QtWidgets.QPushButton("Export Selected"); b_export.clicked.connect(self.export_selected); hl.addWidget(b_export)
        hl.addStretch(1)

        left_layout.addWidget(self.table); left_layout.addWidget(btn_row)

        self.detail = QtWidgets.QTextEdit(); self.detail.setReadOnly(True)
        self.detail.setLineWrapMode(QtWidgets.QTextEdit.LineWrapMode.NoWrap)
        self.detail.setText(
            "Drag & drop files or folders anywhere, or click Add Files.\n"
            "Analyze to compute audio/subtitle delays relative to the first video stream.\n"
            "Positive delay → track AFTER video (apply +ms). Negative → BEFORE (apply -ms)."
        )

        splitter.addWidget(left); splitter.addWidget(self.detail)
        splitter.setStretchFactor(0,2); splitter.setStretchFactor(1,3)
        main_layout.addWidget(splitter)

        self.results: Dict[str, core.FileResult] = {}
        self.threadpool = QtCore.QThreadPool.globalInstance()

    # Window-level D&D (forward to table)
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls(): e.acceptProposedAction()
        else: e.ignore()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls(): e.acceptProposedAction()
        else: e.ignore()

    def dropEvent(self, e):
        if not e.mimeData().hasUrls():
            e.ignore(); return
        raw = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        paths = core.collect_video_paths(raw)
        if paths:
            self.enqueue_files(paths)
            e.acceptProposedAction()
        else:
            e.ignore()

    def enqueue_files(self, paths: List[str]):
        for p in paths:
            if not os.path.isfile(p):
                continue
            if self._row_of(p) != -1:
                continue
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QtWidgets.QTableWidgetItem(p))
            self.table.setItem(r, 1, QtWidgets.QTableWidgetItem("Pending"))
            self.table.setItem(r, 2, QtWidgets.QTableWidgetItem("—"))

    def add_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Add video files", "",
            "Video files (*.mkv *.mp4 *.m4v *.m2ts *.ts *.vob *.mpg *.mpeg *.avi *.mov *.wmv *.m2v);;All files (*.*)"
        )
        if not files: return
        self.enqueue_files(core.collect_video_paths(files))

    def get_selected_files(self) -> List[str]:
        rows = self.table.selectionModel().selectedRows()
        return [self.table.item(r.row(), 0).text() for r in rows]

    def analyze_selected(self):
        files = self.get_selected_files()
        if not files:
            files = [self.table.item(r, 0).text() for r in range(self.table.rowCount())]
        self._run_analysis(files)

    def analyze_all(self):
        files = [self.table.item(r, 0).text() for r in range(self.table.rowCount())]
        self._run_analysis(files)

    def clear_all(self):
        self.table.setRowCount(0)
        self.results.clear()
        self.detail.clear()

    def export_selected(self):
        files = self.get_selected_files()
        if not files:
            QtWidgets.QMessageBox.information(self, "Export", "Select a row first."); return
        f = files[0]
        res = self.results.get(f)
        if not res:
            QtWidgets.QMessageBox.information(self, "Export", "Analyze the file first."); return
        txt = format_result_text(res)
        out, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save result as", os.path.splitext(f)[0] + "_delays.txt", "Text (*.txt)"
        )
        if not out: return
        try:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(txt)
            QtWidgets.QMessageBox.information(self, "Export", f"Saved:\n{out}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Export failed", str(e))

    def update_detail_from_selection(self):
        files = self.get_selected_files()
        if not files: return
        res = self.results.get(files[0])
        if res:
            self.detail.setText(format_result_text(res))

    def _run_analysis(self, files: List[str]):
        if not files: return
        for r in range(self.table.rowCount()):
            p = self.table.item(r, 0).text()
            if p in files:
                self.table.setItem(r, 1, QtWidgets.QTableWidgetItem("Queued"))
        for f in files:
            sig = core.AnalyzeSignals()
            sig.started.connect(self.on_task_started)
            sig.finished.connect(self.on_task_finished)
            sig.failed.connect(self.on_task_failed)
            self.threadpool.start(core.AnalyzeTask(f, sig))

    @QtCore.pyqtSlot(str)
    def on_task_started(self, f: str):
        self._set_status(f, "Running...")

    @QtCore.pyqtSlot(str, core.FileResult)
    def on_task_finished(self, f: str, res: core.FileResult):
        self.results[f] = res
        self._set_status(f, "Done")
        row = self._row_of(f)
        if row != -1:
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(f"{res.video_start_s:.6f}"))
        sel = self.get_selected_files()
        if sel and sel[0] == f:
            self.detail.setText(format_result_text(res))

    @QtCore.pyqtSlot(str, str)
    def on_task_failed(self, f: str, err: str):
        self._set_status(f, f"Error: {err}")
        if self.get_selected_files() and self.get_selected_files()[0] == f:
            self.detail.setText(f"ERROR for {f}\n\n{err}")

    def _row_of(self, path: str) -> int:
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).text() == path:
                return r
        return -1

    def _set_status(self, path: str, text: str):
        r = self._row_of(path)
        if r != -1:
            self.table.setItem(r, 1, QtWidgets.QTableWidgetItem(text))

def format_result_text(res: core.FileResult) -> str:
    def row(kind, idx, start_s, relative_ms, lang, codec, title):
        apply_ms = -relative_ms
        apply_sec = apply_ms / 1000.0
        meta = " | ".join([x for x in (lang, codec, title) if x])
        return (
            f"{kind}:{idx:<2} start_s={start_s:.6f}  "
            f"relative={relative_ms:+d} ms  "
            f"APPLY={apply_ms:+d} ms  (mkvmerge --sync 0:{apply_ms:+d} | ffmpeg -itsoffset {apply_sec:+.3f})"
            + (f"    {meta}" if meta else "")
        )

    lines = []
    lines.append(f"File: {res.file_path}")
    lines.append(f"Video start: {res.video_start_s:.6f} s\n")
    lines.append("---- Audio ----")
    if not res.audio:
        lines.append("(no audio streams)")
    else:
        for a in res.audio:
            lines.append(row("a", a.index, a.start_s, a.delay_ms, a.language, a.codec, a.title))
    lines.append("\n---- Subtitles ----")
    if not res.subs:
        lines.append("(no subtitle streams)")
    else:
        for s in res.subs:
            lines.append(row("s", s.index, s.start_s, s.delay_ms, s.language, s.codec, s.title))
    lines.append("\nLegend: relative = (track_start - video_start). APPLY = value you pass to mkvmerge/ffmpeg.")
    return "\n".join(lines)
