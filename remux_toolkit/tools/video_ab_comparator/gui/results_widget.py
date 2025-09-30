# remux_toolkit/tools/video_ab_comparator/gui/results_widget.py

from PyQt6 import QtWidgets, QtGui, QtCore

class ResultsWidget(QtWidgets.QWidget):
    """A widget to display the A/B comparison results."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        main_layout = QtWidgets.QVBoxLayout(self)

        # --- Verdict ---
        verdict_group = QtWidgets.QGroupBox("Verdict")
        verdict_layout = QtWidgets.QVBoxLayout(verdict_group)
        self.verdict_label = QtWidgets.QLabel("<i>Run a comparison to see the verdict.</i>")
        self.verdict_label.setFont(QtGui.QFont("Segoe UI", 12, QtGui.QFont.Weight.Bold))
        self.verdict_label.setWordWrap(True)
        verdict_layout.addWidget(self.verdict_label)
        main_layout.addWidget(verdict_group)

        # --- Main Splitter (Scorecard | Frame Viewer) ---
        main_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)

        # --- Scorecard ---
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

        # --- Frame Viewer ---
        viewer_group = QtWidgets.QGroupBox("Frame Viewer")
        viewer_layout = QtWidgets.QVBoxLayout(viewer_group)
        self.frame_a_label = QtWidgets.QLabel("Source A Frame")
        self.frame_a_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.frame_a_label.setMinimumSize(480, 270)
        self.frame_b_label = QtWidgets.QLabel("Source B Frame")
        self.frame_b_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.frame_b_label.setMinimumSize(480, 270)

        viewer_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        viewer_splitter.addWidget(self.frame_a_label)
        viewer_splitter.addWidget(self.frame_b_label)
        viewer_layout.addWidget(viewer_splitter)
        main_splitter.addWidget(viewer_group)

        main_splitter.setSizes([400, 800])
        main_layout.addWidget(main_splitter, 1)

    def clear(self):
        """Clears all results from the view."""
        self.verdict_label.setText("<i>Run a comparison to see the verdict.</i>")
        self.scorecard_tree.clear()
        self.frame_a_label.setText("Source A Frame")
        self.frame_b_label.setText("Source B Frame")

    def populate(self, results_data: dict):
        """Populates the widget with data from the pipeline."""
        self.clear()
        self.verdict_label.setText(results_data.get("verdict", "Verdict could not be determined."))

        issues = results_data.get("issues", {})
        for issue_name, issue_data in issues.items():
            a = issue_data.get('a', {})
            b = issue_data.get('b', {})

            # --- NEW LOGIC for multi-part data ---
            if 'data' in a and isinstance(a['data'], dict):
                parent_item = QtWidgets.QTreeWidgetItem([issue_name])
                self.scorecard_tree.addTopLevelItem(parent_item)
                for key, val_a in a['data'].items():
                    val_b = b.get('data', {}).get(key, 'N/A')
                    child_item = QtWidgets.QTreeWidgetItem([f"  - {key}", str(val_a), str(val_b), "---"])
                    parent_item.addChild(child_item)
                parent_item.setExpanded(True)
                continue
            # --- END NEW LOGIC ---

            # Determine winner for scored issues
            winner = "---"
            if a.get('score', -1) != -1 and b.get('score', -1) != -1:
                if a['score'] < b['score']: winner = "A"
                elif b['score'] < a['score']: winner = "B"
                else: winner = "Tie"

            item = QtWidgets.QTreeWidgetItem([
                issue_name, a.get('summary', 'N/A'), b.get('summary', 'N/A'), winner
            ])
            self.scorecard_tree.addTopLevelItem(item)
