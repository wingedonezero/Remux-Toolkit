# remux_toolkit/tools/video_ab_comparator/gui/detailed_comparison_widget.py
from PyQt6 import QtWidgets, QtGui, QtCore
import os
import json

class DetailedComparisonWidget(QtWidgets.QWidget):
    """Widget to display frame-by-frame comparison of analysis chunks."""

    request_chunk_frames = QtCore.pyqtSignal(int, int)  # chunk_idx, frame_idx

    def __init__(self, parent=None):
        super().__init__()
        self.parent_widget = parent
        self.results = None
        self.chunk_data = []
        self.current_chunk_idx = -1
        self.current_frame_idx = 0
        self.frames_per_chunk = []  # Store how many frames each chunk has
        self._init_ui()

    def _init_ui(self):
        main_layout = QtWidgets.QVBoxLayout(self)

        # Info bar
        info_layout = QtWidgets.QHBoxLayout()
        self.info_label = QtWidgets.QLabel("Select a chunk to view frame-by-frame comparison")
        self.info_label.setFont(QtGui.QFont("Segoe UI", 10))
        info_layout.addWidget(self.info_label)
        info_layout.addStretch()
        main_layout.addLayout(info_layout)

        # Splitter for list and details
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)

        # Left side: Chunk list
        list_widget = QtWidgets.QWidget()
        list_layout = QtWidgets.QVBoxLayout(list_widget)
        list_layout.setContentsMargins(0, 0, 0, 0)

        list_label = QtWidgets.QLabel("Analysis Chunks")
        list_label.setFont(QtGui.QFont("Segoe UI", 10, QtGui.QFont.Weight.Bold))
        list_layout.addWidget(list_label)

        self.chunk_list = QtWidgets.QListWidget()
        self.chunk_list.currentRowChanged.connect(self.on_chunk_selected)
        list_layout.addWidget(self.chunk_list)

        splitter.addWidget(list_widget)

        # Right side: Frame-by-frame view
        details_widget = QtWidgets.QWidget()
        details_layout = QtWidgets.QVBoxLayout(details_widget)
        details_layout.setContentsMargins(0, 0, 0, 0)

        # Chunk info
        chunk_info_group = QtWidgets.QGroupBox("Chunk Info")
        chunk_info_layout = QtWidgets.QFormLayout(chunk_info_group)
        self.chunk_start_label = QtWidgets.QLabel("--")
        self.chunk_duration_label = QtWidgets.QLabel("--")
        self.offset_label = QtWidgets.QLabel("--")
        chunk_info_layout.addRow("Chunk Start (A):", self.chunk_start_label)
        chunk_info_layout.addRow("Duration:", self.chunk_duration_label)
        chunk_info_layout.addRow("Applied Offset:", self.offset_label)
        details_layout.addWidget(chunk_info_group)

        # Frame navigation
        nav_group = QtWidgets.QGroupBox("Frame Navigation")
        nav_layout = QtWidgets.QVBoxLayout(nav_group)

        # Frame slider
        slider_layout = QtWidgets.QHBoxLayout()
        slider_layout.addWidget(QtWidgets.QLabel("Frame:"))
        self.frame_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.frame_slider.setMinimum(0)
        self.frame_slider.setMaximum(0)
        self.frame_slider.valueChanged.connect(self.on_frame_changed)
        slider_layout.addWidget(self.frame_slider, 1)
        self.frame_counter_label = QtWidgets.QLabel("0 / 0")
        self.frame_counter_label.setMinimumWidth(60)
        slider_layout.addWidget(self.frame_counter_label)
        nav_layout.addLayout(slider_layout)

        # Navigation buttons
        btn_layout = QtWidgets.QHBoxLayout()
        self.first_btn = QtWidgets.QPushButton("⏮ First")
        self.prev_btn = QtWidgets.QPushButton("◀ Prev")
        self.next_btn = QtWidgets.QPushButton("Next ▶")
        self.last_btn = QtWidgets.QPushButton("Last ⏭")

        self.first_btn.clicked.connect(lambda: self.frame_slider.setValue(0))
        self.prev_btn.clicked.connect(lambda: self.frame_slider.setValue(max(0, self.frame_slider.value() - 1)))
        self.next_btn.clicked.connect(lambda: self.frame_slider.setValue(min(self.frame_slider.maximum(), self.frame_slider.value() + 1)))
        self.last_btn.clicked.connect(lambda: self.frame_slider.setValue(self.frame_slider.maximum()))

        btn_layout.addWidget(self.first_btn)
        btn_layout.addWidget(self.prev_btn)
        btn_layout.addWidget(self.next_btn)
        btn_layout.addWidget(self.last_btn)
        nav_layout.addLayout(btn_layout)

        # Current frame timestamps
        ts_layout = QtWidgets.QHBoxLayout()
        self.frame_ts_a_label = QtWidgets.QLabel("A: --")
        self.frame_ts_b_label = QtWidgets.QLabel("B: --")
        ts_layout.addWidget(self.frame_ts_a_label)
        ts_layout.addStretch()
        ts_layout.addWidget(self.frame_ts_b_label)
        nav_layout.addLayout(ts_layout)

        details_layout.addWidget(nav_group)

        # Frame viewer
        frames_group = QtWidgets.QGroupBox("Frame-by-Frame Comparison")
        frames_layout = QtWidgets.QHBoxLayout(frames_group)

        # Source A frame
        a_frame_container = QtWidgets.QVBoxLayout()
        a_label = QtWidgets.QLabel("Source A")
        a_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        a_label.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
        self.frame_a_viewer = QtWidgets.QLabel("Select a chunk to begin")
        self.frame_a_viewer.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.frame_a_viewer.setMinimumSize(400, 225)
        self.frame_a_viewer.setStyleSheet("border: 1px solid #555; background: #1a1a1a;")
        a_frame_container.addWidget(a_label)
        a_frame_container.addWidget(self.frame_a_viewer, 1)

        # Source B frame
        b_frame_container = QtWidgets.QVBoxLayout()
        b_label = QtWidgets.QLabel("Source B")
        b_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        b_label.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
        self.frame_b_viewer = QtWidgets.QLabel("Select a chunk to begin")
        self.frame_b_viewer.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.frame_b_viewer.setMinimumSize(400, 225)
        self.frame_b_viewer.setStyleSheet("border: 1px solid #555; background: #1a1a1a;")
        b_frame_container.addWidget(b_label)
        b_frame_container.addWidget(self.frame_b_viewer, 1)

        frames_layout.addLayout(a_frame_container)
        frames_layout.addLayout(b_frame_container)
        details_layout.addWidget(frames_group, 1)

        # Metrics comparison table - PER FRAME
        metrics_group = QtWidgets.QGroupBox("Detector Results for Current Frame")
        metrics_layout = QtWidgets.QVBoxLayout(metrics_group)

        self.metrics_table = QtWidgets.QTableWidget()
        self.metrics_table.setColumnCount(4)
        self.metrics_table.setHorizontalHeaderLabels(["Detector", "Score A", "Score B", "Winner"])
        self.metrics_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.metrics_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.metrics_table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.metrics_table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.metrics_table.setAlternatingRowColors(True)
        self.metrics_table.setMaximumHeight(250)

        metrics_layout.addWidget(self.metrics_table)

        # Add note about frame-specific data
        note_label = QtWidgets.QLabel("💡 Scores shown are for the current frame only")
        note_label.setStyleSheet("color: #888; font-style: italic;")
        metrics_layout.addWidget(note_label)

        details_layout.addWidget(metrics_group)

        splitter.addWidget(details_widget)
        splitter.setSizes([250, 950])

        main_layout.addWidget(splitter, 1)

    def clear(self):
        """Clear all data."""
        self.chunk_list.clear()
        self.chunk_data = []
        self.frames_per_chunk = []
        self.results = None
        self.current_chunk_idx = -1
        self.current_frame_idx = 0
        self.info_label.setText("Select a chunk to view frame-by-frame comparison")
        self.chunk_start_label.setText("--")
        self.chunk_duration_label.setText("--")
        self.offset_label.setText("--")
        self.frame_ts_a_label.setText("A: --")
        self.frame_ts_b_label.setText("B: --")
        self.frame_slider.setValue(0)
        self.frame_slider.setMaximum(0)
        self.frame_counter_label.setText("0 / 0")
        self.frame_a_viewer.setText("Select a chunk to begin")
        self.frame_b_viewer.setText("Select a chunk to begin")
        self.metrics_table.setRowCount(0)

    def load_chunk_data(self, temp_dir: str, results: dict):
        """Load chunk comparison data from temp directory."""
        self.clear()
        self.results = results

        chunk_metadata_path = os.path.join(temp_dir, "chunk_metadata.json")

        if not os.path.exists(chunk_metadata_path):
            self.info_label.setText("No chunk data available - run a comparison first")
            return

        try:
            with open(chunk_metadata_path, 'r') as f:
                self.chunk_data = json.load(f)

            # Calculate frames per chunk based on duration and extraction rate (10fps)
            for chunk in self.chunk_data:
                duration = chunk.get('duration', 2.0)
                # Frames extracted at 10fps
                num_frames = int(duration * 10)
                self.frames_per_chunk.append(num_frames)

            # Populate chunk list
            for i, chunk in enumerate(self.chunk_data):
                ts_a = chunk['timestamp_a']
                ts_b = chunk['timestamp_b']
                num_frames = self.frames_per_chunk[i]
                item_text = f"Chunk {i+1}: A@{ts_a:.2f}s / B@{ts_b:.2f}s ({num_frames} frames)"
                self.chunk_list.addItem(item_text)

            self.info_label.setText(f"Loaded {len(self.chunk_data)} chunks - select one to browse frames")

            # Auto-select first chunk
            if self.chunk_data:
                self.chunk_list.setCurrentRow(0)

        except Exception as e:
            self.info_label.setText(f"Error loading chunk data: {e}")

    def on_chunk_selected(self, index: int):
        """Called when user selects a chunk from the list."""
        if index < 0 or index >= len(self.chunk_data):
            return

        self.current_chunk_idx = index
        chunk = self.chunk_data[index]

        # Update chunk info labels
        self.chunk_start_label.setText(f"{chunk['timestamp_a']:.3f}s")
        self.chunk_duration_label.setText(f"{chunk['duration']:.1f}s")

        offset = self.results.get('alignment_offset_secs', 0.0)
        drift = self.results.get('alignment_drift_ratio', 0.0)
        self.offset_label.setText(f"{offset:.3f}s (drift: {drift:.6f})")

        # Setup frame slider
        num_frames = self.frames_per_chunk[index]
        self.frame_slider.setMaximum(max(0, num_frames - 1))
        self.frame_slider.setValue(0)
        self.frame_counter_label.setText(f"1 / {num_frames}")

        # Update metrics table for first frame
        self._update_metrics_table_for_frame(chunk, 0)

        # Load first frame
        self.current_frame_idx = 0
        self.request_chunk_frames.emit(index, 0)

    def on_frame_changed(self, frame_idx: int):
        """Called when frame slider changes."""
        if self.current_chunk_idx < 0:
            return

        self.current_frame_idx = frame_idx
        num_frames = self.frames_per_chunk[self.current_chunk_idx]
        self.frame_counter_label.setText(f"{frame_idx + 1} / {num_frames}")

        # Update timestamp labels
        chunk = self.chunk_data[self.current_chunk_idx]
        chunk_start_a = chunk['timestamp_a']
        chunk_start_b = chunk['timestamp_b']

        # Calculate timestamp for this specific frame (10fps = 0.1s per frame)
        frame_offset = frame_idx * 0.1
        ts_a = chunk_start_a + frame_offset
        ts_b = chunk_start_b + frame_offset

        self.frame_ts_a_label.setText(f"A: {ts_a:.3f}s")
        self.frame_ts_b_label.setText(f"B: {ts_b:.3f}s")

        # Update metrics table for THIS FRAME
        self._update_metrics_table_for_frame(chunk, frame_idx)

        # Request frames
        self.request_chunk_frames.emit(self.current_chunk_idx, frame_idx)

    def _update_metrics_table_for_frame(self, chunk, frame_idx):
        """Update metrics table with scores for a specific frame."""
        self.metrics_table.setRowCount(0)

        # Get frame-specific scores
        frame_scores_list = chunk.get('frame_scores', [])
        if frame_idx >= len(frame_scores_list):
            return

        frame_data = frame_scores_list[frame_idx]
        frame_detectors = frame_data.get('detectors', {})

        for detector_name, scores in frame_detectors.items():
            row = self.metrics_table.rowCount()
            self.metrics_table.insertRow(row)

            score_a = scores.get('score_a', -1)
            score_b = scores.get('score_b', -1)

            # Determine winner
            if score_a >= 0 and score_b >= 0:
                if abs(score_a - score_b) < 2.0:
                    winner = "Tie"
                else:
                    winner = "A" if score_a < score_b else "B"
            elif score_a >= 0:
                winner = "A"
            elif score_b >= 0:
                winner = "B"
            else:
                winner = "N/A"

            self.metrics_table.setItem(row, 0, QtWidgets.QTableWidgetItem(detector_name))
            self.metrics_table.setItem(row, 1, QtWidgets.QTableWidgetItem(f"{score_a:.1f}" if score_a >= 0 else "N/A"))
            self.metrics_table.setItem(row, 2, QtWidgets.QTableWidgetItem(f"{score_b:.1f}" if score_b >= 0 else "N/A"))

            winner_item = QtWidgets.QTableWidgetItem(winner)
            if winner == "A":
                winner_item.setForeground(QtGui.QColor(100, 200, 100))
            elif winner == "B":
                winner_item.setForeground(QtGui.QColor(100, 150, 255))
            self.metrics_table.setItem(row, 3, winner_item)

    def display_chunk_frames(self, chunk_idx: int, frame_idx: int, frame_a, frame_b):
        """Display specific frames from a chunk."""
        if chunk_idx != self.current_chunk_idx or frame_idx != self.current_frame_idx:
            return  # Outdated request

        # Display frame A
        if frame_a is not None:
            h, w, ch = frame_a.shape
            q_img = QtGui.QImage(frame_a.data, w, h, ch * w, QtGui.QImage.Format.Format_BGR888)
            pixmap = QtGui.QPixmap.fromImage(q_img)
            self.frame_a_viewer.setPixmap(
                pixmap.scaled(
                    self.frame_a_viewer.size(),
                    QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                    QtCore.Qt.TransformationMode.SmoothTransformation
                )
            )
        else:
            self.frame_a_viewer.setText("Failed to load frame A")

        # Display frame B
        if frame_b is not None:
            h, w, ch = frame_b.shape
            q_img = QtGui.QImage(frame_b.data, w, h, ch * w, QtGui.QImage.Format.Format_BGR888)
            pixmap = QtGui.QPixmap.fromImage(q_img)
            self.frame_b_viewer.setPixmap(
                pixmap.scaled(
                    self.frame_b_viewer.size(),
                    QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                    QtCore.Qt.TransformationMode.SmoothTransformation
                )
            )
        else:
            self.frame_b_viewer.setText("Failed to load frame B")
