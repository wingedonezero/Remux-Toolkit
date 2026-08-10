# remux_toolkit/tools/mkv_lossless_keeper/mkv_lossless_keeper_gui.py
"""MKV Lossless Keeper — GUI widget for the Remux-Toolkit."""

import os

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QProgressBar, QPushButton, QSplitter, QTextEdit,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import mkv_lossless_keeper_core as core
from .mkv_lossless_keeper_config import DEFAULTS


class DropTree(QTreeWidget):
    files_dropped = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setHeaderLabels(["File / Track", "Codec", "Language", "Action"])
        self.setColumnWidth(0, 420)
        self.setColumnWidth(1, 220)
        self.setColumnWidth(2, 90)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = []
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if not p:
                continue
            if os.path.isdir(p):
                for root, _dirs, files in os.walk(p):
                    for f in sorted(files):
                        if f.lower().endswith(".mkv"):
                            paths.append(os.path.join(root, f))
            elif p.lower().endswith(".mkv"):
                paths.append(p)
        if paths:
            self.files_dropped.emit(paths)
        e.acceptProposedAction()


class MKVLosslessKeeperWidget(QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'mkv_lossless_keeper'

        self.files: list[str] = []            # ordered, deduped
        self.analyses: dict[str, core.FileAnalysis] = {}
        self.worker = None

        layout = QVBoxLayout(self)

        hint = QLabel("Drag && drop MKV files or folders below (folders are scanned recursively).")
        layout.addWidget(hint)

        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter, 1)

        self.tree = DropTree()
        self.tree.files_dropped.connect(self.add_files)
        splitter.addWidget(self.tree)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(QFont("Monospace", 9))
        splitter.addWidget(self.log_box)
        splitter.setSizes([460, 200])

        # --- filter groups (audio | subtitles) ---
        filters_row = QHBoxLayout()

        # Audio group: codec checkboxes + language keep-list
        grp = QGroupBox("Audio — remove these track types")
        grp_v = QVBoxLayout(grp)
        cols = QHBoxLayout()
        col1, col2 = QVBoxLayout(), QVBoxLayout()
        cols.addLayout(col1)
        cols.addLayout(col2)
        grp_v.addLayout(cols)
        self.cat_boxes: dict[str, QCheckBox] = {}
        for idx, (key, (label, default)) in enumerate(core.REMOVABLE_CATEGORIES.items()):
            cb = QCheckBox(label)
            cb.setChecked(default)
            cb.stateChanged.connect(self.refresh_tree_actions)
            self.cat_boxes[key] = cb
            (col1 if idx < 4 else col2).addWidget(cb)
        lossless = QLabel(core.LOSSLESS_LABEL)
        lossless.setStyleSheet("color: green;")
        col2.addWidget(lossless)
        audio_lang_row = QHBoxLayout()
        audio_lang_row.addWidget(QLabel("Keep only languages:"))
        self.audio_lang_edit = QLineEdit()
        self.audio_lang_edit.setPlaceholderText("blank = keep all  •  e.g. eng, jpn")
        self.audio_lang_edit.setToolTip(
            "Comma/space-separated language codes (eng, jpn, spa… or en, ja, es).\n"
            "Leave blank to keep all languages. Applies to ALL audio, lossless included.")
        self.audio_lang_edit.textChanged.connect(self.refresh_tree_actions)
        audio_lang_row.addWidget(self.audio_lang_edit, 1)
        grp_v.addLayout(audio_lang_row)
        filters_row.addWidget(grp, 3)

        # Subtitle group: type checkboxes + language keep-list
        sub_grp = QGroupBox("Subtitles — remove these types")
        sub_v = QVBoxLayout(sub_grp)
        sub_cols = QHBoxLayout()
        scol1, scol2 = QVBoxLayout(), QVBoxLayout()
        sub_cols.addLayout(scol1)
        sub_cols.addLayout(scol2)
        sub_v.addLayout(sub_cols)
        self.sub_boxes: dict[str, QCheckBox] = {}
        for idx, (key, (label, default)) in enumerate(core.SUB_CATEGORIES.items()):
            cb = QCheckBox(label)
            cb.setChecked(default)
            cb.stateChanged.connect(self.refresh_tree_actions)
            self.sub_boxes[key] = cb
            (scol1 if idx < 2 else scol2).addWidget(cb)
        sub_note = QLabel("Other subtitle types are only affected by the language filter.")
        sub_note.setStyleSheet("color: gray; font-size: 11px;")
        sub_v.addWidget(sub_note)
        sub_lang_row = QHBoxLayout()
        sub_lang_row.addWidget(QLabel("Keep only languages:"))
        self.sub_lang_edit = QLineEdit()
        self.sub_lang_edit.setPlaceholderText("blank = keep all  •  e.g. eng")
        self.sub_lang_edit.setToolTip(
            "Comma/space-separated language codes (eng, jpn, spa… or en, ja, es).\n"
            "Leave blank to keep all subtitle languages.")
        self.sub_lang_edit.textChanged.connect(self.refresh_tree_actions)
        sub_lang_row.addWidget(self.sub_lang_edit, 1)
        sub_v.addLayout(sub_lang_row)
        self.keep_und_chk = QCheckBox("Language filters keep untagged (und) tracks")
        self.keep_und_chk.setChecked(True)
        self.keep_und_chk.setToolTip(
            "When a language keep-list is set, tracks with no language tag (und) are\n"
            "kept anyway. Uncheck to remove untagged tracks too. Applies to audio and subs.")
        self.keep_und_chk.stateChanged.connect(self.refresh_tree_actions)
        sub_v.addWidget(self.keep_und_chk)
        filters_row.addWidget(sub_grp, 2)

        layout.addLayout(filters_row)

        # --- buttons ---
        btns = QHBoxLayout()
        self.btn_add = QPushButton("Add Files…")
        self.btn_add.clicked.connect(self.browse_files)
        self.btn_analyze = QPushButton("Analyze")
        self.btn_analyze.clicked.connect(self.start_analyze)
        self.btn_process = QPushButton("Process (remove && verify)")
        self.btn_process.clicked.connect(self.start_process)
        self.btn_process.setEnabled(False)
        self.btn_clear = QPushButton("Clear List")
        self.btn_clear.clicked.connect(self.clear_all)
        for b in (self.btn_add, self.btn_analyze, self.btn_process, self.btn_clear):
            btns.addWidget(b)
        btns.addStretch(1)
        layout.addLayout(btns)

        self.file_bar_label = QLabel("Current file:")
        self.file_bar = QProgressBar()
        self.file_bar.setRange(0, 100)
        file_row = QHBoxLayout()
        file_row.addWidget(self.file_bar_label)
        file_row.addWidget(self.file_bar, 1)
        self.file_row_widget = QWidget()
        self.file_row_widget.setLayout(file_row)
        self.file_row_widget.setVisible(False)
        layout.addWidget(self.file_row_widget)

        self.progress = QProgressBar()
        self.progress.setFormat("Overall: %v / %m files")
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.status = QLabel("Ready. Add files, Analyze, then Process.")
        layout.addWidget(self.status)

        self._load_settings()

    # -------------------------------------------------------------- settings

    def _load_settings(self):
        settings = self.app_manager.load_config(self.tool_name, DEFAULTS)
        saved = settings.get("remove_categories", DEFAULTS["remove_categories"])
        for key, cb in self.cat_boxes.items():
            default = DEFAULTS["remove_categories"].get(key, False)
            cb.setChecked(bool(saved.get(key, default)))
        saved_subs = settings.get("sub_remove_categories", DEFAULTS["sub_remove_categories"])
        for key, cb in self.sub_boxes.items():
            default = DEFAULTS["sub_remove_categories"].get(key, False)
            cb.setChecked(bool(saved_subs.get(key, default)))
        self.audio_lang_edit.setText(settings.get("audio_keep_langs", DEFAULTS["audio_keep_langs"]))
        self.sub_lang_edit.setText(settings.get("sub_keep_langs", DEFAULTS["sub_keep_langs"]))
        self.keep_und_chk.setChecked(bool(settings.get("keep_und", DEFAULTS["keep_und"])))

    def save_settings(self):
        self.app_manager.save_config(self.tool_name, {
            "remove_categories": {k: cb.isChecked() for k, cb in self.cat_boxes.items()},
            "audio_keep_langs": self.audio_lang_edit.text(),
            "sub_remove_categories": {k: cb.isChecked() for k, cb in self.sub_boxes.items()},
            "sub_keep_langs": self.sub_lang_edit.text(),
            "keep_und": self.keep_und_chk.isChecked(),
        })

    def shutdown(self):
        if self.worker and self.worker.isRunning():
            self.worker.abort()
            if not self.worker.wait(5000):
                self.worker.terminate()
                self.worker.wait()
        self.worker = None

    # ------------------------------------------------------------------ files

    def browse_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Select MKV files", "", "MKV files (*.mkv)")
        if paths:
            self.add_files(paths)

    def add_files(self, paths):
        added = 0
        for p in paths:
            p = os.path.abspath(p)
            if p not in self.files:
                self.files.append(p)
                added += 1
                item = QTreeWidgetItem([os.path.basename(p), "", "", "not analyzed"])
                item.setToolTip(0, p)
                self.tree.addTopLevelItem(item)
        self.status.setText(f"Added {added} file(s), {len(self.files)} total. Click Analyze.")
        self.btn_process.setEnabled(False)

    def clear_all(self):
        self.files.clear()
        self.analyses.clear()
        self.tree.clear()
        self.log_box.clear()
        self.btn_process.setEnabled(False)
        self.status.setText("Cleared.")

    # -------------------------------------------------------------- analyzing

    def current_filters(self) -> core.FilterSettings:
        return core.FilterSettings(
            audio_remove_cats={k for k, cb in self.cat_boxes.items() if cb.isChecked()},
            audio_keep_langs=core.parse_lang_list(self.audio_lang_edit.text()),
            sub_remove_cats={k for k, cb in self.sub_boxes.items() if cb.isChecked()},
            sub_keep_langs=core.parse_lang_list(self.sub_lang_edit.text()),
            keep_und=self.keep_und_chk.isChecked(),
        )

    def start_analyze(self):
        if not self.files:
            QMessageBox.information(self, "Nothing to do", "Add or drop some MKV files first.")
            return
        if self.worker and self.worker.isRunning():
            return
        self.set_busy(True, "Analyzing…")
        self.analyses.clear()
        self.worker = core.AnalyzeWorker(list(self.files))
        self.worker.file_done.connect(self.on_analyzed)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished_all.connect(self.on_analyze_done)
        self.worker.start()

    def on_analyzed(self, fa: core.FileAnalysis):
        self.analyses[fa.path] = fa
        self.append_log(
            f"ANALYZED {os.path.basename(fa.path)}: "
            + (f"ERROR {fa.error}" if fa.error else
               f"{fa.n_video} video, {len(fa.audio)} audio, {len(fa.subs)} subs")
        )

    def on_analyze_done(self):
        self.refresh_tree_actions()
        self.set_busy(False, "Analysis complete. Review the plan, then Process.")
        self.btn_process.setEnabled(True)

    def refresh_tree_actions(self):
        """Rebuild the tree to show per-track keep/remove decisions."""
        filters = self.current_filters()
        self.tree.clear()
        for path in self.files:
            fa = self.analyses.get(path)
            if fa is None:
                item = QTreeWidgetItem([os.path.basename(path), "", "", "not analyzed"])
                item.setToolTip(0, path)
                self.tree.addTopLevelItem(item)
                continue
            plan = core.plan_for_file(fa, filters)
            n_remove = len(plan.remove_audio) + len(plan.remove_subs)
            action = plan.skip if plan.skip else f"will remove {n_remove} track(s)"
            top = QTreeWidgetItem([os.path.basename(path), "", "", action])
            top.setToolTip(0, path)
            self.tree.addTopLevelItem(top)
            if not (fa.error or (plan.skip and plan.skip.startswith("error"))):
                remove_ids = {t.tid for t in plan.remove_audio} | {t.tid for t in plan.remove_subs}
                effective = plan.skip is None
                for kind, tracks in (("", fa.audio), ("Sub: ", fa.subs)):
                    for t in tracks:
                        will_remove = effective and t.tid in remove_ids
                        verdict = "REMOVE" if will_remove else "keep"
                        child = QTreeWidgetItem(
                            ["", kind + t.codec + (f"  [{t.name}]" if t.name else ""),
                             t.language, verdict])
                        if will_remove:
                            child.setForeground(3, Qt.GlobalColor.red)
                        else:
                            child.setForeground(3, Qt.GlobalColor.darkGreen)
                        top.addChild(child)
            top.setExpanded(True)

    # ------------------------------------------------------------- processing

    def start_process(self):
        if not self.analyses:
            QMessageBox.information(self, "Analyze first", "Run Analyze before processing.")
            return
        filters = self.current_filters()
        if filters.is_noop():
            QMessageBox.information(self, "Nothing selected",
                                    "Check at least one track type or enter a language filter.")
            return
        if self.worker and self.worker.isRunning():
            return
        self.set_busy(True, "Processing…")
        ordered = [self.analyses[p] for p in self.files if p in self.analyses]
        self.worker = core.ProcessWorker(ordered, filters)
        self.worker.log.connect(self.append_log)
        self.worker.progress.connect(self.on_progress)
        self.worker.file_progress.connect(self.on_file_progress)
        self.worker.finished_all.connect(self.on_process_done)
        self.file_row_widget.setVisible(True)
        self.worker.start()

    def on_process_done(self, results):
        self.set_busy(False, "Done.")
        ok = [r for r in results if r[1] == "success"]
        skipped = [r for r in results if r[1] == "skipped"]
        failed = [r for r in results if r[1] in ("error", "verify-failed")]

        self.append_log("")
        self.append_log("=" * 70)
        self.append_log(f"SUMMARY: {len(ok)} succeeded, {len(skipped)} skipped, {len(failed)} failed")
        for path, _s, detail in ok:
            self.append_log(f"  OK      {os.path.basename(path)}: {detail}")
        for path, _s, detail in skipped:
            self.append_log(f"  SKIPPED {os.path.basename(path)}: {detail}")
        for path, _s, detail in failed:
            self.append_log(f"  FAILED  {os.path.basename(path)}: {detail}")
        self.append_log("=" * 70)

        msg = f"Succeeded: {len(ok)}\nSkipped: {len(skipped)}\nFailed: {len(failed)}"
        if failed:
            QMessageBox.warning(self, "Finished with problems",
                                msg + "\n\nSee the log for details.")
        else:
            QMessageBox.information(self, "Finished", msg + "\n\nAll outputs verified.")
        self.status.setText(f"Done — {len(ok)} ok, {len(skipped)} skipped, {len(failed)} failed.")

    # ---------------------------------------------------------------- helpers

    def on_progress(self, done, total):
        self.progress.setMaximum(total)
        self.progress.setValue(done)

    def on_file_progress(self, percent, name):
        self.file_bar.setValue(percent)
        self.file_bar_label.setText(f"Current file: {name}")
        self.status.setText(f"Processing {name} — {percent}%")

    def set_busy(self, busy: bool, text: str):
        self.progress.setVisible(busy)
        self.progress.setValue(0)
        if not busy:
            self.file_row_widget.setVisible(False)
        self.file_bar.setValue(0)
        self.file_bar_label.setText("Current file:")
        for b in (self.btn_add, self.btn_analyze, self.btn_process, self.btn_clear):
            b.setEnabled(not busy)
        self.status.setText(text)

    def append_log(self, line: str):
        self.log_box.append(line)
