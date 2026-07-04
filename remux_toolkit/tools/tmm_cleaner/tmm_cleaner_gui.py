# remux_toolkit/tools/tmm_cleaner/tmm_cleaner_gui.py

import os
import datetime

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QWidget, QSplitter, QListWidget, QListWidgetItem, QTreeWidget,
    QTreeWidgetItem, QPushButton, QLabel, QVBoxLayout, QHBoxLayout,
    QCheckBox, QMessageBox, QFileDialog, QPlainTextEdit, QGroupBox,
    QAbstractItemView,
)

from . import tmm_cleaner_core as core
from .tmm_cleaner_config import DEFAULTS


class FolderDropList(QListWidget):
    """Folder list (left pane) that accepts directory drops."""
    foldersDropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

    def _dirs_from_event(self, event):
        if not event.mimeData().hasUrls():
            return []
        return [u.toLocalFile() for u in event.mimeData().urls()
                if u.toLocalFile() and os.path.isdir(u.toLocalFile())]

    def dragEnterEvent(self, event):
        if self._dirs_from_event(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._dirs_from_event(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        dirs = self._dirs_from_event(event)
        if dirs:
            self.foldersDropped.emit(dirs)
            event.acceptProposedAction()
        else:
            event.ignore()


class TMMCleanerWidget(QWidget):
    UserRole = Qt.ItemDataRole.UserRole

    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'tmm_cleaner'
        self.setAcceptDrops(True)            # dropping anywhere works too

        self.folders = {}        # realpath -> {"entries": [...]}
        self.items = {}          # realpath -> QListWidgetItem
        self.current_folder = None

        self._build_ui()
        self._load_settings()
        self.log("TMM Cleaner started. Deletion is permanent (no trash).")

    # ---- UI construction -------------------------------------------------- #
    def _build_ui(self):
        root = QVBoxLayout(self)

        title = QLabel("Drag TinyMediaManager folders below, scan, "
                       "uncheck anything you want to keep, then delete.")
        f = title.font(); f.setBold(True); title.setFont(f)
        root.addWidget(title)

        # options -------------------------------------------------------
        opts = QGroupBox("What to remove (everything else — media, .txt, "
                         "no-extension files — is always kept)")
        ol = QHBoxLayout(opts)
        self.opt_images = QCheckBox("Artwork images")
        self.opt_nfo = QCheckBox(".nfo files")
        self.opt_subs = QCheckBox("Subtitles")
        self.opt_recurse = QCheckBox("Include sub-folders (TV seasons)")
        self.opt_rmdirs = QCheckBox("Remove emptied folders")
        self.opt_images.setChecked(True)
        self.opt_nfo.setChecked(True)
        self.opt_subs.setChecked(False)
        self.opt_recurse.setChecked(True)
        self.opt_rmdirs.setChecked(True)
        for cb in (self.opt_images, self.opt_nfo, self.opt_subs,
                   self.opt_recurse, self.opt_rmdirs):
            ol.addWidget(cb)
            cb.stateChanged.connect(self.rescan_all)
        ol.addStretch(1)
        root.addWidget(opts)

        # split panes ---------------------------------------------------
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # left: folders
        left = QWidget(); ll = QVBoxLayout(left); ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("Dropped folders"))
        self.folder_list = FolderDropList()
        self.folder_list.foldersDropped.connect(self.add_folders)
        self.folder_list.currentItemChanged.connect(self.on_folder_selected)
        ll.addWidget(self.folder_list, 1)
        lb = QHBoxLayout()
        b_add = QPushButton("Add folder…"); b_add.clicked.connect(self.browse_folder)
        b_rm = QPushButton("Remove"); b_rm.clicked.connect(self.remove_selected_folders)
        b_clear = QPushButton("Clear"); b_clear.clicked.connect(self.clear_all)
        for b in (b_add, b_rm, b_clear):
            lb.addWidget(b)
        ll.addLayout(lb)

        # right: files to delete
        right = QWidget(); rl = QVBoxLayout(right); rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel("Files flagged for deletion (uncheck to keep)"))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File", "Type", "Size"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setColumnWidth(0, 380)
        self.tree.itemChanged.connect(self.on_item_changed)
        rl.addWidget(self.tree, 1)
        rb = QHBoxLayout()
        b_check = QPushButton("Check all"); b_check.clicked.connect(lambda: self.set_all_checks(True))
        b_uncheck = QPushButton("Uncheck all"); b_uncheck.clicked.connect(lambda: self.set_all_checks(False))
        b_sel_keep = QPushButton("Keep selected"); b_sel_keep.clicked.connect(lambda: self.set_selected_checks(False))
        rb.addWidget(b_check); rb.addWidget(b_uncheck); rb.addWidget(b_sel_keep); rb.addStretch(1)
        rl.addLayout(rb)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 720])
        root.addWidget(splitter, 1)

        # action row ----------------------------------------------------
        action = QHBoxLayout()
        self.btn_scan = QPushButton("🔍  Scan")
        self.btn_scan.clicked.connect(self.rescan_all)
        self.btn_delete = QPushButton("🗑  Delete checked — permanent")
        self.btn_delete.setStyleSheet(
            "QPushButton{background:#c0392b;color:white;font-weight:bold;padding:6px;}"
            "QPushButton:hover{background:#e74c3c;}")
        self.btn_delete.clicked.connect(self.do_delete)
        self.summary = QLabel("Nothing scanned yet.")
        action.addWidget(self.btn_scan)
        action.addWidget(self.btn_delete)
        action.addStretch(1)
        action.addWidget(self.summary)
        root.addLayout(action)

        # log -----------------------------------------------------------
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFixedHeight(120)
        self.log_view.setFont(QFont("monospace"))
        root.addWidget(self.log_view)

        # status line (stands in for the old QMainWindow status bar)
        self.status_lbl = QLabel("Ready — drag a folder in to begin.")
        root.addWidget(self.status_lbl)

    # ---- settings persistence --------------------------------------------- #
    def _load_settings(self):
        settings = self.app_manager.load_config(self.tool_name, DEFAULTS)
        self.opt_images.setChecked(settings.get("del_images", DEFAULTS["del_images"]))
        self.opt_nfo.setChecked(settings.get("del_nfo", DEFAULTS["del_nfo"]))
        self.opt_subs.setChecked(settings.get("del_subs", DEFAULTS["del_subs"]))
        self.opt_recurse.setChecked(settings.get("recurse", DEFAULTS["recurse"]))
        self.opt_rmdirs.setChecked(settings.get("remove_empty_dirs", DEFAULTS["remove_empty_dirs"]))

    def save_settings(self):
        self.app_manager.save_config(self.tool_name, {
            "del_images": self.opt_images.isChecked(),
            "del_nfo": self.opt_nfo.isChecked(),
            "del_subs": self.opt_subs.isChecked(),
            "recurse": self.opt_recurse.isChecked(),
            "remove_empty_dirs": self.opt_rmdirs.isChecked(),
        })

    # ---- widget-level drag & drop ------------------------------------------ #
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        dirs = [u.toLocalFile() for u in event.mimeData().urls()
                if u.toLocalFile() and os.path.isdir(u.toLocalFile())]
        if dirs:
            self.add_folders(dirs)
            event.acceptProposedAction()

    # ---- helpers ---------------------------------------------------------- #
    def log(self, msg):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{ts}] {msg}")

    def set_status(self, msg):
        self.status_lbl.setText(msg)

    def opt_values(self):
        return (self.opt_recurse.isChecked(), self.opt_images.isChecked(),
                self.opt_nfo.isChecked(), self.opt_subs.isChecked())

    # ---- folder management ------------------------------------------------ #
    def browse_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Choose a media folder")
        if path:
            self.add_folders([path])

    def add_folders(self, paths):
        added = []
        for p in paths:
            rp = os.path.realpath(p)
            if not os.path.isdir(rp) or rp in self.folders:
                continue
            self.folders[rp] = {"entries": []}
            item = QListWidgetItem()
            item.setData(self.UserRole, rp)
            self.items[rp] = item
            self.folder_list.addItem(item)
            added.append(rp)
        for rp in added:
            self.scan_one(rp)
            self.update_left_item(rp)
        if added:
            self.folder_list.setCurrentItem(self.items[added[-1]])
            self.log(f"Added {len(added)} folder(s).")
            self.update_summary()

    def remove_selected_folders(self):
        for item in self.folder_list.selectedItems():
            rp = item.data(self.UserRole)
            self.folders.pop(rp, None)
            self.items.pop(rp, None)
            self.folder_list.takeItem(self.folder_list.row(item))
            if rp == self.current_folder:
                self.current_folder = None
                self._clear_tree()
        self.update_summary()

    def clear_all(self):
        self.folders.clear()
        self.items.clear()
        self.folder_list.clear()
        self.current_folder = None
        self._clear_tree()
        self.update_summary()
        self.log("Cleared all folders.")

    # ---- scanning --------------------------------------------------------- #
    def scan_one(self, rp):
        recurse, di, dn, ds = self.opt_values()
        try:
            entries = core.scan_folder(rp, recurse, di, dn, ds)
        except OSError as exc:
            entries = []
            self.log(f"! Could not scan {rp}: {exc}")
        self.folders[rp]["entries"] = entries

    def rescan_all(self):
        if not self.folders:
            return
        for rp in self.folders:
            self.scan_one(rp)
            self.update_left_item(rp)
        if self.current_folder:
            self.populate_tree(self.current_folder)
        self.update_summary()
        total = sum(len(d["entries"]) for d in self.folders.values())
        self.log(f"Scan complete — {total} file(s) flagged across "
                 f"{len(self.folders)} folder(s).")

    def update_left_item(self, rp):
        item = self.items.get(rp)
        if not item:
            return
        entries = self.folders[rp]["entries"]
        n = len(entries)
        if n == 0:
            item.setText(f"{os.path.basename(rp) or rp}\n  (nothing to clean)")
        else:
            size = core.human_size(sum(e["size"] for e in entries))
            item.setText(f"{os.path.basename(rp) or rp}\n  {n} file(s), {size}")
        item.setToolTip(rp)

    # ---- right pane ------------------------------------------------------- #
    def _clear_tree(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        self.tree.blockSignals(False)

    def on_folder_selected(self, current, _previous):
        self.current_folder = current.data(self.UserRole) if current else None
        if self.current_folder:
            self.populate_tree(self.current_folder)
        else:
            self._clear_tree()

    def populate_tree(self, rp):
        self.tree.blockSignals(True)
        self.tree.clear()
        for idx, e in enumerate(self.folders[rp]["entries"]):
            it = QTreeWidgetItem([e["rel"], core.REASON_LABELS.get(e["reason"], e["reason"]),
                                  core.human_size(e["size"])])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(0, Qt.CheckState.Checked if e["checked"]
                             else Qt.CheckState.Unchecked)
            it.setData(0, self.UserRole, idx)
            it.setToolTip(0, e["path"])
            self.tree.addTopLevelItem(it)
        self.tree.blockSignals(False)

    def on_item_changed(self, item, column):
        if column != 0 or not self.current_folder:
            return
        idx = item.data(0, self.UserRole)
        checked = item.checkState(0) == Qt.CheckState.Checked
        self.folders[self.current_folder]["entries"][idx]["checked"] = checked
        self.update_left_item(self.current_folder)
        self.update_summary()

    def set_all_checks(self, checked):
        if not self.current_folder:
            return
        for e in self.folders[self.current_folder]["entries"]:
            e["checked"] = checked
        self.populate_tree(self.current_folder)
        self.update_left_item(self.current_folder)
        self.update_summary()

    def set_selected_checks(self, checked):
        if not self.current_folder:
            return
        for item in self.tree.selectedItems():
            idx = item.data(0, self.UserRole)
            self.folders[self.current_folder]["entries"][idx]["checked"] = checked
        self.populate_tree(self.current_folder)
        self.update_left_item(self.current_folder)
        self.update_summary()

    # ---- summary ---------------------------------------------------------- #
    def checked_entries(self):
        out = []
        for rp, data in self.folders.items():
            for e in data["entries"]:
                if e["checked"]:
                    out.append((rp, e))
        return out

    def update_summary(self):
        checked = self.checked_entries()
        n = len(checked)
        size = core.human_size(sum(e["size"] for _rp, e in checked))
        self.summary.setText(f"{n} file(s) selected · {size}")
        self.btn_delete.setEnabled(n > 0)

    # ---- deletion --------------------------------------------------------- #
    def do_delete(self):
        targets = self.checked_entries()
        if not targets:
            QMessageBox.information(self, "Nothing to delete",
                                    "No files are checked for deletion.")
            return
        total = sum(e["size"] for _rp, e in targets)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Confirm permanent deletion")
        box.setText(f"Permanently delete {len(targets)} file(s)?")
        box.setInformativeText(
            f"This frees {core.human_size(total)} and CANNOT be undone — "
            "files are removed directly, not sent to the trash.")
        box.setStandardButtons(QMessageBox.StandardButton.Yes |
                               QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            self.log("Deletion cancelled.")
            return

        deleted, freed, errors = 0, 0, []
        for _rp, e in targets:
            try:
                os.remove(e["path"])
                deleted += 1
                freed += e["size"]
                self.log(f"deleted  {e['path']}")
            except OSError as exc:
                errors.append((e["path"], exc))
                self.log(f"! FAILED  {e['path']}  ({exc})")

        removed_dirs = 0
        if self.opt_rmdirs.isChecked():
            for rp in self.folders:
                removed_dirs += core.remove_empty_dirs(rp)
            if removed_dirs:
                self.log(f"removed {removed_dirs} emptied folder(s).")

        self.rescan_all()                 # refresh so deleted files disappear

        msg = (f"Deleted {deleted} file(s), freed {core.human_size(freed)}.")
        if removed_dirs:
            msg += f" Removed {removed_dirs} empty folder(s)."
        if errors:
            msg += f"\n{len(errors)} file(s) could not be deleted (see log)."
        self.log(msg.replace("\n", " "))
        self.set_status(msg.replace("\n", " "))
        (QMessageBox.warning if errors else QMessageBox.information)(
            self, "Done", msg)
