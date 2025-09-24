# remux_toolkit/tools/ffmpeg_dvd_remuxer/gui/title_table.py
from pathlib import Path
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QTableWidget, QAbstractItemView

class DropTable(QTableWidget):
    pathsDropped = pyqtSignal(list)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop) # No internal reordering

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            paths = [str(Path(url.toLocalFile())) for url in event.mimeData().urls() if url.isLocalFile()]
            if paths:
                self.pathsDropped.emit(paths)
                event.acceptProposedAction()
