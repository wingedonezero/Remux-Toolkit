"""
GUI for Video Sync Tool
"""

import os
from pathlib import Path
from PyQt6 import QtWidgets, QtCore, QtGui
from . import video_sync_config as config
from . import video_sync_core as core


class VideoSyncWidget(QtWidgets.QWidget):
    """
    Widget for synchronizing videos using audio correlation.
    Aligns target video(s) to match a reference video perfectly.
    """

    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'video_sync'

        # Workers
        self.analysis_worker = None
        self.alignment_worker = None

        # Analysis results
        self.segment_map = None

        self._init_ui()
        self._load_settings()

        # Enable drag and drop
        self.setAcceptDrops(True)

    def _init_ui(self):
        """Initialize the user interface"""
        main_layout = QtWidgets.QVBoxLayout(self)

        # Create splitter for resizable sections
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)

        # Top section: File management and settings
        top_widget = self._create_input_section()
        splitter.addWidget(top_widget)

        # Middle section: Segment map display
        middle_widget = self._create_segment_section()
        splitter.addWidget(middle_widget)

        # Bottom section: Log output
        bottom_widget = self._create_log_section()
        splitter.addWidget(bottom_widget)

        # Set initial splitter sizes
        splitter.setSizes([350, 200, 250])

        main_layout.addWidget(splitter)

    def _create_input_section(self):
        """Create the file input and settings section"""
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)

        # Reference file
        ref_group = QtWidgets.QGroupBox("Reference Video (Perfect Master)")
        ref_layout = QtWidgets.QHBoxLayout(ref_group)

        self.reference_input = QtWidgets.QLineEdit()
        self.reference_input.setPlaceholderText("Select reference MKV file...")
        self.reference_input.textChanged.connect(self._update_ui_state)
        ref_layout.addWidget(self.reference_input, 1)

        self.browse_ref_btn = QtWidgets.QPushButton("Browse...")
        self.browse_ref_btn.clicked.connect(self._browse_reference)
        ref_layout.addWidget(self.browse_ref_btn)

        self.clear_ref_btn = QtWidgets.QPushButton("Clear")
        self.clear_ref_btn.clicked.connect(lambda: self.reference_input.clear())
        ref_layout.addWidget(self.clear_ref_btn)

        layout.addWidget(ref_group)

        # Target files
        target_group = QtWidgets.QGroupBox("Target Video(s) to Align (In Order)")
        target_layout = QtWidgets.QVBoxLayout(target_group)

        # File list
        self.file_count_label = QtWidgets.QLabel("Target Files (0)")
        self.file_count_label.setStyleSheet("font-weight: bold;")
        target_layout.addWidget(self.file_count_label)

        self.target_list = QtWidgets.QListWidget()
        self.target_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.target_list.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.InternalMove)
        self.target_list.itemSelectionChanged.connect(self._update_ui_state)
        target_layout.addWidget(self.target_list)

        # Target file buttons
        button_layout = QtWidgets.QHBoxLayout()

        self.add_targets_btn = QtWidgets.QPushButton("Add Files")
        self.add_targets_btn.clicked.connect(self._add_target_files)
        button_layout.addWidget(self.add_targets_btn)

        self.remove_targets_btn = QtWidgets.QPushButton("Remove Selected")
        self.remove_targets_btn.clicked.connect(self._remove_selected_targets)
        button_layout.addWidget(self.remove_targets_btn)

        self.clear_targets_btn = QtWidgets.QPushButton("Clear All")
        self.clear_targets_btn.clicked.connect(self._clear_targets)
        button_layout.addWidget(self.clear_targets_btn)

        self.move_up_btn = QtWidgets.QPushButton("↑ Move Up")
        self.move_up_btn.clicked.connect(self._move_target_up)
        button_layout.addWidget(self.move_up_btn)

        self.move_down_btn = QtWidgets.QPushButton("↓ Move Down")
        self.move_down_btn.clicked.connect(self._move_target_down)
        button_layout.addWidget(self.move_down_btn)

        button_layout.addStretch()
        target_layout.addLayout(button_layout)

        layout.addWidget(target_group)

        # Settings
        settings_group = QtWidgets.QGroupBox("Analysis Settings")
        settings_layout = QtWidgets.QFormLayout(settings_group)

        self.language_input = QtWidgets.QLineEdit()
        self.language_input.setText("jpn")
        self.language_input.setPlaceholderText("3-letter language code (e.g., jpn, eng)")
        settings_layout.addRow("Audio Language:", self.language_input)

        self.threshold_spin = QtWidgets.QDoubleSpinBox()
        self.threshold_spin.setRange(0.0, 1.0)
        self.threshold_spin.setSingleStep(0.05)
        self.threshold_spin.setValue(0.7)
        self.threshold_spin.setDecimals(2)
        settings_layout.addRow("Correlation Threshold:", self.threshold_spin)

        layout.addWidget(settings_group)

        # Output settings
        output_group = QtWidgets.QGroupBox("Output Settings")
        output_layout = QtWidgets.QVBoxLayout(output_group)

        # Output directory
        dir_layout = QtWidgets.QHBoxLayout()
        dir_layout.addWidget(QtWidgets.QLabel("Output Directory:"))
        self.output_dir_input = QtWidgets.QLineEdit()
        self.output_dir_input.setPlaceholderText("Auto-set to reference file's directory")
        self.output_dir_input.textChanged.connect(self._update_output_preview)
        dir_layout.addWidget(self.output_dir_input, 1)

        self.browse_dir_btn = QtWidgets.QPushButton("Browse...")
        self.browse_dir_btn.clicked.connect(self._browse_output_dir)
        dir_layout.addWidget(self.browse_dir_btn)
        output_layout.addLayout(dir_layout)

        # Output filename
        file_layout = QtWidgets.QHBoxLayout()
        file_layout.addWidget(QtWidgets.QLabel("Filename:"))
        self.output_filename_input = QtWidgets.QLineEdit()
        self.output_filename_input.setText("aligned_output.mkv")
        self.output_filename_input.textChanged.connect(self._update_output_preview)
        file_layout.addWidget(self.output_filename_input, 1)
        output_layout.addLayout(file_layout)

        # Full output path preview
        self.output_preview_label = QtWidgets.QLabel("Full Path: ")
        self.output_preview_label.setWordWrap(True)
        self.output_preview_label.setStyleSheet("color: #0066cc;")
        output_layout.addWidget(self.output_preview_label)

        layout.addWidget(output_group)

        # Action buttons
        action_layout = QtWidgets.QHBoxLayout()

        self.analyze_btn = QtWidgets.QPushButton("1. Analyze & Find Segments")
        self.analyze_btn.setEnabled(False)
        self.analyze_btn.setStyleSheet("font-weight: bold; padding: 8px; background-color: #4CAF50; color: white;")
        self.analyze_btn.clicked.connect(self._start_analysis)
        action_layout.addWidget(self.analyze_btn)

        self.align_btn = QtWidgets.QPushButton("2. Create Aligned Video")
        self.align_btn.setEnabled(False)
        self.align_btn.setStyleSheet("font-weight: bold; padding: 8px; background-color: #2196F3; color: white;")
        self.align_btn.clicked.connect(self._start_alignment)
        action_layout.addWidget(self.align_btn)

        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_operation)
        action_layout.addWidget(self.cancel_btn)

        action_layout.addStretch()
        layout.addLayout(action_layout)

        return widget

    def _create_segment_section(self):
        """Create the segment map display section"""
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)

        # Header
        header_layout = QtWidgets.QHBoxLayout()
        header_layout.addWidget(QtWidgets.QLabel("Detected Segments"))
        header_layout.addStretch()

        self.clear_segments_btn = QtWidgets.QPushButton("Clear")
        self.clear_segments_btn.clicked.connect(self._clear_segment_display)
        header_layout.addWidget(self.clear_segments_btn)

        layout.addLayout(header_layout)

        # Segment display
        self.segment_display = QtWidgets.QTextEdit()
        self.segment_display.setReadOnly(True)
        font = QtGui.QFont("Monospace")
        font.setStyleHint(QtGui.QFont.StyleHint.TypeWriter)
        self.segment_display.setFont(font)
        layout.addWidget(self.segment_display)

        return widget

    def _create_log_section(self):
        """Create the log output section"""
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)

        # Log label and clear button
        header_layout = QtWidgets.QHBoxLayout()
        header_layout.addWidget(QtWidgets.QLabel("Log Output"))
        header_layout.addStretch()

        self.clear_log_btn = QtWidgets.QPushButton("Clear Log")
        self.clear_log_btn.clicked.connect(self._clear_log)
        header_layout.addWidget(self.clear_log_btn)

        layout.addLayout(header_layout)

        # Log output text area
        self.log_output = QtWidgets.QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        font = QtGui.QFont("Monospace")
        font.setStyleHint(QtGui.QFont.StyleHint.TypeWriter)
        self.log_output.setFont(font)
        layout.addWidget(self.log_output)

        # Progress bar
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        return widget

    def _browse_reference(self):
        """Browse for reference file"""
        file, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select Reference MKV File",
            "",
            "MKV Files (*.mkv);;All Files (*)"
        )

        if file:
            self.reference_input.setText(file)
            # Auto-set output directory
            if not self.output_dir_input.text():
                self.output_dir_input.setText(str(Path(file).parent))

    def _add_target_files(self):
        """Add target files"""
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "Select Target MKV Files (In Order)",
            "",
            "MKV Files (*.mkv);;All Files (*)"
        )

        if files:
            self._add_targets_to_list(files)

    def _add_targets_to_list(self, paths):
        """Add target file paths to the list widget"""
        for path in paths:
            # Validate it's an MKV file
            if not path.lower().endswith('.mkv'):
                self._log(f"Skipping non-MKV file: {path}")
                continue

            # Check if already in list
            already_exists = False
            for i in range(self.target_list.count()):
                if self.target_list.item(i).data(QtCore.Qt.ItemDataRole.UserRole) == path:
                    already_exists = True
                    break

            if already_exists:
                self._log(f"File already in list: {Path(path).name}")
                continue

            # Add to list
            item = QtWidgets.QListWidgetItem(f"{self.target_list.count() + 1}. {Path(path).name}")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path)
            self.target_list.addItem(item)

        self._update_file_count()
        self._update_ui_state()

    def _remove_selected_targets(self):
        """Remove selected target files"""
        for item in self.target_list.selectedItems():
            self.target_list.takeItem(self.target_list.row(item))

        # Renumber items
        for i in range(self.target_list.count()):
            item = self.target_list.item(i)
            path = item.data(QtCore.Qt.ItemDataRole.UserRole)
            item.setText(f"{i + 1}. {Path(path).name}")

        self._update_file_count()
        self._update_ui_state()

    def _clear_targets(self):
        """Clear all target files"""
        self.target_list.clear()
        self._update_file_count()
        self._update_ui_state()

    def _move_target_up(self):
        """Move selected target up"""
        selected_items = self.target_list.selectedItems()
        if not selected_items:
            return

        item = selected_items[0]
        row = self.target_list.row(item)

        if row > 0:
            self.target_list.takeItem(row)
            self.target_list.insertItem(row - 1, item)
            self.target_list.setCurrentItem(item)

            # Renumber
            for i in range(self.target_list.count()):
                it = self.target_list.item(i)
                path = it.data(QtCore.Qt.ItemDataRole.UserRole)
                it.setText(f"{i + 1}. {Path(path).name}")

    def _move_target_down(self):
        """Move selected target down"""
        selected_items = self.target_list.selectedItems()
        if not selected_items:
            return

        item = selected_items[0]
        row = self.target_list.row(item)

        if row < self.target_list.count() - 1:
            self.target_list.takeItem(row)
            self.target_list.insertItem(row + 1, item)
            self.target_list.setCurrentItem(item)

            # Renumber
            for i in range(self.target_list.count()):
                it = self.target_list.item(i)
                path = it.data(QtCore.Qt.ItemDataRole.UserRole)
                it.setText(f"{i + 1}. {Path(path).name}")

    def _browse_output_dir(self):
        """Browse for output directory"""
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select Output Directory",
            self.output_dir_input.text()
        )

        if directory:
            self.output_dir_input.setText(directory)

    def _update_file_count(self):
        """Update the file count label"""
        count = self.target_list.count()
        self.file_count_label.setText(f"Target Files ({count})")

    def _update_ui_state(self):
        """Update UI element states based on current state"""
        has_reference = bool(self.reference_input.text())
        has_targets = self.target_list.count() > 0
        has_selection = len(self.target_list.selectedItems()) > 0

        self.remove_targets_btn.setEnabled(has_selection)
        self.clear_targets_btn.setEnabled(has_targets)
        self.move_up_btn.setEnabled(has_selection)
        self.move_down_btn.setEnabled(has_selection)
        self.analyze_btn.setEnabled(has_reference and has_targets)
        self.align_btn.setEnabled(self.segment_map is not None)

    def _update_output_preview(self):
        """Update the output path preview label"""
        output_dir = self.output_dir_input.text()
        filename = self.output_filename_input.text()

        if output_dir and filename:
            full_path = str(Path(output_dir) / filename)
            self.output_preview_label.setText(f"Full Path: {full_path}")
        else:
            self.output_preview_label.setText("Full Path: ")

    def _start_analysis(self):
        """Start the analysis operation"""
        reference_path = self.reference_input.text()

        # Get target paths in order
        target_paths = []
        for i in range(self.target_list.count()):
            item = self.target_list.item(i)
            target_paths.append(item.data(QtCore.Qt.ItemDataRole.UserRole))

        # Validate files
        is_valid, error = core.validate_mkv_file(reference_path)
        if not is_valid:
            QtWidgets.QMessageBox.warning(self, "Invalid Reference", error)
            return

        for path in target_paths:
            is_valid, error = core.validate_mkv_file(path)
            if not is_valid:
                QtWidgets.QMessageBox.warning(self, "Invalid Target", error)
                return

        # Clear previous results
        self._clear_log()
        self._clear_segment_display()
        self.segment_map = None

        self._log("Starting analysis...")
        self._log(f"Reference: {Path(reference_path).name}")
        for i, path in enumerate(target_paths, 1):
            self._log(f"Target {i}: {Path(path).name}")
        self._log("")

        # Disable UI
        self._set_ui_busy(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

        # Get settings
        settings = {
            'audio_language': self.language_input.text(),
            'correlation_threshold': self.threshold_spin.value(),
            'chunk_duration_sec': config.DEFAULTS['chunk_duration_sec'],
        }

        # Start analysis worker
        self.analysis_worker = core.AnalysisWorker(reference_path, target_paths, settings)
        self.analysis_worker.progress.connect(self._log)
        self.analysis_worker.result.connect(self._on_analysis_finished)
        self.analysis_worker.start()

    def _on_analysis_finished(self, segment_map, error_message):
        """Handle analysis completion"""
        self._set_ui_busy(False)
        self.progress_bar.setVisible(False)

        if error_message:
            self._log(f"\n✗ Analysis failed: {error_message}")
            QtWidgets.QMessageBox.critical(self, "Analysis Failed", error_message)
            return

        self.segment_map = segment_map
        self._display_segment_map(segment_map)

        self._log("\n✓ Analysis completed successfully!")
        self._log(f"Global offset detected: {segment_map.global_offset_ms}ms")
        self._log("\nReview the segments above, then click 'Create Aligned Video' to proceed.")

        self._update_ui_state()

    def _display_segment_map(self, segment_map):
        """Display the segment map in the segment display area"""
        lines = []
        lines.append("=" * 80)
        lines.append("SEGMENT MAP - What will be kept/cut from target files")
        lines.append("=" * 80)
        lines.append("")
        lines.append(f"Total Reference Duration: {self._format_time(segment_map.total_duration_ms)}")
        lines.append(f"Global Offset: {segment_map.global_offset_ms}ms")
        lines.append("")

        for i, segment in enumerate(segment_map.segments, 1):
            lines.append(f"Segment {i}:")
            lines.append(f"  Target File: {segment.target_file_index + 1}")
            lines.append(f"  Target Time: {self._format_time(segment.target_start_ms)} → {self._format_time(segment.target_end_ms)}")
            lines.append(f"  Maps to Ref: {self._format_time(segment.reference_start_ms)} → {self._format_time(segment.reference_end_ms)}")
            lines.append(f"  Confidence:  {segment.confidence:.3f}")
            lines.append(f"  Offset:      {segment.offset_ms}ms")
            lines.append("")

        lines.append("=" * 80)

        self.segment_display.setPlainText("\n".join(lines))

    def _format_time(self, ms):
        """Format milliseconds as HH:MM:SS.mmm"""
        total_seconds = ms / 1000.0
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        seconds = int(total_seconds % 60)
        milliseconds = int((total_seconds % 1) * 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"

    def _start_alignment(self):
        """Start the alignment operation"""
        if self.segment_map is None:
            QtWidgets.QMessageBox.warning(
                self,
                "No Analysis",
                "Please run analysis first."
            )
            return

        # Get output path
        output_dir = self.output_dir_input.text()
        output_filename = self.output_filename_input.text()

        if not output_dir:
            QtWidgets.QMessageBox.warning(self, "No Output Directory", "Please specify an output directory.")
            return

        if not output_filename:
            QtWidgets.QMessageBox.warning(self, "No Output Filename", "Please specify an output filename.")
            return

        output_path = str(Path(output_dir) / output_filename)

        # Check if output file exists
        if Path(output_path).exists():
            reply = QtWidgets.QMessageBox.question(
                self,
                "File Exists",
                f"The file '{output_filename}' already exists. Overwrite?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
            )

            if reply == QtWidgets.QMessageBox.StandardButton.No:
                return

        # Get target paths
        target_paths = []
        for i in range(self.target_list.count()):
            item = self.target_list.item(i)
            target_paths.append(item.data(QtCore.Qt.ItemDataRole.UserRole))

        # Generate commands with trim buffers
        trim_end_buffer_sec = config.DEFAULTS.get('trim_end_buffer_sec', 5.0)
        trim_start_buffer_sec = config.DEFAULTS.get('trim_start_buffer_sec', 0.0)

        commands, temp_files = core.generate_alignment_commands(
            self.segment_map,
            target_paths,
            output_path,
            trim_end_buffer_ms=int(trim_end_buffer_sec * 1000),
            trim_start_buffer_ms=int(trim_start_buffer_sec * 1000),
            use_keyframe_detection=True  # Use exact keyframe positions
        )

        self._clear_log()
        self._log("Starting alignment operation...")
        self._log(f"Output: {output_path}\n")
        self._log(f"Will execute {len(commands)} mkvmerge commands\n")

        # Disable UI
        self._set_ui_busy(True)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)

        # Start alignment worker
        self.alignment_worker = core.AlignmentWorker(commands, temp_files, output_dir)
        self.alignment_worker.line_ready.connect(self._log)
        self.alignment_worker.progress.connect(self._update_progress)
        self.alignment_worker.finished_signal.connect(self._on_alignment_finished)
        self.alignment_worker.start()

    def _on_alignment_finished(self, return_code, error_message):
        """Handle alignment completion"""
        self._set_ui_busy(False)
        self.progress_bar.setVisible(False)

        if return_code == 0:
            self._log("\n✓ Alignment completed successfully!")
            QtWidgets.QMessageBox.information(self, "Success", "Video aligned successfully!")
        elif "cancelled" in error_message.lower():
            self._log(f"\n✗ Operation cancelled")
        else:
            self._log(f"\n✗ Alignment failed: {error_message}")
            QtWidgets.QMessageBox.critical(self, "Error", f"Alignment failed:\n{error_message}")

    def _cancel_operation(self):
        """Cancel the current operation"""
        if self.analysis_worker and self.analysis_worker.isRunning():
            self._log("\nCancelling analysis...")
            self.analysis_worker.stop()

        if self.alignment_worker and self.alignment_worker.isRunning():
            self._log("\nCancelling alignment...")
            self.alignment_worker.stop()

    def _update_progress(self, percent):
        """Update the progress bar"""
        self.progress_bar.setValue(percent)

    def _set_ui_busy(self, busy):
        """Set UI to busy or idle state"""
        self.browse_ref_btn.setEnabled(not busy)
        self.clear_ref_btn.setEnabled(not busy)
        self.add_targets_btn.setEnabled(not busy)
        self.remove_targets_btn.setEnabled(not busy)
        self.clear_targets_btn.setEnabled(not busy)
        self.move_up_btn.setEnabled(not busy)
        self.move_down_btn.setEnabled(not busy)
        self.analyze_btn.setEnabled(not busy)
        self.align_btn.setEnabled(not busy and self.segment_map is not None)
        self.cancel_btn.setEnabled(busy)
        self.reference_input.setEnabled(not busy)
        self.target_list.setEnabled(not busy)
        self.language_input.setEnabled(not busy)
        self.threshold_spin.setEnabled(not busy)
        self.output_dir_input.setEnabled(not busy)
        self.output_filename_input.setEnabled(not busy)
        self.browse_dir_btn.setEnabled(not busy)

    def _log(self, message):
        """Append message to log output"""
        self.log_output.appendPlainText(message)

    def _clear_log(self):
        """Clear the log output"""
        self.log_output.clear()

    def _clear_segment_display(self):
        """Clear the segment display"""
        self.segment_display.clear()

    # Drag and Drop Support
    def dragEnterEvent(self, event):
        """Handle drag enter event"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        """Handle drop event"""
        urls = event.mimeData().urls()
        paths = [url.toLocalFile() for url in urls if url.isLocalFile()]

        if paths and len(paths) == 1:
            # Single file - assume it's the reference
            self.reference_input.setText(paths[0])
            if not self.output_dir_input.text():
                self.output_dir_input.setText(str(Path(paths[0]).parent))
        elif paths:
            # Multiple files - add as targets
            self._add_targets_to_list(paths)

    # Paste Support
    def keyPressEvent(self, event):
        """Handle key press events for paste support"""
        if event.matches(QtGui.QKeySequence.StandardKey.Paste):
            clipboard = QtWidgets.QApplication.clipboard()
            text = clipboard.text()

            if text:
                lines = text.strip().split('\n')
                paths = []

                for line in lines:
                    line = line.strip()
                    if line.startswith('"') and line.endswith('"'):
                        line = line[1:-1]
                    elif line.startswith("'") and line.endswith("'"):
                        line = line[1:-1]

                    if line and Path(line).exists():
                        paths.append(line)

                if paths:
                    self._add_targets_to_list(paths)
                else:
                    self._log("No valid file paths found in clipboard")
        else:
            super().keyPressEvent(event)

    # Settings Management
    def _load_settings(self):
        """Load settings from config"""
        settings = self.app_manager.load_config(self.tool_name, config.DEFAULTS)

        # Load reference file
        reference_file = settings.get('reference_file', '')
        if reference_file:
            self.reference_input.setText(reference_file)

        # Load target files
        target_files = settings.get('target_files', [])
        if target_files:
            self._add_targets_to_list(target_files)

        # Load output settings
        output_dir = settings.get('output_directory', '')
        if output_dir:
            self.output_dir_input.setText(output_dir)

        output_filename = settings.get('output_filename', config.DEFAULTS['output_filename'])
        self.output_filename_input.setText(output_filename)

        # Load analysis settings
        self.language_input.setText(settings.get('audio_language', config.DEFAULTS['audio_language']))
        self.threshold_spin.setValue(settings.get('correlation_threshold', config.DEFAULTS['correlation_threshold']))

    def save_settings(self):
        """Save settings to config"""
        # Get target list
        target_files = []
        for i in range(self.target_list.count()):
            item = self.target_list.item(i)
            target_files.append(item.data(QtCore.Qt.ItemDataRole.UserRole))

        settings = {
            'reference_file': self.reference_input.text(),
            'target_files': target_files,
            'output_directory': self.output_dir_input.text(),
            'output_filename': self.output_filename_input.text(),
            'audio_language': self.language_input.text(),
            'correlation_threshold': self.threshold_spin.value(),
        }

        self.app_manager.save_config(self.tool_name, settings)

    def shutdown(self):
        """Clean up resources"""
        # Stop any running workers
        if self.analysis_worker and self.analysis_worker.isRunning():
            self.analysis_worker.stop()
            self.analysis_worker.quit()
            self.analysis_worker.wait()

        if self.alignment_worker and self.alignment_worker.isRunning():
            self.alignment_worker.stop()
            self.alignment_worker.quit()
            self.alignment_worker.wait()
