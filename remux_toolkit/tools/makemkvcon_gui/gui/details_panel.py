# remux_toolkit/tools/makemkvcon_gui/gui/details_panel.py
from PyQt6.QtWidgets import QTreeWidget, QTreeWidgetItem, QHeaderView
from PyQt6.QtCore import Qt

class DetailsPanel(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["Property", "Value"])
        self.setRootIsDecorated(True)
        hdr = self.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

    def show_disc(self, label: str, path: str, total_titles: str):
        self.clear()
        disc_node = QTreeWidgetItem(["Disc", label])
        self.addTopLevelItem(disc_node)
        QTreeWidgetItem(disc_node, ["Path", path])
        QTreeWidgetItem(disc_node, ["Titles Found", total_titles])
        self.expandAll()

    def show_title(self, t_idx: int, info: dict):
        self.clear()
        title_node = QTreeWidgetItem(["Title", f"#{t_idx}"])
        self.addTopLevelItem(title_node)
        if info.get("duration"): QTreeWidgetItem(title_node, ["Duration", info["duration"]])
        if info.get("size"): QTreeWidgetItem(title_node, ["File Size", info["size"]])
        if info.get("chapters") is not None: QTreeWidgetItem(title_node, ["Chapters", str(info["chapters"])])
        if info.get("source"): QTreeWidgetItem(title_node, ["Source File", info["source"]])
        if info.get("name"): QTreeWidgetItem(title_node, ["Title Name", info["name"]])
        if info.get("original_title_id"): QTreeWidgetItem(title_node, ["Original Title ID", info["original_title_id"]])

        stream_groups = {}
        for s in info.get("streams", []):
            kind = s.get("kind", "Other")
            if kind not in stream_groups:
                stream_groups[kind] = QTreeWidgetItem([kind, ""])
                self.addTopLevelItem(stream_groups[kind])

            parts = [s.get('lang', ''), f"({s.get('codec', '')})", s.get('channels_display', ''), s.get('res', '')]
            desc = " ".join(p for p in parts if p).strip()
            track_node = QTreeWidgetItem(stream_groups[kind], [f"Track #{s.get('index', '?')}", desc])
            if s.get("flags"): QTreeWidgetItem(track_node, ["Flags", ", ".join(s.get("flags"))])

        self.expandAll()
