# remux_toolkit/tools/audio_authenticity/audio_authenticity_gui.py
"""GUI for the Audio Authenticity & Provenance tool."""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
from PyQt6 import QtCore, QtGui, QtWidgets

from . import audio_authenticity_config as config
from . import audio_authenticity_core as core

MEDIA_SUFFIXES = {".mkv", ".mp4", ".m2ts", ".ts", ".avi", ".mov", ".wav", ".flac",
                  ".ac3", ".eac3", ".dts", ".thd", ".mka", ".m4a", ".aac", ".mp3", ".wv"}

VERDICT_COLOURS = {
    "FAKE LOSSLESS": "#c0392b",
    "DUAL MONO": "#c0392b",
    "DUAL MONO (near-exact)": "#c0392b",
    "FAKE MULTICHANNEL": "#c0392b",
    "NEAR-MONO": "#d35400",
    "BAND-LIMITED": "#d35400",
    "LOSSY (as declared)": "#d35400",
    "NO LOSSY FINGERPRINT": "#27ae60",
    "TRUE STEREO": "#27ae60",
    "DISCRETE MULTICHANNEL": "#27ae60",
    "MONO": "#7f8c8d",
    "IDENTICAL": "#27ae60",
    "SAME MASTER": "#27ae60",
    "SAME MASTER (RE-TIMED)": "#27ae60",
    "SAME MASTER, REWORKED": "#d35400",
    "RELATED": "#d35400",
    "UNRELATED": "#7f8c8d",
    "DECODE FAILED": "#c0392b",
    "ANALYSIS FAILED": "#c0392b",
}


def _colour_lut() -> np.ndarray:
    """Spek-ish ramp: near-black -> blue -> green -> yellow -> red -> white."""
    stops = [(0.00, (0, 0, 10)), (0.20, (20, 20, 120)), (0.40, (0, 150, 140)),
             (0.62, (180, 210, 40)), (0.82, (240, 120, 30)), (1.00, (255, 255, 235))]
    lut = np.zeros((256, 3), dtype=np.uint8)
    for i in range(256):
        f = i / 255.0
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            if p0 <= f <= p1:
                t = (f - p0) / max(p1 - p0, 1e-9)
                lut[i] = [int(c0[k] + t * (c1[k] - c0[k])) for k in range(3)]
                break
    return lut


