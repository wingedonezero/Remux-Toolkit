# remux_toolkit/tools/ffmpeg_dvd_remuxer/gui/queue_tree.py
from pathlib import Path
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QTreeWidget, QAbstractItemView

class DropTree(QTreeWidget):
    pathsDropped = pyqtSignal(list)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAcceptDrops(True)

        # --- THIS IS THE DEFINITIVE FIX ---
        # Instead of NoDragDrop, we set the mode to DropOnly.
        # This allows external files to be dropped onto the widget
        # while still preventing internal items from being dragged/reordered.
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        # --- END FIX ---

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            paths = [str(Path(url.toLocalFile())) for url in event.mimeData().urls() if url.isLocalFile()]
            if paths:
                self.pathsDropped.emit(paths)
                event.acceptProposedAction()
        else:
            super().dropEvent(event)
