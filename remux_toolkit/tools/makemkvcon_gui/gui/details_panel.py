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

    def show_disc(self, label: str, path: str, total_titles: str, disc_info: dict = None):
        """Display disc-level information"""
        self.clear()
        disc_node = QTreeWidgetItem(["Disc", label])
        self.addTopLevelItem(disc_node)
        QTreeWidgetItem(disc_node, ["Path", path])
        QTreeWidgetItem(disc_node, ["Titles Found", total_titles])

        # Add additional disc info if available
        if disc_info:
            if disc_info.get("type"):
                QTreeWidgetItem(disc_node, ["Type", disc_info["type"]])
            if disc_info.get("volume_name"):
                QTreeWidgetItem(disc_node, ["Volume Name", disc_info["volume_name"]])
            if disc_info.get("language_name"):
                QTreeWidgetItem(disc_node, ["Language", disc_info["language_name"]])
            if disc_info.get("comment"):
                QTreeWidgetItem(disc_node, ["Comment", disc_info["comment"]])

        self.expandAll()

    def show_title(self, t_idx: int, info: dict):
        """Display comprehensive title information"""
        self.clear()
        title_node = QTreeWidgetItem(["Title", f"#{t_idx}"])
        self.addTopLevelItem(title_node)

        # Basic title information
        if info.get("name"):
            QTreeWidgetItem(title_node, ["Title Name", info["name"]])
        if info.get("duration"):
            QTreeWidgetItem(title_node, ["Duration", info["duration"]])
        if info.get("size"):
            QTreeWidgetItem(title_node, ["File Size", info["size"]])
        if info.get("size_bytes"):
            try:
                size_mb = int(info["size_bytes"]) / (1024 * 1024)
                QTreeWidgetItem(title_node, ["Size (MB)", f"{size_mb:,.2f}"])
            except (ValueError, TypeError):
                pass
        if info.get("chapters") is not None:
            QTreeWidgetItem(title_node, ["Chapters", str(info["chapters"])])
        if info.get("bitrate"):
            QTreeWidgetItem(title_node, ["Bitrate", info["bitrate"]])

        # Source information
        if info.get("source"):
            QTreeWidgetItem(title_node, ["Source File", info["source"]])
        if info.get("original_title_id"):
            QTreeWidgetItem(title_node, ["Original Title ID", info["original_title_id"]])
        if info.get("segments_count") and info["segments_count"] != "0":
            QTreeWidgetItem(title_node, ["Segments", info["segments_count"]])

        # Advanced information
        if info.get("angle_info"):
            QTreeWidgetItem(title_node, ["Angle Info", info["angle_info"]])
        if info.get("seamless_info"):
            QTreeWidgetItem(title_node, ["Seamless Info", info["seamless_info"]])
        if info.get("datetime"):
            QTreeWidgetItem(title_node, ["Date/Time", info["datetime"]])
        if info.get("output_filename"):
            QTreeWidgetItem(title_node, ["Output Filename", info["output_filename"]])
        if info.get("comment"):
            QTreeWidgetItem(title_node, ["Comment", info["comment"]])

        # Stream information grouped by type
        stream_groups = {}
        for s in info.get("streams", []):
            kind = s.get("kind", "Other")
            if kind not in stream_groups:
                stream_groups[kind] = QTreeWidgetItem([kind, ""])
                self.addTopLevelItem(stream_groups[kind])

            # Build stream description
            parts = []
            if s.get('lang'):
                parts.append(s['lang'])

            # Use codec_short if available, fallback to parsed codec
            codec = s.get('codec_short') or s.get('codec', '')
            if codec:
                parts.append(f"({codec})")

            # Add format-specific details
            if kind == "Video":
                if s.get('res'):
                    parts.append(s['res'])
                if s.get('fps'):
                    parts.append(f"{s['fps']} fps")
                if s.get('ar'):
                    parts.append(f"AR: {s['ar']}")
            elif kind == "Audio":
                if s.get('channels_display'):
                    parts.append(s['channels_display'])
                elif s.get('channels_layout'):
                    parts.append(s['channels_layout'])
                if s.get('sample_rate'):
                    parts.append(f"{s['sample_rate']} Hz")
                if s.get('bitrate'):
                    parts.append(f"{s['bitrate']}")

            desc = " ".join(p for p in parts if p).strip()
            track_label = f"Track #{s.get('index', '?')}"
            if s.get('name'):
                track_label += f" - {s['name']}"

            track_node = QTreeWidgetItem(stream_groups[kind], [track_label, desc])

            # Add detailed stream properties
            if s.get("lang_code"):
                QTreeWidgetItem(track_node, ["Language Code", s["lang_code"]])
            if s.get("codec_long"):
                QTreeWidgetItem(track_node, ["Codec (Full)", s["codec_long"]])
            if s.get("codec_id"):
                QTreeWidgetItem(track_node, ["Codec ID", s["codec_id"]])

            # Flags
            if flags := s.get("flags"):
                QTreeWidgetItem(track_node, ["Flags", ", ".join(flags)])

            # Output conversion info
            if s.get("output_codec_short"):
                output_node = QTreeWidgetItem(track_node, ["Output Conversion", ""])
                QTreeWidgetItem(output_node, ["Codec", s["output_codec_short"]])
                if s.get("output_conversion_type"):
                    QTreeWidgetItem(output_node, ["Type", s["output_conversion_type"]])
                if s.get("output_audio_sample_rate"):
                    QTreeWidgetItem(output_node, ["Sample Rate", s["output_audio_sample_rate"]])
                if s.get("output_audio_channels"):
                    QTreeWidgetItem(output_node, ["Channels", s["output_audio_channels"]])
                if s.get("output_audio_mix_desc"):
                    QTreeWidgetItem(output_node, ["Mix", s["output_audio_mix_desc"]])

            # Metadata
            if s.get("metadata_lang_name"):
                QTreeWidgetItem(track_node, ["Metadata Language", s["metadata_lang_name"]])

            # MKV-specific flags
            if s.get("mkv_flags_text"):
                QTreeWidgetItem(track_node, ["MKV Flags", s["mkv_flags_text"]])

        self.expandAll()