class SpectrogramView(QtWidgets.QWidget):
    """Spek-style spectrogram with a frequency axis and the detected wall marked."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image: QtGui.QImage | None = None
        self._nyquist = 24000.0
        self._cutoff = 0.0
        self._title = ""
        self._duration = 0.0
        self._floor_db = -120.0
        self.setMinimumHeight(260)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                           QtWidgets.QSizePolicy.Policy.Expanding)

    def set_track(self, track):
        data = getattr(track, "spectrogram", None) if track is not None else None
        if data is None:
            self._image = None
            self._title = "No spectrogram for this track."
            self.update()
            return
        db = np.asarray(data, dtype=np.float32)
        norm = np.clip((db - self._floor_db) / (0.0 - self._floor_db), 0.0, 1.0)
        idx = (norm * 255).astype(np.uint8)
        rgb = _colour_lut()[idx]                     # (rows, cols, 3)
        rgb = np.flipud(rgb).copy()                  # low frequency at the bottom
        h, w, _ = rgb.shape
        self._image = QtGui.QImage(rgb.data, w, h, 3 * w,
                                   QtGui.QImage.Format.Format_RGB888).copy()
        self._nyquist = track.nyquist_hz or 24000.0
        self._cutoff = track.cutoff_hz or 0.0
        self._duration = track.duration_s or 0.0
        self._title = f"{track.name} - {track.content_verdict}"
        self.update()

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor("#111318"))
        left, right, top, bottom = 64, 12, 22, 26
        plot = QtCore.QRect(left, top, max(1, self.width() - left - right),
                            max(1, self.height() - top - bottom))
        painter.setPen(QtGui.QColor("#d0d4dc"))
        if self._image is None:
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter,
                             self._title or "Run an analysis to see spectrograms.")
            painter.end()
            return

        painter.drawText(QtCore.QRect(left, 2, plot.width(), 18),
                         QtCore.Qt.AlignmentFlag.AlignLeft, self._title)
        painter.drawImage(plot, self._image)
        painter.setPen(QtGui.QColor("#555a66"))
        painter.drawRect(plot)

        # frequency axis
        painter.setPen(QtGui.QColor("#aab0bb"))
        step = 2000 if self._nyquist <= 26000 else 5000
        f = 0
        while f <= self._nyquist:
            y = plot.bottom() - int(plot.height() * f / max(self._nyquist, 1))
            painter.drawText(QtCore.QRect(0, y - 8, left - 6, 16),
                             QtCore.Qt.AlignmentFlag.AlignRight, f"{f//1000} kHz")
            painter.drawLine(left - 4, y, left, y)
            f += step

        # time axis
        if self._duration > 0:
            for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
                x = plot.left() + int(plot.width() * frac)
                secs = self._duration * frac
                painter.drawText(QtCore.QRect(x - 30, plot.bottom() + 4, 60, 18),
                                 QtCore.Qt.AlignmentFlag.AlignCenter,
                                 f"{int(secs//60)}:{int(secs%60):02d}")

        # the wall, where there is one
        if 0 < self._cutoff < self._nyquist * 0.999:
            y = plot.bottom() - int(plot.height() * self._cutoff / max(self._nyquist, 1))
            pen = QtGui.QPen(QtGui.QColor("#ff5252"))
            pen.setStyle(QtCore.Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(plot.left(), y, plot.right(), y)
            label = f"content ends at {self._cutoff/1000:.2f} kHz"
            box = QtCore.QRect(plot.left() + 6, y - 20, 200, 17)
            painter.fillRect(box, QtGui.QColor(0, 0, 0, 190))
            painter.setPen(QtGui.QColor("#ff8a80"))
            painter.drawText(box.adjusted(5, 0, 0, 0),
                             QtCore.Qt.AlignmentFlag.AlignVCenter, label)
        painter.end()


class DropList(QtWidgets.QListWidget):
    """Source list that accepts dragged-in files and folders."""
    pathsDropped = QtCore.pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)

    @staticmethod
    def _paths(mime):
        if not mime.hasUrls():
            return []
        return [u.toLocalFile() for u in mime.urls() if u.isLocalFile() and u.toLocalFile()]

    def dragEnterEvent(self, e):
        e.acceptProposedAction() if self._paths(e.mimeData()) else super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        e.acceptProposedAction() if self._paths(e.mimeData()) else super().dragMoveEvent(e)

    def dropEvent(self, e):
        paths = self._paths(e.mimeData())
        if not paths:
            super().dropEvent(e)
            return
        e.acceptProposedAction()
        self.pathsDropped.emit(paths)


class AnalysisWorker(QtCore.QObject):
    progress = QtCore.pyqtSignal(float)
    status = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal(object, str)   # Report | None, error text

    def __init__(self, sources: list[str], settings: core.Settings):
        super().__init__()
        self.sources = sources
        self.settings = settings
        self._stop = False

    def stop(self):
        self._stop = True

    def reset(self):
        """Clear the stop flag so the worker can be reused for another run."""
        self._stop = False

    def run(self):
        try:
            report = core.run_analysis(
                self.sources, self.settings,
                progress=self.progress.emit, status=self.status.emit,
                cancel=lambda: self._stop)
            self.finished.emit(report, "")
        except InterruptedError:
            self.finished.emit(None, "Stopped.")
        except Exception as e:                      # noqa: BLE001 - surfaced in the UI
            self.finished.emit(None, f"{type(e).__name__}: {e}")


class AudioAuthenticityWidget(QtWidgets.QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = "audio_authenticity"
        self.settings: dict = {}
        self.report: core.Report | None = None
        self.worker: AnalysisWorker | None = None
        self.thread: QtCore.QThread | None = None
        self._running = False

        self._init_ui()
        self._load_settings()
        # A previous session that was killed mid-run can leave staged decodes
        # behind; opening the tool is the natural moment to clear them.
        try:
            stale = core.sweep_work_dir(core.Settings.from_dict(self.settings))
            if stale:
                self.status_label.setText(
                    f"Cleared {stale} leftover decode file(s) from an interrupted run.")
        except Exception:
            pass

    # ---------------- UI ----------------
    def _init_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)

        top = QtWidgets.QWidget()
        top_layout = QtWidgets.QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)

        src_group = QtWidgets.QGroupBox("Sources  (drag MKVs or folders here)")
        src_layout = QtWidgets.QVBoxLayout(src_group)
        self.source_list = DropList()
        self.source_list.pathsDropped.connect(self.add_paths)
        src_layout.addWidget(self.source_list)

        btn_row = QtWidgets.QHBoxLayout()
        self.btn_add = QtWidgets.QPushButton("Add Files…")
        self.btn_add.clicked.connect(self._pick_files)
        self.btn_add_folder = QtWidgets.QPushButton("Add Folder…")
        self.btn_add_folder.clicked.connect(self._pick_folder)
        self.btn_remove = QtWidgets.QPushButton("Remove Selected")
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_clear = QtWidgets.QPushButton("Clear")
        self.btn_clear.clicked.connect(self.clear_sources)
        self.btn_analyze = QtWidgets.QPushButton("Analyze")
        self.btn_analyze.clicked.connect(self.start_analysis)
        self.btn_stop = QtWidgets.QPushButton("Stop")
        self.btn_stop.clicked.connect(self.stop_analysis)
        self.btn_stop.setEnabled(False)
        self.btn_save = QtWidgets.QPushButton("Save Report…")
        self.btn_save.clicked.connect(self._save_report)
        self.btn_save.setEnabled(False)
        for b in (self.btn_add, self.btn_add_folder, self.btn_remove, self.btn_clear,
                  self.btn_analyze, self.btn_stop, self.btn_save):
            btn_row.addWidget(b)
        btn_row.addStretch()
        src_layout.addLayout(btn_row)
        top_layout.addWidget(src_group)

        self.status_label = QtWidgets.QLabel("Add one or more sources, then Analyze.")
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        top_layout.addWidget(self.status_label)
        top_layout.addWidget(self.progress)
        splitter.addWidget(top)

        self.tabs = QtWidgets.QTabWidget()
        self.track_table = self._make_table(
            ["Source", "Track", "Codec", "Ch", "Rate", "Bandwidth", "Content", "Channels", "Issues"])
        self.track_table.itemSelectionChanged.connect(self._track_row_selected)
        self.tabs.addTab(self.track_table, "Tracks")
        self.match_table = self._make_table(
            ["Track A", "Track B", "Verdict", "Offset", "Null", "Windows", "Notes"])
        self.tabs.addTab(self.match_table, "Source Matching")
        spec_page = QtWidgets.QWidget()
        spec_layout = QtWidgets.QVBoxLayout(spec_page)
        spec_layout.setContentsMargins(4, 4, 4, 4)
        picker_row = QtWidgets.QHBoxLayout()
        picker_row.addWidget(QtWidgets.QLabel("Track:"))
        self.spec_picker = QtWidgets.QComboBox()
        self.spec_picker.currentIndexChanged.connect(self._show_spectrogram)
        picker_row.addWidget(self.spec_picker, 1)
        spec_layout.addLayout(picker_row)
        self.spectrogram_view = SpectrogramView()
        spec_layout.addWidget(self.spectrogram_view, 1)
        self.tabs.addTab(spec_page, "Spectrogram")

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QtGui.QFontDatabase.systemFont(
            QtGui.QFontDatabase.SystemFont.FixedFont))
        self.tabs.addTab(self.log, "Full Report")
        splitter.addWidget(self.tabs)
        splitter.setSizes([260, 520])
        layout.addWidget(splitter)

    @staticmethod
    def _make_table(headers: list[str]) -> QtWidgets.QTableWidget:
        t = QtWidgets.QTableWidget()
        t.setColumnCount(len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        t.setAlternatingRowColors(True)
        hdr = t.horizontalHeader()
        for i in range(len(headers) - 1):
            hdr.setSectionResizeMode(i, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(len(headers) - 1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        return t

    # ---------------- sources ----------------
    def add_paths(self, paths: list[str]):
        added = 0
        for p in paths:
            path = Path(p)
            if path.is_dir():
                for f in sorted(path.rglob("*")):
                    if f.suffix.lower() in MEDIA_SUFFIXES and self._add_one(str(f)):
                        added += 1
            elif self._add_one(str(path)):
                added += 1
        if added:
            self.status_label.setText(f"Added {added} file(s); {self.source_list.count()} total.")
        elif paths:
            self.status_label.setText("Nothing added - already listed, or no media files found.")

    def _add_one(self, path: str) -> bool:
        if not os.path.isfile(path):
            return False
        if any(self.source_list.item(i).text() == path for i in range(self.source_list.count())):
            return False
        self.source_list.addItem(path)
        return True

    def _pick_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Select media files", str(Path.home()),
            "Media (*.mkv *.mp4 *.m2ts *.ts *.wav *.flac *.ac3 *.dts *.thd *.mka);;All files (*)")
        if files:
            self.add_paths(files)

    def _pick_folder(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Select folder", str(Path.home()))
        if d:
            self.add_paths([d])

    def _remove_selected(self):
        for item in self.source_list.selectedItems():
            self.source_list.takeItem(self.source_list.row(item))

    def clear_sources(self):
        self.source_list.clear()
        self.track_table.setRowCount(0)
        self.match_table.setRowCount(0)
        self.log.clear()
        self.report = None
        self.btn_save.setEnabled(False)
        self.status_label.setText("Add one or more sources, then Analyze.")

    def _sources(self) -> list[str]:
        return [self.source_list.item(i).text() for i in range(self.source_list.count())]

    # ---------------- run ----------------
    def start_analysis(self):
        if self._running:
            return
        sources = self._sources()
        if not sources:
            self.status_label.setText("No sources listed.")
            return
        ok, msg = core.check_dependencies()
        if not ok:
            self.status_label.setText(msg)
            return

        # A previous run must be fully joined: QThread.start() is a silent
        # no-op on a thread that has not finished.
        self._join_thread()

        self.track_table.setRowCount(0)
        self.match_table.setRowCount(0)
        self.log.clear()
        self.report = None
        self.btn_save.setEnabled(False)
        self.progress.setValue(0)

        self.worker = AnalysisWorker(sources, core.Settings.from_dict(self.settings))
        self.worker.reset()
        self.thread = QtCore.QThread(self)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(lambda f: self.progress.setValue(int(f * 1000)))
        self.worker.status.connect(self.status_label.setText)
        self.worker.finished.connect(self._on_finished)

        self._running = True
        self._set_busy(True)
        self.thread.start()

    def stop_analysis(self):
        if self._running and self.worker:
            self.worker.stop()
            self.status_label.setText("Stopping…")
            self.btn_stop.setEnabled(False)

    def _join_thread(self, timeout_ms: int = 15000):
        if self.thread is not None:
            if self.worker is not None:
                self.worker.stop()
            if self.thread.isRunning():
                self.thread.quit()
                self.thread.wait(timeout_ms)
            self.thread.deleteLater()
        self.thread = None
        self.worker = None

    def _set_busy(self, busy: bool):
        for b in (self.btn_analyze, self.btn_add, self.btn_add_folder,
                  self.btn_remove, self.btn_clear):
            b.setEnabled(not busy)
        self.btn_stop.setEnabled(busy)

    def _on_finished(self, report, error: str):
        self._running = False
        self._set_busy(False)
        if self.thread is not None:
            self.thread.quit()
        if report is None:
            self.status_label.setText(error or "Analysis failed.")
            self.progress.setValue(0)
            return

        self.report = report
        self._fill_tracks(report)
        self._fill_matches(report)
        self._fill_spectrograms(report)
        self.log.setPlainText(core.format_report(report))
        self.btn_save.setEnabled(True)
        self.progress.setValue(1000)

        flagged = sum(1 for t in report.tracks
                      if t.content_verdict in ("FAKE LOSSLESS",)
                      or t.channel_verdict.startswith(("DUAL MONO", "FAKE", "NEAR-MONO")))
        matches = sum(1 for p in report.pairs
                      if p.tier in ("IDENTICAL", "SAME MASTER", "SAME MASTER (RE-TIMED)"))
        self.status_label.setText(
            f"Done: {len(report.tracks)} track(s), {flagged} flagged, "
            f"{len(report.pairs)} pair(s) compared, {matches} same-master match(es)."
            + (f"  {len(report.errors)} error(s)." if report.errors else ""))

    # ---------------- results ----------------
    @staticmethod
    def _cell(text: str, colour: str | None = None, tooltip: str = "") -> QtWidgets.QTableWidgetItem:
        item = QtWidgets.QTableWidgetItem(text)
        if colour:
            item.setForeground(QtGui.QColor(colour))
        if tooltip:
            item.setToolTip(tooltip)
        return item

    def _fill_tracks(self, report: core.Report):
        self.track_table.setRowCount(0)
        for t in report.tracks:
            r = self.track_table.rowCount()
            self.track_table.insertRow(r)
            declared = {True: "lossless", False: "lossy", None: "unknown"}[t.declared_lossless]
            codec = f"{t.profile or t.codec}"
            if t.declared_bits:
                codec += f" {t.declared_bits}bit"
            band = (f"{t.cutoff_hz/1000:.2f} kHz" if t.cutoff_hz else "-")
            if t.wall_db_per_khz >= 8:
                band += f"  wall {t.wall_db_per_khz:.0f} dB/kHz"
            cells = [
                self._cell(t.source_label, tooltip=t.source),
                self._cell(f"#{t.stream_index} {t.language}"
                           + (f" ({t.title})" if t.title else "")),
                self._cell(codec, tooltip=f"container declares: {declared}"),
                self._cell(str(t.channels)),
                self._cell(f"{t.sample_rate}"),
                self._cell(band),
                self._cell(t.content_verdict, VERDICT_COLOURS.get(t.content_verdict),
                           "\n".join(t.content_reasons)),
                self._cell(t.channel_verdict, VERDICT_COLOURS.get(t.channel_verdict),
                           "\n".join(t.channel_reasons)),
                self._cell("; ".join(t.issues) if t.issues else "-",
                           "#d35400" if t.issues else None, "\n".join(t.issues)),
            ]
            for c, item in enumerate(cells):
                self.track_table.setItem(r, c, item)

    def _track_row_selected(self):
        rows = {i.row() for i in self.track_table.selectedIndexes()}
        if not rows or not self.report:
            return
        row = next(iter(rows))
        for i in range(self.spec_picker.count()):
            if self.spec_picker.itemData(i) == row:
                self.spec_picker.setCurrentIndex(i)
                break

    def _fill_matches(self, report: core.Report):
        self.match_table.setRowCount(0)
        order = {"IDENTICAL": 0, "SAME MASTER": 1, "SAME MASTER (RE-TIMED)": 2,
                 "SAME MASTER, REWORKED": 3, "RELATED": 4, "UNRELATED": 5, "NO DATA": 6}
        for p in sorted(report.pairs, key=lambda x: (order.get(x.tier, 9), x.null_db)):
            r = self.match_table.rowCount()
            self.match_table.insertRow(r)
            cells = [
                self._cell(p.a), self._cell(p.b),
                self._cell(p.tier, VERDICT_COLOURS.get(p.tier)),
                self._cell(f"{p.offset_ms:+.2f} ms"),
                self._cell(f"{p.null_db:+.1f} dB"),
                self._cell(f"{p.locked_windows}/{p.total_windows}"),
                self._cell(" ".join(p.notes), tooltip="\n".join(p.notes)),
            ]
            for c, item in enumerate(cells):
                self.match_table.setItem(r, c, item)

    def _fill_spectrograms(self, report: core.Report):
        self.spec_picker.blockSignals(True)
        self.spec_picker.clear()
        for i, t in enumerate(report.tracks):
            if getattr(t, "spectrogram", None) is not None:
                self.spec_picker.addItem(f"{t.name} - {t.content_verdict}", i)
        self.spec_picker.blockSignals(False)
        if self.spec_picker.count():
            self.spec_picker.setCurrentIndex(0)
            self._show_spectrogram(0)
        else:
            self.spectrogram_view.set_track(None)

    def _show_spectrogram(self, _index: int):
        if not self.report:
            return
        data = self.spec_picker.currentData()
        if data is None:
            return
        self.spectrogram_view.set_track(self.report.tracks[data])

    def _save_report(self):
        if not self.report:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save report", str(Path.home() / "audio-authenticity.txt"),
            "Text (*.txt);;All files (*)")
        if not path:
            return
        try:
            Path(path).write_text(core.format_report(self.report), encoding="utf-8")
            self.status_label.setText(f"Report saved to {path}")
        except OSError as e:
            self.status_label.setText(f"Could not save: {e}")

    # ---------------- lifecycle ----------------
    def _load_settings(self):
        self.settings = self.app_manager.load_config(self.tool_name, config.DEFAULTS)
        for p in self.settings.get("sources", []) or []:
            self._add_one(p)

    def save_settings(self):
        self.settings["sources"] = self._sources()
        self.app_manager.save_config(self.tool_name, self.settings)

    def shutdown(self):
        """Closing the tab should free everything, like closing an app."""
        self._join_thread(10000)
        self.report = None
        self.spectrogram_view.set_track(None)
        self.spec_picker.clear()
        self.track_table.setRowCount(0)
        self.match_table.setRowCount(0)
        self.log.clear()
        try:
            core.sweep_work_dir(core.Settings.from_dict(self.settings))
        except Exception:
            pass
