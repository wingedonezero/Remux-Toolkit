"""
GUI for MKV Combiner Tool
"""

import os
from pathlib import Path
from PyQt6 import QtWidgets, QtCore, QtGui
from . import mkv_combiner_config as config
from . import mkv_combiner_core as core


class MKVCombinerWidget(QtWidgets.QWidget):
    """
    Widget for combining multiple MKV files into one using mkvmerge.
    Supports drag-drop, copy-paste, and manual file selection.
    """

    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'mkv_combiner'

        # Workers
        self.analysis_worker = None
        self.combine_worker = None

        self._init_ui()
        self._load_settings()

        # Enable drag and drop
        self.setAcceptDrops(True)

    def _init_ui(self):
        """Initialize the user interface"""
        main_layout = QtWidgets.QVBoxLayout(self)

        # Create splitter for resizable sections
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)

        # Top section: File management
        top_widget = self._create_file_section()
        splitter.addWidget(top_widget)

        # Bottom section: Log output
        bottom_widget = self._create_log_section()
        splitter.addWidget(bottom_widget)

        # Set initial splitter sizes
        splitter.setSizes([400, 300])

        main_layout.addWidget(splitter)

    def _create_file_section(self):
        """Create the file management section"""
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)

        # File list label
        self.file_count_label = QtWidgets.QLabel("Files to Combine (0/5)")
        self.file_count_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.file_count_label)

        # File list widget (with drag-drop reordering)
        self.file_list = QtWidgets.QListWidget()
        self.file_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.file_list.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.InternalMove)
        self.file_list.setAcceptDrops(True)
        self.file_list.itemSelectionChanged.connect(self._update_ui_state)
        layout.addWidget(self.file_list)

        # File management buttons
        button_layout = QtWidgets.QHBoxLayout()

        self.add_files_btn = QtWidgets.QPushButton("Add Files")
        self.add_files_btn.clicked.connect(self._add_files)
        button_layout.addWidget(self.add_files_btn)

        self.remove_btn = QtWidgets.QPushButton("Remove Selected")
        self.remove_btn.clicked.connect(self._remove_selected)
        button_layout.addWidget(self.remove_btn)

        self.clear_btn = QtWidgets.QPushButton("Clear All")
        self.clear_btn.clicked.connect(self._clear_all)
        button_layout.addWidget(self.clear_btn)

        self.move_up_btn = QtWidgets.QPushButton("↑ Move Up")
        self.move_up_btn.clicked.connect(self._move_up)
        button_layout.addWidget(self.move_up_btn)

        self.move_down_btn = QtWidgets.QPushButton("↓ Move Down")
        self.move_down_btn.clicked.connect(self._move_down)
        button_layout.addWidget(self.move_down_btn)

        button_layout.addStretch()
        layout.addLayout(button_layout)

        # Paste instruction label
        paste_label = QtWidgets.QLabel("Tip: Paste file paths here (Ctrl+V) or drag files onto the window")
        paste_label.setStyleSheet("color: gray; font-style: italic;")
        layout.addWidget(paste_label)

        # Output settings
        output_group = QtWidgets.QGroupBox("Output Settings")
        output_layout = QtWidgets.QVBoxLayout(output_group)

        # Output directory
        dir_layout = QtWidgets.QHBoxLayout()
        dir_layout.addWidget(QtWidgets.QLabel("Output Directory:"))
        self.output_dir_input = QtWidgets.QLineEdit()
        self.output_dir_input.setPlaceholderText("Auto-set to first file's directory")
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
        self.output_filename_input.setText("combined_output.mkv")
        self.output_filename_input.textChanged.connect(self._update_output_preview)
        file_layout.addWidget(self.output_filename_input, 1)
        output_layout.addLayout(file_layout)

        # Full output path preview
        self.output_preview_label = QtWidgets.QLabel("Full Path: ")
        self.output_preview_label.setWordWrap(True)
        self.output_preview_label.setStyleSheet("color: #0066cc;")
        output_layout.addWidget(self.output_preview_label)

        layout.addWidget(output_group)

        # Control buttons
        control_layout = QtWidgets.QHBoxLayout()

        self.combine_btn = QtWidgets.QPushButton("Combine Files")
        self.combine_btn.setEnabled(False)
        self.combine_btn.setStyleSheet("font-weight: bold; padding: 8px;")
        self.combine_btn.clicked.connect(self._start_combine)
        control_layout.addWidget(self.combine_btn)

        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_operation)
        control_layout.addWidget(self.cancel_btn)

        control_layout.addStretch()
        layout.addLayout(control_layout)

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

    def _add_files(self):
        """Open file dialog to add MKV files"""
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "Select MKV Files",
            "",
            "MKV Files (*.mkv);;All Files (*)"
        )

        if files:
            self._add_paths_to_list(files)

    def _add_paths_to_list(self, paths):
        """Add file paths to the list widget"""
        max_files = config.DEFAULTS['max_files']
        current_count = self.file_list.count()

        for path in paths:
            # Check max files limit
            if current_count >= max_files:
                self._log(f"Maximum file limit reached ({max_files} files)")
                QtWidgets.QMessageBox.warning(
                    self,
                    "Maximum Files",
                    f"Cannot add more than {max_files} files."
                )
                break

            # Validate it's an MKV file
            if not path.lower().endswith('.mkv'):
                self._log(f"Skipping non-MKV file: {path}")
                continue

            # Check if already in list
            already_exists = False
            for i in range(self.file_list.count()):
                if self.file_list.item(i).data(QtCore.Qt.ItemDataRole.UserRole) == path:
                    already_exists = True
                    break

            if already_exists:
                self._log(f"File already in list: {Path(path).name}")
                continue

            # Add to list
            item = QtWidgets.QListWidgetItem(Path(path).name)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path)
            self.file_list.addItem(item)
            current_count += 1

            # Set output directory to first file's directory
            if self.file_list.count() == 1:
                self.output_dir_input.setText(str(Path(path).parent))

        self._update_file_count()
        self._update_ui_state()
        self._log(f"Added {len(paths)} file(s)")

    def _remove_selected(self):
        """Remove selected items from the list"""
        for item in self.file_list.selectedItems():
            self.file_list.takeItem(self.file_list.row(item))

        self._update_file_count()
        self._update_ui_state()

    def _clear_all(self):
        """Clear all files from the list"""
        self.file_list.clear()
        self._update_file_count()
        self._update_ui_state()

    def _move_up(self):
        """Move selected item up in the list"""
        selected_items = self.file_list.selectedItems()
        if not selected_items:
            return

        item = selected_items[0]
        row = self.file_list.row(item)

        if row > 0:
            self.file_list.takeItem(row)
            self.file_list.insertItem(row - 1, item)
            self.file_list.setCurrentItem(item)

    def _move_down(self):
        """Move selected item down in the list"""
        selected_items = self.file_list.selectedItems()
        if not selected_items:
            return

        item = selected_items[0]
        row = self.file_list.row(item)

        if row < self.file_list.count() - 1:
            self.file_list.takeItem(row)
            self.file_list.insertItem(row + 1, item)
            self.file_list.setCurrentItem(item)

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
        count = self.file_list.count()
        max_files = config.DEFAULTS['max_files']
        self.file_count_label.setText(f"Files to Combine ({count}/{max_files})")

    def _update_ui_state(self):
        """Update UI element states based on current state"""
        has_files = self.file_list.count() > 0
        has_enough_files = self.file_list.count() >= 2
        has_selection = len(self.file_list.selectedItems()) > 0

        self.remove_btn.setEnabled(has_selection)
        self.clear_btn.setEnabled(has_files)
        self.move_up_btn.setEnabled(has_selection)
        self.move_down_btn.setEnabled(has_selection)
        self.combine_btn.setEnabled(has_enough_files)

    def _update_output_preview(self):
        """Update the output path preview label"""
        output_dir = self.output_dir_input.text()
        filename = self.output_filename_input.text()

        if output_dir and filename:
            full_path = str(Path(output_dir) / filename)
            self.output_preview_label.setText(f"Full Path: {full_path}")
        else:
            self.output_preview_label.setText("Full Path: ")

    def _start_combine(self):
        """Start the combine operation"""
        # Get file paths in order
        file_paths = []
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            file_paths.append(item.data(QtCore.Qt.ItemDataRole.UserRole))

        if len(file_paths) < 2:
            QtWidgets.QMessageBox.warning(
                self,
                "Not Enough Files",
                "Please select at least 2 files to combine."
            )
            return

        # Get output path
        output_dir = self.output_dir_input.text()
        output_filename = self.output_filename_input.text()

        if not output_dir:
            QtWidgets.QMessageBox.warning(
                self,
                "No Output Directory",
                "Please specify an output directory."
            )
            return

        if not output_filename:
            QtWidgets.QMessageBox.warning(
                self,
                "No Output Filename",
                "Please specify an output filename."
            )
            return

        output_path = str(Path(output_dir) / output_filename)

        # Check if output file already exists
        if Path(output_path).exists():
            reply = QtWidgets.QMessageBox.question(
                self,
                "File Exists",
                f"The file '{output_filename}' already exists. Overwrite?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
            )

            if reply == QtWidgets.QMessageBox.StandardButton.No:
                return

        # Clear log and start
        self._clear_log()
        self._log("Starting combine operation...")
        self._log(f"Output: {output_path}\n")

        # Disable UI
        self._set_ui_busy(True)

        # Generate command
        command = core.generate_combine_command(file_paths, output_path)
        self._log(f"Command: {command}\n")

        # Start combine worker
        self.combine_worker = core.CombineWorker(command)
        self.combine_worker.line_ready.connect(self._log)
        self.combine_worker.progress.connect(self._update_progress)
        self.combine_worker.finished_signal.connect(self._on_combine_finished)
        self.combine_worker.start()

        # Show progress bar
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)

    def _cancel_operation(self):
        """Cancel the current operation"""
        if self.combine_worker and self.combine_worker.isRunning():
            self._log("\nCancelling operation...")
            self.combine_worker.stop()

    def _on_combine_finished(self, return_code, error_message):
        """Handle combine operation completion"""
        self._set_ui_busy(False)
        self.progress_bar.setVisible(False)

        if return_code == 0:
            self._log("\n✓ Combine operation completed successfully!")
            QtWidgets.QMessageBox.information(
                self,
                "Success",
                "Files combined successfully!"
            )
        elif return_code == -1 and "cancelled" in error_message.lower():
            self._log(f"\n✗ Operation cancelled")
        else:
            self._log(f"\n✗ Combine operation failed: {error_message}")
            QtWidgets.QMessageBox.critical(
                self,
                "Error",
                f"Combine operation failed:\n{error_message}"
            )

    def _update_progress(self, percent):
        """Update the progress bar"""
        self.progress_bar.setValue(percent)

    def _set_ui_busy(self, busy):
        """Set UI to busy or idle state"""
        self.add_files_btn.setEnabled(not busy)
        self.remove_btn.setEnabled(not busy)
        self.clear_btn.setEnabled(not busy)
        self.move_up_btn.setEnabled(not busy)
        self.move_down_btn.setEnabled(not busy)
        self.combine_btn.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)
        self.file_list.setEnabled(not busy)
        self.output_dir_input.setEnabled(not busy)
        self.output_filename_input.setEnabled(not busy)
        self.browse_dir_btn.setEnabled(not busy)

    def _log(self, message):
        """Append message to log output"""
        self.log_output.appendPlainText(message)

    def _clear_log(self):
        """Clear the log output"""
        self.log_output.clear()

    # Drag and Drop Support
    def dragEnterEvent(self, event):
        """Handle drag enter event"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        """Handle drop event"""
        urls = event.mimeData().urls()
        paths = [url.toLocalFile() for url in urls if url.isLocalFile()]

        if paths:
            self._add_paths_to_list(paths)

    # Paste Support
    def keyPressEvent(self, event):
        """Handle key press events for paste support"""
        if event.matches(QtGui.QKeySequence.StandardKey.Paste):
            clipboard = QtWidgets.QApplication.clipboard()
            text = clipboard.text()

            if text:
                # Split by newlines and filter valid paths
                lines = text.strip().split('\n')
                paths = []

                for line in lines:
                    line = line.strip()
                    # Remove quotes if present
                    if line.startswith('"') and line.endswith('"'):
                        line = line[1:-1]
                    elif line.startswith("'") and line.endswith("'"):
                        line = line[1:-1]

                    # Check if it's a valid file path
                    if line and Path(line).exists():
                        paths.append(line)

                if paths:
                    self._add_paths_to_list(paths)
                else:
                    self._log("No valid file paths found in clipboard")
        else:
            super().keyPressEvent(event)

    # Settings Management
    def _load_settings(self):
        """Load settings from config"""
        settings = self.app_manager.load_config(self.tool_name, config.DEFAULTS)

        # Load file list
        file_list = settings.get('file_list', [])
        if file_list:
            self._add_paths_to_list(file_list)

        # Load output settings
        output_dir = settings.get('output_directory', '')
        if output_dir:
            self.output_dir_input.setText(output_dir)

        output_filename = settings.get('output_filename', config.DEFAULTS['output_filename'])
        self.output_filename_input.setText(output_filename)

    def save_settings(self):
        """Save settings to config"""
        # Get file list
        file_list = []
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            file_list.append(item.data(QtCore.Qt.ItemDataRole.UserRole))

        settings = {
            'file_list': file_list,
            'output_directory': self.output_dir_input.text(),
            'output_filename': self.output_filename_input.text(),
        }

        self.app_manager.save_config(self.tool_name, settings)

    def shutdown(self):
        """Clean up resources"""
        # Stop any running workers
        if self.analysis_worker and self.analysis_worker.isRunning():
            self.analysis_worker.quit()
            self.analysis_worker.wait()

        if self.combine_worker and self.combine_worker.isRunning():
            self.combine_worker.stop()
            self.combine_worker.quit()
            self.combine_worker.wait()
