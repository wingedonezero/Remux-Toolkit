# remux_toolkit/tools/ffmpeg_dvd_remuxer/gui/details_panel.py
from PyQt6.QtWidgets import QTreeWidget, QTreeWidgetItem, QHeaderView

class DetailsPanel(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["Property", "Value"])
        self.setRootIsDecorated(True)
        hdr = self.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

    def clear_panel(self):
        self.clear()

    def show_disc_info(self, job):
        self.clear()
        disc_node = QTreeWidgetItem(["Disc", job.base_name])
        self.addTopLevelItem(disc_node)
        QTreeWidgetItem(disc_node, ["Path", str(job.source_path)])
        QTreeWidgetItem(disc_node, ["Titles Found", str(len(job.titles_info))])
        if job.group_name:
            QTreeWidgetItem(disc_node, ["Group", job.group_name])
        self.expandAll()

    def show_title_info(self, disc_job, title_info: dict):
        self.clear()
        title_node = QTreeWidgetItem(["Title", f"#{title_info.get('title', '?')} on {disc_job.base_name}"])
        self.addTopLevelItem(title_node)

        if (length := title_info.get('length')):
            QTreeWidgetItem(title_node, ["Length", length])
        if (chapters := title_info.get('chapters')):
            QTreeWidgetItem(title_node, ["Chapters", chapters])

        # This rewritten section now displays full, rich per-stream metadata
        stream_groups = {}
        for stream in title_info.get("streams", []):
            kind = stream.get("codec_type", "other").capitalize()
            if kind not in stream_groups:
                stream_groups[kind] = QTreeWidgetItem([kind, ""])
                self.addTopLevelItem(stream_groups[kind])

            # --- Create a descriptive summary for the track ---
            stream_index = stream.get('index')
            codec = stream.get('codec_name', 'N/A')
            lang_tag = stream.get('tags', {}).get('language', 'und')
            lang = f"[{lang_tag}]" if lang_tag != 'und' else ""
            desc_parts = [f"Track #{stream_index}", f"({codec})", lang]

            # Add specific details to the summary line
            if kind == 'Video':
                width = stream.get('width', 0)
                height = stream.get('height', 0)
                if width and height: desc_parts.append(f"{width}x{height}")
            if kind == 'Audio':
                if layout := stream.get('channel_layout'): desc_parts.append(layout)
                elif ch := stream.get('channels'): desc_parts.append(f"{ch}ch")

            track_node = QTreeWidgetItem(stream_groups[kind], [" ".join(filter(None, desc_parts)), ""])

            # --- Add detailed sub-items for each track ---
            if (fr := stream.get('r_frame_rate')) and fr != '0/0':
                QTreeWidgetItem(track_node, ["Frame Rate", fr])
            if (ar := stream.get('display_aspect_ratio')):
                 QTreeWidgetItem(track_node, ["Aspect Ratio", ar])
            if (fo := title_info.get('field_order')):
                QTreeWidgetItem(track_node, ["Interlacing", fo])
            if (br := stream.get('bit_rate')):
                QTreeWidgetItem(track_node, ["Bitrate", f"{int(br) // 1000} kb/s"])
            if (sr := stream.get('sample_rate')):
                QTreeWidgetItem(track_node, ["Sample Rate", f"{sr} Hz"])

        self.expandAll()
