# remux_toolkit/tools/media_info/media_info_gui.py

from PyQt6 import QtWidgets, QtCore, QtGui
from .media_info_core import InfoWorker
from ..delay_inspector.delay_inspector_core import collect_video_paths # Reusing this helper
import os

class MediaInfoWidget(QtWidgets.QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'media_info'
        self.results = {}
        self.threadpool = QtCore.QThreadPool.globalInstance()
        self.setAcceptDrops(True)
        self._init_ui()

    def _init_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)

        # Left side (File List)
        left_widget = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_widget)
        self.file_list = QtWidgets.QListWidget()
        self.file_list.itemSelectionChanged.connect(self._on_selection_changed)

        button_layout = QtWidgets.QHBoxLayout()
        add_files_btn = QtWidgets.QPushButton("Add Files...")
        add_files_btn.clicked.connect(self._add_files)
        analyze_btn = QtWidgets.QPushButton("Analyze")
        analyze_btn.clicked.connect(self._analyze_selected)
        clear_btn = QtWidgets.QPushButton("Clear")
        clear_btn.clicked.connect(self._clear_list)

        button_layout.addWidget(add_files_btn)
        button_layout.addWidget(analyze_btn)
        button_layout.addWidget(clear_btn)

        left_layout.addWidget(self.file_list)
        left_layout.addLayout(button_layout)
        splitter.addWidget(left_widget)

        # Right side (Info Tree)
        self.info_tree = QtWidgets.QTreeWidget()
        self.info_tree.setHeaderLabels(["Property", "Value"])
        self.info_tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.info_tree.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self.info_tree)

        splitter.setSizes([300, 700])
        layout.addWidget(splitter)

    def _add_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Select Media Files", "", "All Files (*.*)")
        if files:
            self.add_paths_to_list(files)

    def add_paths_to_list(self, paths: list):
        current_files = [self.file_list.item(i).text() for i in range(self.file_list.count())]
        for path in paths:
            if path not in current_files:
                self.file_list.addItem(path)

    def _analyze_selected(self):
        items = self.file_list.selectedItems()
        if not items:
            # If nothing is selected, analyze all items in the list
            items = [self.file_list.item(i) for i in range(self.file_list.count())]

        for item in items:
            path = item.text()
            item.setForeground(QtGui.QColor('orange')) # Mark as "in progress"
            worker = InfoWorker(path)
            worker.signals.finished.connect(self._on_analysis_finished)
            worker.signals.error.connect(self._on_analysis_error)
            self.threadpool.start(worker)

    def _on_analysis_finished(self, path: str, data: dict):
        self.results[path] = data
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item.text() == path:
                item.setForeground(QtGui.QColor('lightgreen')) # Mark as "done"
                if item.isSelected():
                    self._display_info(path)
                break

    def _on_analysis_error(self, path: str, error_msg: str):
        self.results[path] = {"Error": error_msg}
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item.text() == path:
                item.setForeground(QtGui.QColor('red')) # Mark as "error"
                if item.isSelected():
                    self._display_info(path)
                break

    def _on_selection_changed(self):
        items = self.file_list.selectedItems()
        if items:
            self._display_info(items[0].text())

    def _display_info(self, path: str):
        self.info_tree.clear()
        data = self.results.get(path)
        if not data:
            return

        for category, content in data.items():
            category_item = QtWidgets.QTreeWidgetItem(self.info_tree, [category])
            if isinstance(content, list): # Streams
                for i, stream in enumerate(content):
                    stream_type = stream.get('codec_type', 'Unknown').capitalize()
                    stream_index = stream.get('index', i)
                    stream_item = QtWidgets.QTreeWidgetItem(category_item, [f"Stream #{stream_index} ({stream_type})"])
                    self._populate_tree(stream, stream_item)
            elif isinstance(content, dict): # Container
                 self._populate_tree(content, category_item)
            else: # Simple error message
                QtWidgets.QTreeWidgetItem(category_item, ["Error", str(content)])
        self.info_tree.expandAll()

    def _populate_tree(self, data: dict, parent_item):
        for key, value in sorted(data.items()):
            if isinstance(value, dict):
                child_item = QtWidgets.QTreeWidgetItem(parent_item, [str(key)])
                self._populate_tree(value, child_item)
            else:
                QtWidgets.QTreeWidgetItem(parent_item, [str(key), str(value)])

    def _clear_list(self):
        self.file_list.clear()
        self.info_tree.clear()
        self.results.clear()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        paths = collect_video_paths(urls)
        self.add_paths_to_list(paths)

    def shutdown(self):
        self.threadpool.waitForDone()

    def save_settings(self):
        pass # No settings to save yet
