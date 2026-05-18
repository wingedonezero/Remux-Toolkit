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

        # Analyzer fields — surface the "why" for every title so the user can
        # always understand why a title was or wasn't auto-checked.
        analyzer_lines: list[tuple[str, str]] = []
        if (vts := info.get("vts")) is not None:
            analyzer_lines.append(
                ("VTS / PGC", f"VTS {vts}, PGC {info.get('pgc', '?')}, "
                              f"{info.get('num_cells', 0)} cell(s)")
            )
        if cls := info.get("classification"):
            analyzer_lines.append(("Classification", cls))
        if (dup_of := info.get("duplicate_of")) is not None:
            basis = info.get("duplicate_basis") or ""
            extra = f"  (matched on: {basis})" if basis else ""
            analyzer_lines.append(("Duplicate of", f"#{dup_of}{extra}"))
        if contains := info.get("contains_titles"):
            analyzer_lines.append(
                ("Contains titles", ", ".join(f"#{n}" for n in contains))
            )
        if cc := info.get("closed_caption_channels"):
            analyzer_lines.append(("Closed Captions", ", ".join(cc)))
        if info.get("hidden_by_default"):
            analyzer_lines.append(
                ("Hidden by default", "Yes — unchecked but selectable; check to include")
            )

        if analyzer_lines:
            analyzer_node = QTreeWidgetItem(["Analysis", ""])
            self.addTopLevelItem(analyzer_node)
            for label, value in analyzer_lines:
                row = QTreeWidgetItem(analyzer_node, [label, value])
                if label == "Duplicate of":
                    row.setForeground(1, QColor(255, 180, 100))
                elif label == "Hidden by default":
                    row.setForeground(1, QColor(180, 180, 180))

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
