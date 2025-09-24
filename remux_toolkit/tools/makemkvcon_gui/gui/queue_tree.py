# remux_toolkit/tools/makemkvcon_gui/gui/queue_tree.py
from pathlib import Path
from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import QAbstractItemView, QTreeWidget

class DropTree(QTreeWidget):
    pathsDropped = pyqtSignal(list)
    itemsReordered = pyqtSignal()

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setUniformRowHeights(True)
        self.setExpandsOnDoubleClick(True)

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
                return
        super().dropEvent(event)
        self.itemsReordered.emit()
