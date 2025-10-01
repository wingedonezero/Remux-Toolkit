# remux_toolkit/tools/video_ab_comparator/gui/results_widget.py
from PyQt6 import QtWidgets, QtGui, QtCore

class ResultsWidget(QtWidgets.QWidget):
    """A widget to display the A/B comparison results."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        main_layout = QtWidgets.QVBoxLayout(self)
        verdict_group = QtWidgets.QGroupBox("Verdict")
        verdict_layout = QtWidgets.QVBoxLayout(verdict_group)
        self.verdict_label = QtWidgets.QLabel("<i>Run a comparison to see the verdict.</i>")
        self.verdict_label.setFont(QtGui.QFont("Segoe UI", 12, QtGui.QFont.Weight.Bold))
        self.verdict_label.setWordWrap(True)
        verdict_layout.addWidget(self.verdict_label)
        main_layout.addWidget(verdict_group)

        main_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        viewer_group = QtWidgets.QGroupBox("Frame Viewer")
        viewer_layout = QtWidgets.QHBoxLayout(viewer_group)
        self.frame_a_label = QtWidgets.QLabel("Source A Frame")
        self.frame_a_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.frame_a_label.setMinimumSize(480, 270)
        self.frame_b_label = QtWidgets.QLabel("Source B Frame")
        self.frame_b_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.frame_b_label.setMinimumSize(480, 270)
        viewer_layout.addWidget(self.frame_a_label, 1)
        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        viewer_layout.addWidget(sep)
        viewer_layout.addWidget(self.frame_b_label, 1)
        main_splitter.addWidget(viewer_group)

        scorecard_group = QtWidgets.QGroupBox("Scorecard")
        scorecard_layout = QtWidgets.QVBoxLayout(scorecard_group)
        self.scorecard_tree = QtWidgets.QTreeWidget()
        self.scorecard_tree.setHeaderLabels(["Metric", "Source A", "Source B", "Winner"])
        self.scorecard_tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.scorecard_tree.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.scorecard_tree.header().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.scorecard_tree.header().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        scorecard_layout.addWidget(self.scorecard_tree)
        main_splitter.addWidget(scorecard_group)

        main_splitter.setSizes([600, 300])
        main_layout.addWidget(main_splitter, 1)

    def clear(self):
        self.verdict_label.setText("<i>Run a comparison to see the verdict.</i>")
        self.scorecard_tree.clear()
        self.frame_a_label.setText("Source A Frame")
        self.frame_b_label.setText("Source B Frame")

    def populate(self, results):
        self.results = results
        self.verdict_label.setText(results.get("verdict", ""))
        self.scorecard_tree.clear()

        for issue, data in results.get("issues", {}).items():
            a_s = data['a'].get('summary', '')
            b_s = data['b'].get('summary', '')
            winner = data.get('winner', '')
            item = QtWidgets.QTreeWidgetItem([issue, a_s, b_s, winner])
            self.scorecard_tree.addTopLevelItem(item)

    def map_ts_b(self, ts_a: float) -> float:
        """
        Map timestamp from source A to corresponding timestamp in source B.

        Convention: offset represents how much B is ahead of A.
        - Negative offset: B is behind A (needs to add time)
        - Positive offset: B is ahead of A (needs to subtract time)

        Formula: ts_b = ts_a - offset - drift*ts_a
        """
        off = float(self.results.get("alignment_offset_secs", 0.0))
        drift = float(self.results.get("alignment_drift_ratio", 0.0))

        # Map timestamp: ts_b = ts_a - (offset + drift*ts_a)
        ts_b = ts_a - (off + drift * ts_a)

        return max(0.0, ts_b)
