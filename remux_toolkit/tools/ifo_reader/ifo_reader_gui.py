# remux_toolkit/tools/ifo_reader/ifo_reader_gui.py
from pathlib import Path
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit,
    QFileDialog, QTreeWidget, QTreeWidgetItem, QApplication, QLabel, QHeaderView
)
from PyQt6.QtCore import QThread, Qt, pyqtSlot
from .ifo_reader_core import Worker

class IfoReaderWidget(QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.worker = None
        self.thread = None
        self._init_ui()
        self._setup_worker()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        input_layout = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Select a DVD folder (e.g., one containing VIDEO_TS) or an ISO file...")
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_file)
        parse_btn = QPushButton("Read DVD Structure")
        parse_btn.clicked.connect(self.start_parsing)
        input_layout.addWidget(self.path_edit, 1)
        input_layout.addWidget(browse_btn)
        input_layout.addWidget(parse_btn)
        layout.addLayout(input_layout)

        self.results_tree = QTreeWidget()
        self.results_tree.setColumnCount(2)
        self.results_tree.setHeaderLabels(["Property", "Value"])
        self.results_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.results_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.results_tree, 1)

        action_layout = QHBoxLayout()
        self.copy_btn = QPushButton("Copy Details to Clipboard")
        self.copy_btn.clicked.connect(self._copy_details)
        self.status_label = QLabel("Ready")
        action_layout.addWidget(self.status_label, 1, Qt.AlignmentFlag.AlignLeft)
        action_layout.addWidget(self.copy_btn, 0, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(action_layout)

    def _browse_file(self):
        path = QFileDialog.getExistingDirectory(self, "Select DVD Folder", "")
        if path:
            self.path_edit.setText(path)
            self.start_parsing()

    def _setup_worker(self):
        self.thread = QThread()
        self.worker = Worker()
        self.worker.moveToThread(self.thread)
        self.worker.finished.connect(self.on_parsing_finished)
        self.thread.start()

    def start_parsing(self):
        file_path = self.path_edit.text()
        if not file_path:
            self.status_label.setText("Error: No folder/file selected.")
            return

        self.results_tree.clear()
        self.status_label.setText(f"Reading {Path(file_path).name} with lsdvd...")
        self.worker.parse_ifo(file_path)

    @pyqtSlot(dict, str)
    def on_parsing_finished(self, data, error_string):
        self.results_tree.clear()
        if error_string:
            self.status_label.setText("An error occurred.")
            error_lines = error_string.splitlines()
            parent_item = QTreeWidgetItem(self.results_tree, ["Error Report"])
            for line in error_lines:
                QTreeWidgetItem(parent_item, [line])
            self.results_tree.expandAll()
            return

        self.status_label.setText("Parsing complete.")
        self._populate_tree(data)
        self.results_tree.expandAll()

    def _populate_tree(self, data, parent_item=None):
        if parent_item is None:
            parent_item = self.results_tree.invisibleRootItem()

        if "titles" in data:
            mode = data.get('parsing_mode', 'Unknown')
            parent_item.setText(0, f"Disc ({len(data['titles'])} Titles Found via {mode})")
            for title in data['titles']:
                title_num = title.get('title_number') or title.get('ix', '?')
                props = title.get('properties', {})
                duration = props.get('length', '0s')
                chapters = props.get('chapters', '0')
                title_item = QTreeWidgetItem(parent_item, [f"Title {title_num}", f"{duration}, {chapters} chapters"])
                self._populate_tree(title, title_item)
            return

        for key, value in data.items():
            key_str = str(key).replace('_', ' ').title()
            if isinstance(value, dict) and value:
                child_item = QTreeWidgetItem(parent_item, [key_str])
                self._populate_tree(value, child_item)
            elif isinstance(value, list) and value:
                for item in value:
                     self._populate_tree(item, parent_item)
            else:
                QTreeWidgetItem(parent_item, [key_str, str(value)])

    def _copy_details(self):
        text_report = self._get_tree_text(self.results_tree.invisibleRootItem())
        QApplication.clipboard().setText(text_report)
        self.status_label.setText("Details copied to clipboard.")

    def _get_tree_text(self, item, indent=0):
        text = ""
        for i in range(item.childCount()):
            child = item.child(i)
            prop = child.text(0)
            val = child.text(1)
            text += "  " * indent
            if val:
                text += f"{prop}: {val}\n"
            else:
                text += f"{prop}:\n"
            text += self._get_tree_text(child, indent + 1)
        return text

    def shutdown(self):
        if self.thread and self.thread.isRunning():
            self.thread.quit()
            self.thread.wait(2000)
