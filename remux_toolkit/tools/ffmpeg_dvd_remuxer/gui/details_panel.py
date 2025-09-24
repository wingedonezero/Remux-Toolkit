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
        if (audio := title_info.get('audio')):
            QTreeWidgetItem(title_node, ["Audio Streams", audio])
        if (subs := title_info.get('subs')):
            QTreeWidgetItem(title_node, ["Subtitles", subs])
        self.expandAll()
