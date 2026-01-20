# remux_toolkit/tools/ffmpeg_dvd_gui/gui/details_panel.py
from PyQt6.QtWidgets import QTreeWidget, QTreeWidgetItem, QHeaderView
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor


class DetailsPanel(QTreeWidget):
    """Panel displaying disc and title details for FFmpeg DVD remuxer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["Property", "Value"])
        self.setRootIsDecorated(True)
        hdr = self.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

    def show_disc(self, label: str, path: str, total_titles: str, disc_info: dict = None):
        """Display disc-level information."""
        self.clear()
        disc_node = QTreeWidgetItem(["Disc", label])
        self.addTopLevelItem(disc_node)
        QTreeWidgetItem(disc_node, ["Path", path])
        QTreeWidgetItem(disc_node, ["Titles Found", total_titles])

        if disc_info:
            if disc_info.get("type"):
                QTreeWidgetItem(disc_node, ["Type", disc_info["type"]])

        self.expandAll()

    def show_title(self, t_idx: int, info: dict):
        """Display title information."""
        self.clear()
        title_node = QTreeWidgetItem(["Title", f"#{t_idx}"])
        self.addTopLevelItem(title_node)

        # Basic info
        if info.get("duration"):
            QTreeWidgetItem(title_node, ["Duration", info["duration"]])

        if info.get("chapters") is not None:
            chapters = info["chapters"]
            item = QTreeWidgetItem(title_node, ["Chapters", str(chapters)])
            if chapters > 0:
                item.setForeground(1, QColor(100, 255, 100))

        if info.get("video_codec"):
            QTreeWidgetItem(title_node, ["Video Codec", info["video_codec"]])

        if info.get("audio_count"):
            QTreeWidgetItem(title_node, ["Audio Tracks", str(info["audio_count"])])

        if info.get("subtitle_count"):
            QTreeWidgetItem(title_node, ["Subtitle Tracks", str(info["subtitle_count"])])

        # Stream details grouped by type
        stream_groups = {}
        for s in info.get("streams", []):
            kind = s.get("kind", "Other")
            if kind not in stream_groups:
                stream_groups[kind] = QTreeWidgetItem([kind, ""])
                self.addTopLevelItem(stream_groups[kind])

            # Build stream description
            parts = []

            codec = s.get('codec', '')
            if codec:
                parts.append(f"({codec})")

            lang = s.get('language', '')
            if lang and lang != 'und':
                parts.append(lang)

            # Type-specific details
            if kind == "Video":
                width = s.get('width', 0)
                height = s.get('height', 0)
                if width and height:
                    parts.append(f"{width}x{height}")

            elif kind == "Audio":
                channels = s.get('channels', 0)
                if channels:
                    if channels == 1:
                        parts.append("Mono")
                    elif channels == 2:
                        parts.append("Stereo")
                    elif channels == 6:
                        parts.append("5.1")
                    elif channels == 8:
                        parts.append("7.1")
                    else:
                        parts.append(f"{channels}ch")

                sample_rate = s.get('sample_rate', 0)
                if sample_rate:
                    parts.append(f"{sample_rate} Hz")

            desc = " ".join(p for p in parts if p).strip()
            track_label = f"Track #{s.get('index', '?')}"

            QTreeWidgetItem(stream_groups[kind], [track_label, desc])

        self.expandAll()
