"""Video Source Analyzer — PyQt6 GUI."""

from __future__ import annotations

import json
import os
from pathlib import Path

from PyQt6 import QtWidgets, QtCore, QtGui

from .video_source_analyzer_config import DEFAULTS
from .core.worker import AnalysisWorker
from .core.models import AnalysisResult, Segment


# ═══════════════════════════════════════════════════════════════════════════
# Segment Map Widget (custom painted color bar)
# ═══════════════════════════════════════════════════════════════════════════

_SEG_COLORS = {
    "FILM": QtGui.QColor(76, 175, 80),     # green
    "VIDEO": QtGui.QColor(244, 67, 54),     # red
    "MIXED": QtGui.QColor(255, 193, 7),     # amber
}


class SegmentMapWidget(QtWidgets.QWidget):
    """Color-coded horizontal bar showing FILM/VIDEO/MIXED segments."""

    segment_clicked = QtCore.pyqtSignal(int)  # index of clicked segment

    def __init__(self, parent=None):
        super().__init__(parent)
        self.segments: list[Segment] = []
        self.total_frames: int = 0
        self.fps: float = 29.97
        self._selected_idx: int = -1
        self._hover_idx: int = -1
        self.setMinimumHeight(48)
        self.setMaximumHeight(64)
        self.setMouseTracking(True)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )

    def set_data(self, segments: list[Segment], total_frames: int, fps: float):
        self.segments = segments
        self.total_frames = total_frames if total_frames > 0 else 1
        self.fps = fps if fps > 0 else 29.97
        self._selected_idx = -1
        self._hover_idx = -1
        self.update()

    def clear_data(self):
        self.segments = []
        self.total_frames = 0
        self._selected_idx = -1
        self._hover_idx = -1
        self.update()

    def _seg_rect(self, idx: int) -> QtCore.QRectF:
        if not self.segments or self.total_frames <= 0:
            return QtCore.QRectF()
        seg = self.segments[idx]
        w = self.width()
        h = self.height()
        x = seg.start_frame / self.total_frames * w
        seg_w = seg.frame_count / self.total_frames * w
        return QtCore.QRectF(x, 0, max(seg_w, 2), h)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        if not self.segments:
            painter.fillRect(self.rect(), QtGui.QColor(60, 60, 60))
            painter.setPen(QtGui.QColor(120, 120, 120))
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter,
                             "No segment data")
            painter.end()
            return

        # Draw segments
        for i, seg in enumerate(self.segments):
            rect = self._seg_rect(i)
            color = _SEG_COLORS.get(seg.seg_type, QtGui.QColor(100, 100, 100))

            if i == self._selected_idx:
                color = color.lighter(130)
            elif i == self._hover_idx:
                color = color.lighter(115)

            painter.fillRect(rect, color)

            # Draw border between segments
            if i > 0:
                painter.setPen(QtGui.QColor(30, 30, 30))
                painter.drawLine(
                    QtCore.QPointF(rect.x(), 0),
                    QtCore.QPointF(rect.x(), self.height()),
                )

        # Draw selection indicator
        if 0 <= self._selected_idx < len(self.segments):
            rect = self._seg_rect(self._selected_idx)
            pen = QtGui.QPen(QtGui.QColor(255, 255, 255), 2)
            painter.setPen(pen)
            painter.drawRect(rect.adjusted(1, 1, -1, -1))

        painter.end()

    def mouseMoveEvent(self, event):
        old = self._hover_idx
        self._hover_idx = self._idx_at(event.position())
        if self._hover_idx != old:
            self.update()

        # Tooltip
        if 0 <= self._hover_idx < len(self.segments):
            seg = self.segments[self._hover_idx]
            t_start = seg.start_frame / self.fps
            t_end = seg.end_frame / self.fps
            tip = (
                f"{seg.seg_type}  |  Frames {seg.start_frame:,}-{seg.end_frame:,}\n"
                f"Time {t_start:.1f}s - {t_end:.1f}s  |  {seg.frame_count:,} frames\n"
                f"Film cycling: {seg.cycling_pct:.0f}%  |  Interlaced: {seg.interlaced_pct:.0f}%"
            )
            QtWidgets.QToolTip.showText(event.globalPosition().toPoint(), tip, self)
        else:
            QtWidgets.QToolTip.hideText()

    def mousePressEvent(self, event):
        idx = self._idx_at(event.position())
        if idx >= 0:
            self._selected_idx = idx
            self.update()
            self.segment_clicked.emit(idx)

    def leaveEvent(self, event):
        self._hover_idx = -1
        self.update()

    def _idx_at(self, pos) -> int:
        for i in range(len(self.segments)):
            if self._seg_rect(i).contains(pos):
                return i
        return -1


# ═══════════════════════════════════════════════════════════════════════════
# Main Widget
# ═══════════════════════════════════════════════════════════════════════════

class VideoSourceAnalyzerWidget(QtWidgets.QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = "video_source_analyzer"
        self.results: dict[str, AnalysisResult] = {}
        self._worker: AnalysisWorker | None = None
        self._thread: QtCore.QThread | None = None
        self.setAcceptDrops(True)
        self._init_ui()
        self._load_settings()

    # ── UI Setup ───────────────────────────────────────────────────────

    def _init_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)

        # ── Left Panel (file list + controls) ──────────────────────────
        left_widget = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self.file_list = QtWidgets.QListWidget()
        self.file_list.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.file_list.itemSelectionChanged.connect(self._on_selection_changed)

        # Buttons
        btn_row1 = QtWidgets.QHBoxLayout()
        self.add_files_btn = QtWidgets.QPushButton("Add Files...")
        self.add_files_btn.clicked.connect(self._add_files)
        self.add_folder_btn = QtWidgets.QPushButton("Add Folder...")
        self.add_folder_btn.clicked.connect(self._add_folder)
        self.clear_btn = QtWidgets.QPushButton("Clear")
        self.clear_btn.clicked.connect(self._clear_list)
        btn_row1.addWidget(self.add_files_btn)
        btn_row1.addWidget(self.add_folder_btn)
        btn_row1.addWidget(self.clear_btn)

        btn_row2 = QtWidgets.QHBoxLayout()
        self.analyze_btn = QtWidgets.QPushButton("Analyze All")
        self.analyze_btn.clicked.connect(self._analyze_all)
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop_analysis)
        self.stop_btn.setEnabled(False)
        btn_row2.addWidget(self.analyze_btn)
        btn_row2.addWidget(self.stop_btn)

        # Options
        opts_layout = QtWidgets.QHBoxLayout()
        self.auto_l2_cb = QtWidgets.QCheckBox("Auto Layer 2")
        self.auto_l2_cb.setToolTip(
            "Automatically run pixel analysis when bitstream alone is ambiguous"
        )
        self.auto_l3_cb = QtWidgets.QCheckBox("Auto Layer 3")
        self.auto_l3_cb.setToolTip(
            "Automatically run field-swap validation when Layer 2 is ambiguous"
        )
        opts_layout.addWidget(self.auto_l2_cb)
        opts_layout.addWidget(self.auto_l3_cb)
        opts_layout.addStretch()

        # Progress
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setVisible(False)
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setStyleSheet("color: #aaa; font-size: 11px;")

        left_layout.addWidget(self.file_list)
        left_layout.addLayout(btn_row1)
        left_layout.addLayout(btn_row2)
        left_layout.addLayout(opts_layout)
        left_layout.addWidget(self.progress_bar)
        left_layout.addWidget(self.status_label)

        splitter.addWidget(left_widget)

        # ── Right Panel (tabbed detail view) ───────────────────────────
        self.tabs = QtWidgets.QTabWidget()

        # Tab 1: Summary
        self.summary_text = QtWidgets.QTextEdit()
        self.summary_text.setReadOnly(True)
        self.summary_text.setFont(QtGui.QFont("Monospace", 10))
        self.tabs.addTab(self.summary_text, "Summary")

        # Tab 2: Segment Map
        seg_widget = QtWidgets.QWidget()
        seg_layout = QtWidgets.QVBoxLayout(seg_widget)
        seg_layout.setContentsMargins(4, 4, 4, 4)

        # Legend
        legend_layout = QtWidgets.QHBoxLayout()
        for label, color in [("FILM", _SEG_COLORS["FILM"]),
                              ("VIDEO", _SEG_COLORS["VIDEO"]),
                              ("MIXED", _SEG_COLORS["MIXED"])]:
            swatch = QtWidgets.QLabel(f"  {label}  ")
            swatch.setStyleSheet(
                f"background-color: {color.name()}; color: white; "
                f"font-weight: bold; padding: 2px 8px; border-radius: 3px;"
            )
            legend_layout.addWidget(swatch)
        legend_layout.addStretch()
        seg_layout.addLayout(legend_layout)

        self.segment_map = SegmentMapWidget()
        self.segment_map.segment_clicked.connect(self._on_segment_clicked)
        seg_layout.addWidget(self.segment_map)

        # Segment detail table
        self.segment_table = QtWidgets.QTableWidget()
        self.segment_table.setColumnCount(7)
        self.segment_table.setHorizontalHeaderLabels([
            "Type", "Start Frame", "End Frame",
            "Frame Count", "Duration", "Film Cycling %", "Interlaced %",
        ])
        self.segment_table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        self.segment_table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.segment_table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.segment_table.itemSelectionChanged.connect(
            self._on_segment_table_selected
        )
        seg_layout.addWidget(self.segment_table)

        self.tabs.addTab(seg_widget, "Segment Map")

        # Tab 3: Layer Details
        self.detail_tree = QtWidgets.QTreeWidget()
        self.detail_tree.setHeaderLabels(["Property", "Value"])
        self.detail_tree.header().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.detail_tree.header().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        self.tabs.addTab(self.detail_tree, "Layer Details")

        # Tab 4: Export
        export_widget = QtWidgets.QWidget()
        export_layout = QtWidgets.QVBoxLayout(export_widget)
        export_layout.setContentsMargins(8, 8, 8, 8)

        export_layout.addWidget(QtWidgets.QLabel(
            "Export analysis results for the selected file or all files."
        ))

        # Export path
        path_row = QtWidgets.QHBoxLayout()
        path_row.addWidget(QtWidgets.QLabel("Export directory:"))
        self.export_path_edit = QtWidgets.QLineEdit()
        self.export_path_edit.setPlaceholderText("Select export directory...")
        path_row.addWidget(self.export_path_edit)
        browse_btn = QtWidgets.QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_export_dir)
        path_row.addWidget(browse_btn)
        export_layout.addLayout(path_row)

        # Export buttons
        btn_grid = QtWidgets.QGridLayout()
        self.export_full_btn = QtWidgets.QPushButton("Export JSON (Full Detail)")
        self.export_full_btn.setToolTip(
            "All per-frame data — use for accuracy analysis and refinement"
        )
        self.export_full_btn.clicked.connect(lambda: self._export("full"))

        self.export_summary_btn = QtWidgets.QPushButton("Export JSON (Summary Only)")
        self.export_summary_btn.setToolTip(
            "Classification + metrics, no per-frame data"
        )
        self.export_summary_btn.clicked.connect(lambda: self._export("summary"))

        self.export_log_btn = QtWidgets.QPushButton("Export Log (.log)")
        self.export_log_btn.setToolTip("Human-readable summary text")
        self.export_log_btn.clicked.connect(lambda: self._export("log"))

        self.export_all_btn = QtWidgets.QPushButton("Export All Files (Full)")
        self.export_all_btn.setToolTip(
            "Export full JSON for every analyzed file"
        )
        self.export_all_btn.clicked.connect(lambda: self._export("all_full"))

        btn_grid.addWidget(self.export_full_btn, 0, 0)
        btn_grid.addWidget(self.export_summary_btn, 0, 1)
        btn_grid.addWidget(self.export_log_btn, 1, 0)
        btn_grid.addWidget(self.export_all_btn, 1, 1)
        export_layout.addLayout(btn_grid)

        export_layout.addStretch()
        self.tabs.addTab(export_widget, "Export")

        splitter.addWidget(self.tabs)
        splitter.setSizes([350, 650])
        layout.addWidget(splitter)

    # ── Settings ───────────────────────────────────────────────────────

    def _load_settings(self):
        settings = self.app_manager.load_config(self.tool_name, DEFAULTS)
        self.auto_l2_cb.setChecked(settings.get("auto_layer2", True))
        self.auto_l3_cb.setChecked(settings.get("auto_layer3", True))
        self.export_path_edit.setText(settings.get("last_export_dir", ""))

    def save_settings(self):
        settings = {
            "auto_layer2": self.auto_l2_cb.isChecked(),
            "auto_layer3": self.auto_l3_cb.isChecked(),
            "last_export_dir": self.export_path_edit.text(),
        }
        self.app_manager.save_config(self.tool_name, settings)

    # ── File Management ────────────────────────────────────────────────

    def _add_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Select Video Files", "",
            "Video Files (*.mkv *.avi *.mp4 *.mpg *.mpeg *.vob *.m2ts *.ts);;All Files (*.*)",
        )
        if files:
            self._add_paths(files)

    def _add_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select Folder"
        )
        if folder:
            video_exts = {".mkv", ".avi", ".mp4", ".mpg", ".mpeg", ".vob",
                          ".m2ts", ".ts", ".m2v"}
            files = []
            for root, _, filenames in os.walk(folder):
                for fn in sorted(filenames):
                    if Path(fn).suffix.lower() in video_exts:
                        files.append(os.path.join(root, fn))
            if files:
                self._add_paths(files)
            else:
                self.status_label.setText("No video files found in folder.")

    def _add_paths(self, paths: list[str]):
        current = {
            self.file_list.item(i).data(QtCore.Qt.ItemDataRole.UserRole)
            for i in range(self.file_list.count())
        }
        for p in paths:
            if p not in current:
                item = QtWidgets.QListWidgetItem(os.path.basename(p))
                item.setData(QtCore.Qt.ItemDataRole.UserRole, p)
                item.setForeground(QtGui.QColor(150, 150, 150))
                self.file_list.addItem(item)

    def _clear_list(self):
        self.file_list.clear()
        self.results.clear()
        self._clear_detail_panels()

    def _clear_detail_panels(self):
        self.summary_text.clear()
        self.segment_map.clear_data()
        self.segment_table.setRowCount(0)
        self.detail_tree.clear()

    # ── Drag & Drop ────────────────────────────────────────────────────

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = []
        video_exts = {".mkv", ".avi", ".mp4", ".mpg", ".mpeg", ".vob",
                      ".m2ts", ".ts", ".m2v"}
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if os.path.isdir(p):
                for root, _, filenames in os.walk(p):
                    for fn in sorted(filenames):
                        if Path(fn).suffix.lower() in video_exts:
                            paths.append(os.path.join(root, fn))
            elif Path(p).suffix.lower() in video_exts:
                paths.append(p)
        if paths:
            self._add_paths(paths)

    # ── Analysis ───────────────────────────────────────────────────────

    def _analyze_all(self):
        file_paths = []
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            fp = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if fp:
                file_paths.append(fp)
                item.setForeground(QtGui.QColor(150, 150, 150))  # reset

        if not file_paths:
            return

        self._set_analyzing(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, len(file_paths))
        self.progress_bar.setValue(0)
        self._files_done = 0
        self._files_total = len(file_paths)

        # Create worker + thread
        self._thread = QtCore.QThread(self)
        self._worker = AnalysisWorker()
        self._worker.moveToThread(self._thread)

        self._worker.file_started.connect(self._on_file_started)
        self._worker.layer_progress.connect(self._on_layer_progress)
        self._worker.file_finished.connect(self._on_file_finished)
        self._worker.file_error.connect(self._on_file_error)
        self._worker.batch_finished.connect(self._on_batch_finished)

        auto_l2 = self.auto_l2_cb.isChecked()
        auto_l3 = self.auto_l3_cb.isChecked()

        self._thread.started.connect(
            lambda: self._worker.analyze_files(file_paths, auto_l2, auto_l3)
        )
        self._thread.start()

    def _stop_analysis(self):
        if self._worker:
            self._worker.stop()
        self.status_label.setText("Stopping...")

    def _set_analyzing(self, analyzing: bool):
        self.analyze_btn.setEnabled(not analyzing)
        self.stop_btn.setEnabled(analyzing)
        self.add_files_btn.setEnabled(not analyzing)
        self.add_folder_btn.setEnabled(not analyzing)
        self.clear_btn.setEnabled(not analyzing)

    def _on_file_started(self, filepath: str):
        # Mark item orange
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item.data(QtCore.Qt.ItemDataRole.UserRole) == filepath:
                item.setForeground(QtGui.QColor("orange"))
                break
        self.status_label.setText(f"Analyzing: {os.path.basename(filepath)}")

    def _on_layer_progress(self, filepath: str, msg: str):
        self.status_label.setText(msg)

    def _on_file_finished(self, filepath: str, result: AnalysisResult):
        self.results[filepath] = result
        self._files_done += 1
        self.progress_bar.setValue(self._files_done)

        # Color by classification
        color = self._classification_color(result.classification.classification)
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item.data(QtCore.Qt.ItemDataRole.UserRole) == filepath:
                item.setForeground(color)
                # Show classification in display text
                cls = result.classification.classification.upper().replace("_", " ")
                conf = result.classification.confidence
                item.setText(f"{os.path.basename(filepath)}  [{cls} ({conf})]")
                break

        # Auto-select if it's the only/current file
        selected = self.file_list.selectedItems()
        if not selected or len(selected) == 1:
            for i in range(self.file_list.count()):
                item = self.file_list.item(i)
                if item.data(QtCore.Qt.ItemDataRole.UserRole) == filepath:
                    self.file_list.setCurrentItem(item)
                    break

    def _on_file_error(self, filepath: str, error: str):
        self._files_done += 1
        self.progress_bar.setValue(self._files_done)

        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item.data(QtCore.Qt.ItemDataRole.UserRole) == filepath:
                item.setForeground(QtGui.QColor("red"))
                item.setText(f"{os.path.basename(filepath)}  [ERROR]")
                break

        self.status_label.setText(f"Error: {error}")

    def _on_batch_finished(self):
        self._set_analyzing(False)
        self.progress_bar.setVisible(False)
        self.status_label.setText(
            f"Done: {self._files_done}/{self._files_total} files analyzed"
        )
        if self._thread:
            self._thread.quit()
            self._thread.wait()

    def _classification_color(self, cls: str) -> QtGui.QColor:
        colors = {
            "soft_telecine": QtGui.QColor(76, 175, 80),      # green
            "soft_telecine_mixed": QtGui.QColor(139, 195, 74),
            "hard_telecine": QtGui.QColor(33, 150, 243),     # blue
            "interlaced": QtGui.QColor(244, 67, 54),          # red
            "progressive": QtGui.QColor(0, 188, 212),         # cyan
            "mixed": QtGui.QColor(255, 193, 7),               # amber
            "unknown": QtGui.QColor(158, 158, 158),           # gray
        }
        return colors.get(cls, QtGui.QColor(200, 200, 200))

    # ── Selection → Detail Update ──────────────────────────────────────

    def _on_selection_changed(self):
        items = self.file_list.selectedItems()
        if not items:
            self._clear_detail_panels()
            return

        filepath = items[0].data(QtCore.Qt.ItemDataRole.UserRole)
        result = self.results.get(filepath)
        if not result:
            self._clear_detail_panels()
            self.summary_text.setPlainText("Not yet analyzed.")
            return

        self._display_summary(result)
        self._display_segment_map(result)
        self._display_layer_details(result)

    def _display_summary(self, result: AnalysisResult):
        self.summary_text.setPlainText(result.summary_text())

    def _display_segment_map(self, result: AnalysisResult):
        bs = result.bitstream
        if bs and bs.segments:
            fps = result.stream_info.fps if result.stream_info.fps > 0 else 29.97
            self.segment_map.set_data(bs.segments, bs.coded_frames, fps)

            # Populate table
            self.segment_table.setRowCount(len(bs.segments))
            for row, seg in enumerate(bs.segments):
                t_start = seg.start_frame / fps
                t_end = seg.end_frame / fps
                duration = f"{t_start:.1f}s - {t_end:.1f}s ({seg.duration_sec:.1f}s)"

                items = [
                    seg.seg_type,
                    f"{seg.start_frame:,}",
                    f"{seg.end_frame:,}",
                    f"{seg.frame_count:,}",
                    duration,
                    f"{seg.cycling_pct:.1f}%",
                    f"{seg.interlaced_pct:.1f}%",
                ]
                for col, text in enumerate(items):
                    cell = QtWidgets.QTableWidgetItem(text)
                    cell.setFlags(
                        QtCore.Qt.ItemFlag.ItemIsEnabled
                        | QtCore.Qt.ItemFlag.ItemIsSelectable
                    )
                    # Color-code the type column
                    if col == 0:
                        color = _SEG_COLORS.get(seg.seg_type)
                        if color:
                            cell.setForeground(color)
                    self.segment_table.setItem(row, col, cell)
        else:
            self.segment_map.clear_data()
            self.segment_table.setRowCount(0)

    def _on_segment_clicked(self, idx: int):
        if 0 <= idx < self.segment_table.rowCount():
            self.segment_table.selectRow(idx)

    def _on_segment_table_selected(self):
        rows = self.segment_table.selectionModel().selectedRows()
        if rows:
            self.segment_map._selected_idx = rows[0].row()
            self.segment_map.update()

    def _display_layer_details(self, result: AnalysisResult):
        tree = self.detail_tree
        tree.clear()

        # Stream Info
        si_node = QtWidgets.QTreeWidgetItem(tree, ["Stream Info", ""])
        si = result.stream_info
        for k, v in [
            ("Codec", f"{si.codec} ({si.codec_id})"),
            ("Resolution", f"{si.width}x{si.height}"),
            ("Frame Rate", f"{si.fps} fps ({si.fps_mode})"),
            ("Original FPS", si.fps_original),
            ("Scan Type", si.scan_type),
            ("Scan Order", si.scan_order),
            ("Duration", f"{si.duration_sec:.1f}s ({si.duration_sec / 60:.1f} min)"),
            ("Frame Count", f"{si.frame_count:,}"),
            ("MPEG-2", str(si.is_mpeg2)),
            ("VFR", str(si.is_vfr)),
            ("Pulldown", str(si.has_pulldown)),
        ]:
            QtWidgets.QTreeWidgetItem(si_node, [k, str(v)])

        # Layer 1
        bs = result.bitstream
        if bs and not bs.error:
            l1_node = QtWidgets.QTreeWidgetItem(tree, ["Layer 1: Bitstream", ""])
            for k, v in [
                ("Film%", f"{bs.film_pct:.2f}%"),
                ("Field Repeats", f"{bs.field_rpts:,}"),
                ("Frame Repeats", f"{bs.frame_rpts:,}"),
                ("Coded Frames", f"{bs.coded_frames:,}"),
                ("Playback Frames", f"{bs.playback_frames:,}"),
                ("Field Order", bs.dominant_field_order),
                ("Cycling (FILM)", f"{bs.cycling_count:,}"),
                ("Not Cycling (VIDEO)", f"{bs.not_cycling_count:,}"),
                ("Progressive", f"{bs.progressive_frames:,} ({bs.progressive_pct:.1f}%)"),
                ("Interlaced", f"{bs.interlaced_frames:,} ({bs.interlaced_pct:.1f}%)"),
                ("I-frames", f"{bs.i_frames:,}"),
                ("P-frames", f"{bs.p_frames:,}"),
                ("B-frames", f"{bs.b_frames:,}"),
                ("Time", f"{bs.elapsed_sec:.1f}s"),
            ]:
                QtWidgets.QTreeWidgetItem(l1_node, [k, str(v)])

            # Flag combos
            if bs.flag_combos:
                combo_node = QtWidgets.QTreeWidgetItem(l1_node, ["Flag Combos", ""])
                total = bs.coded_frames or 1
                for combo, count in sorted(bs.flag_combos.items(), key=lambda x: -x[1]):
                    pct = count / total * 100
                    QtWidgets.QTreeWidgetItem(combo_node, [combo, f"{count:,} ({pct:.1f}%)"])

            # TRF distribution
            if bs.trf_distribution:
                trf_node = QtWidgets.QTreeWidgetItem(l1_node, ["TRF Distribution", ""])
                trf_names = {"0": "TFF=0 RFF=0", "1": "TFF=0 RFF=1",
                             "2": "TFF=1 RFF=0", "3": "TFF=1 RFF=1"}
                total = bs.coded_frames or 1
                for trf_val, count in sorted(bs.trf_distribution.items()):
                    name = trf_names.get(trf_val, f"trf={trf_val}")
                    pct = count / total * 100
                    QtWidgets.QTreeWidgetItem(trf_node, [f"trf={trf_val} ({name})", f"{count:,} ({pct:.1f}%)"])

        # Layer 2
        px = result.pixel
        if px and not px.error:
            l2_node = QtWidgets.QTreeWidgetItem(tree, ["Layer 2: Pixel Analysis", ""])

            chi_node = QtWidgets.QTreeWidgetItem(l2_node, ["Chi-Square Test", ""])
            for k, v in [
                ("Detected", str(px.chi_square_detected)),
                ("Energy Ratio", f"{px.energy_ratio:.4f}"),
                ("Std Ratio", f"{px.std_ratio:.4f}"),
            ]:
                QtWidgets.QTreeWidgetItem(chi_node, [k, v])

            xn_node = QtWidgets.QTreeWidgetItem(l2_node, ["Field Diff x[n]", ""])
            for k, v in [
                ("Mean", f"{px.xn_mean:.4f}"),
                ("Std", f"{px.xn_std:.4f}"),
                ("Median", f"{px.xn_median:.4f}"),
            ]:
                QtWidgets.QTreeWidgetItem(xn_node, [k, v])

            comb_node = QtWidgets.QTreeWidgetItem(l2_node, ["Combing Stats", ""])
            for k, v in [
                ("Combed Frames", f"{px.combed_frames:,} ({px.combed_pct:.1f}%)"),
                ("Median Ratio", f"{px.median_ratio:.4f}"),
            ]:
                QtWidgets.QTreeWidgetItem(comb_node, [k, v])

            ac_node = QtWidgets.QTreeWidgetItem(l2_node, ["Autocorrelation", ""])
            for k, v in [
                ("x[n] Lag5/Lag1", f"{px.xn_lag5_lag1_ratio:.3f}"),
                ("Comb Lag5/Lag1", f"{px.comb_lag5_lag1_ratio:.3f}"),
                ("Has Variance", str(px.has_variance)),
            ]:
                QtWidgets.QTreeWidgetItem(ac_node, [k, v])

            # Full autocorrelation values
            if px.xn_autocorrelation:
                xn_ac_node = QtWidgets.QTreeWidgetItem(ac_node, ["x[n] Lags", ""])
                for lag_str, val in sorted(px.xn_autocorrelation.items(), key=lambda x: int(x[0])):
                    marker = " (period-5)" if lag_str == "5" else ""
                    QtWidgets.QTreeWidgetItem(xn_ac_node, [f"Lag {lag_str}{marker}", f"{val:+.4f}"])
            if px.comb_autocorrelation:
                cb_ac_node = QtWidgets.QTreeWidgetItem(ac_node, ["Comb Lags", ""])
                for lag_str, val in sorted(px.comb_autocorrelation.items(), key=lambda x: int(x[0])):
                    marker = " (period-5)" if lag_str == "5" else ""
                    QtWidgets.QTreeWidgetItem(cb_ac_node, [f"Lag {lag_str}{marker}", f"{val:+.4f}"])

            dup_node = QtWidgets.QTreeWidgetItem(l2_node, ["Duplicate Fields", ""])
            for k, v in [
                ("Dup% (SAD<0.5)", f"{px.dup_field_pct:.1f}%"),
                ("Dup% (SAD<0.2)", f"{px.dup_field_pct_02:.1f}%"),
                ("Dup% (SAD<1.0)", f"{px.dup_field_pct_10:.1f}%"),
                ("Top Median SAD", f"{px.dup_field_top_median_sad:.4f}"),
                ("Bot Median SAD", f"{px.dup_field_bot_median_sad:.4f}"),
                ("AC Lag5/Lag1", f"{px.dup_field_lag5_lag1_ratio:.3f}"),
                ("Period-5", str(px.dup_field_has_period5)),
            ]:
                QtWidgets.QTreeWidgetItem(dup_node, [k, v])

            for k, v in [
                ("Frames", f"{px.total_frames:,}"),
                ("Time", f"{px.elapsed_sec:.1f}s ({px.frames_per_sec:.0f} f/s)"),
            ]:
                QtWidgets.QTreeWidgetItem(l2_node, [k, v])

        # Layer 3
        fs = result.field_swap
        if fs and not fs.error and not fs.insufficient_data:
            l3_node = QtWidgets.QTreeWidgetItem(tree, ["Layer 3: Field-Swap", ""])
            for k, v in [
                ("Total Combed", f"{fs.total_combed:,}"),
                ("Degenerate Skipped", f"{fs.degenerate_skipped:,}"),
                ("Eligible", f"{fs.eligible_combed:,}"),
                ("Tested", f"{fs.tested:,}"),
                ("Fixable", f"{fs.fixed_count:,} ({fs.fix_pct:.1f}%)"),
                ("Unfixable", f"{fs.unfixable_count:,} ({100 - fs.fix_pct:.1f}%)"),
                ("Time", f"{fs.elapsed_sec:.1f}s"),
            ]:
                QtWidgets.QTreeWidgetItem(l3_node, [k, v])

        # Classification
        cls = result.classification
        cls_node = QtWidgets.QTreeWidgetItem(tree, ["Classification", ""])
        for k, v in [
            ("Result", cls.classification.upper().replace("_", " ")),
            ("Confidence", cls.confidence),
            ("Reason", cls.reason),
            ("Film Source", cls.film_source or "-"),
            ("Video Source", cls.video_source or "-"),
            ("Film%", f"{cls.film_pct:.1f}%"),
            ("Video%", f"{cls.video_pct:.1f}%"),
            ("Layers Run",
             f"{'1' if result.layer1_ran else '-'}"
             f"{'2' if result.layer2_ran else '-'}"
             f"{'3' if result.layer3_ran else '-'}"),
            ("Total Time", f"{result.total_elapsed_sec:.1f}s"),
        ]:
            QtWidgets.QTreeWidgetItem(cls_node, [k, v])

        tree.expandAll()

    # ── Export ─────────────────────────────────────────────────────────

    def _browse_export_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select Export Directory"
        )
        if d:
            self.export_path_edit.setText(d)

    def _get_export_dir(self) -> str | None:
        d = self.export_path_edit.text().strip()
        if not d:
            QtWidgets.QMessageBox.warning(
                self, "Export", "Please select an export directory first."
            )
            return None
        os.makedirs(d, exist_ok=True)
        return d

    def _get_selected_result(self) -> tuple[str, AnalysisResult] | None:
        items = self.file_list.selectedItems()
        if not items:
            QtWidgets.QMessageBox.warning(
                self, "Export", "Please select a file first."
            )
            return None
        fp = items[0].data(QtCore.Qt.ItemDataRole.UserRole)
        result = self.results.get(fp)
        if not result:
            QtWidgets.QMessageBox.warning(
                self, "Export", "Selected file has not been analyzed yet."
            )
            return None
        return fp, result

    def _safe_filename(self, filepath: str) -> str:
        name = os.path.splitext(os.path.basename(filepath))[0]
        for ch in " ()[]!@#$%^&=+{}':;":
            name = name.replace(ch, "_")
        while "__" in name:
            name = name.replace("__", "_")
        return name.strip("_")

    def _export(self, mode: str):
        export_dir = self._get_export_dir()
        if not export_dir:
            return

        if mode == "all_full":
            count = 0
            for fp, result in self.results.items():
                name = self._safe_filename(fp)
                out = os.path.join(export_dir, f"{name}_full.json")
                with open(out, "w") as f:
                    f.write(result.to_json(include_per_frame=True))
                count += 1
            self.status_label.setText(f"Exported {count} files to {export_dir}")
            return

        sel = self._get_selected_result()
        if not sel:
            return
        fp, result = sel
        name = self._safe_filename(fp)

        if mode == "full":
            out = os.path.join(export_dir, f"{name}_full.json")
            with open(out, "w") as f:
                f.write(result.to_json(include_per_frame=True))
        elif mode == "summary":
            out = os.path.join(export_dir, f"{name}_summary.json")
            with open(out, "w") as f:
                f.write(result.to_json(include_per_frame=False))
        elif mode == "log":
            out = os.path.join(export_dir, f"{name}_summary.log")
            with open(out, "w") as f:
                f.write(result.summary_text())

        self.status_label.setText(f"Exported: {out}")

    # ── Cleanup ────────────────────────────────────────────────────────

    def shutdown(self):
        if self._worker:
            self._worker.stop()
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(3000)
