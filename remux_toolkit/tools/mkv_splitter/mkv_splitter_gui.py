# remux_toolkit/tools/mkv_splitter/mkv_splitter_gui.py

import os
import shlex
import subprocess
from PyQt6 import QtWidgets, QtCore, QtGui

from . import mkv_splitter_core as core
from . import mkv_splitter_config as config

def parse_target_duration(text):
    """Parse 'mm:ss' (e.g. '23:40') or plain/decimal minutes ('23', '23.5') to float minutes."""
    text = text.strip()
    if not text:
        raise ValueError("Target episode duration is empty. Use mm:ss, e.g. 23:40.")
    try:
        if ':' in text:
            m, s = text.split(':', 1)
            minutes = int(m) + int(s) / 60.0
        else:
            minutes = float(text)
    except ValueError:
        raise ValueError(f"Invalid target duration '{text}'. Use mm:ss, e.g. 23:40.")
    if minutes <= 0:
        raise ValueError("Target episode duration must be greater than zero.")
    return minutes

def format_target_duration(minutes):
    """Format float minutes as 'mm:ss' for display (23.6667 -> '23:40')."""
    m = int(minutes)
    s = round((minutes - m) * 60)
    if s == 60:
        m, s = m + 1, 0
    return f"{m}:{s:02d}"

class DropLineEdit(QtWidgets.QLineEdit):
    """Path box that accepts an MKV file or a folder dragged onto it."""
    pathsDropped = QtCore.pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    @staticmethod
    def _local_paths(mime):
        if not mime.hasUrls():
            return []
        return [u.toLocalFile() for u in mime.urls() if u.isLocalFile() and u.toLocalFile()]

    def dragEnterEvent(self, event):
        if self._local_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._local_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        paths = self._local_paths(event.mimeData())
        if not paths:
            # Plain text drag - let QLineEdit do its normal thing.
            super().dropEvent(event)
            return
        self.setText(paths[0])
        event.acceptProposedAction()
        self.pathsDropped.emit(paths)


class AnalysisWorker(QtCore.QThread):
    """Worker to handle the file analysis in the background."""
    # Emits: mkv_info dict, analysis log string, split_points list
    result = QtCore.pyqtSignal(dict, str, list)
    error = QtCore.pyqtSignal(str)

    def __init__(self, file_path, min_duration, num_episodes, analysis_mode, target_duration,
                 manual_chapters=None, manual_timestamps=""):
        super().__init__()
        self.file_path = file_path
        self.min_duration = min_duration
        self.num_episodes = num_episodes
        self.analysis_mode = analysis_mode
        self.target_duration = target_duration
        self.manual_chapters = manual_chapters
        self.manual_timestamps = manual_timestamps

    def run(self):
        try:
            mkv_info, error = core.get_mkv_info(self.file_path)
            if error:
                raise RuntimeError(error)

            log, split_points = core.analyze_chapters(
                mkv_info, self.min_duration, self.num_episodes,
                self.analysis_mode, self.target_duration,
                manual_chapters=self.manual_chapters,
                manual_timestamps=self.manual_timestamps,
            )
            self.result.emit(mkv_info, log, split_points)
        except Exception as e:
            self.error.emit(str(e))

class ExecutionWorker(QtCore.QThread):
    """Worker to execute the mkvmerge command and stream its output."""
    line_ready = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal(int)

    def __init__(self, command, parent=None):
        super().__init__(parent)
        self.command = command
        self._process = None
        self._stopped = False

    def stop(self):
        """Request the worker to stop and kill the mkvmerge subprocess."""
        self._stopped = True
        proc = self._process
        if proc:
            try:
                proc.kill()
            except OSError:
                pass

    def run(self):
        try:
            # Popen with settings for real-time text output
            self._process = subprocess.Popen(
                shlex.split(self.command),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1
            )
            # Read output line by line as it comes in
            for line in iter(self._process.stdout.readline, ''):
                if self._stopped:
                    break
                self.line_ready.emit(line.strip())

            self._process.stdout.close()
            if self._stopped:
                self._process.kill()
                self._process.wait()
                return
            return_code = self._process.wait()
            self.finished.emit(return_code)
        except Exception as e:
            if not self._stopped:
                self.line_ready.emit(f"FATAL EXECUTION ERROR: {e}")
                self.finished.emit(-1)
        finally:
            self._process = None

class BatchAnalysisWorker(QtCore.QThread):
    """Worker to analyze multiple MKV files for batch mode."""
    result = QtCore.pyqtSignal(str, list)  # log, list of (file_path, command) tuples
    error = QtCore.pyqtSignal(str)

    def __init__(self, file_paths, chapters_to_remove):
        super().__init__()
        self.file_paths = file_paths
        self.chapters_to_remove = chapters_to_remove

    def run(self):
        try:
            log_lines = []
            commands = []
            for file_path in self.file_paths:
                fname = os.path.basename(file_path)
                log_lines.append(f"--- Analyzing: {fname} ---")
                mkv_info, err = core.get_mkv_info(file_path)
                if err:
                    log_lines.append(f"  ❌ Error: {err}")
                    continue

                analysis_log, split_points = core.analyze_chapters(
                    mkv_info, 15.0, self.chapters_to_remove,
                    "Remove Chapters from End", 23.0
                )
                log_lines.append(analysis_log)

                if split_points:
                    cmd = core.generate_mkvmerge_command(file_path, split_points, [])
                    commands.append((file_path, cmd))
                    log_lines.append(f"  ✅ Command generated")
                else:
                    log_lines.append(f"  ⚠️ No split points generated")
                log_lines.append("")

            log_lines.append(f"\n--- BATCH SUMMARY: {len(commands)}/{len(self.file_paths)} files ready ---")
            self.result.emit("\n".join(log_lines), commands)
        except Exception as e:
            self.error.emit(str(e))


class BatchExecutionWorker(QtCore.QThread):
    """Worker to execute multiple mkvmerge commands sequentially for batch mode."""
    line_ready = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal(int)

    def __init__(self, commands, parent=None):
        super().__init__(parent)
        self.commands = commands  # list of (file_path, command_string) tuples
        self._process = None
        self._stopped = False

    def stop(self):
        self._stopped = True
        proc = self._process
        if proc:
            try:
                proc.kill()
            except OSError:
                pass

    def run(self):
        total = len(self.commands)
        failed = 0
        for idx, (file_path, command) in enumerate(self.commands, 1):
            if self._stopped:
                break
            self.line_ready.emit(f"\n--- [{idx}/{total}] Processing: {os.path.basename(file_path)} ---")
            self.line_ready.emit(f"Command: {command}\n")
            try:
                self._process = subprocess.Popen(
                    shlex.split(command),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    bufsize=1
                )
                for line in iter(self._process.stdout.readline, ''):
                    if self._stopped:
                        break
                    self.line_ready.emit(line.strip())

                self._process.stdout.close()
                if self._stopped:
                    self._process.kill()
                    self._process.wait()
                    return
                return_code = self._process.wait()
                if return_code != 0:
                    failed += 1
                    self.line_ready.emit(f"  ⚠️ Exit code: {return_code}")
                else:
                    self.line_ready.emit(f"  ✅ Done")
            except Exception as e:
                if not self._stopped:
                    self.line_ready.emit(f"  FATAL ERROR: {e}")
                    failed += 1
            finally:
                self._process = None

        if not self._stopped:
            self.line_ready.emit(f"\n--- BATCH COMPLETE: {total - failed}/{total} succeeded ---")
            self.finished.emit(0 if failed == 0 else 1)


class MKVSplitterWidget(QtWidgets.QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'mkv_splitter'
        self.analysis_worker = None
        self.execution_worker = None
        self.analysis_results = {}
        self._batch_commands = []  # for batch mode
        self._chapter_rows = []
        self._populating_chapters = False
        self._init_ui()
        self._load_settings()

    def _init_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        main_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)

        # --- Top Pane (Inputs & Analysis) ---
        top_pane = QtWidgets.QWidget()
        top_layout = QtWidgets.QVBoxLayout(top_pane)

        input_group = QtWidgets.QGroupBox("Input")
        input_layout = QtWidgets.QHBoxLayout(input_group)
        self.file_path_input = DropLineEdit()
        self.file_path_input.setPlaceholderText("Drag an MKV here, or select/paste a path...")
        self.file_path_input.pathsDropped.connect(self._on_paths_dropped)
        browse_btn = QtWidgets.QPushButton("Browse File...")
        browse_btn.clicked.connect(self._select_file)
        self.browse_folder_btn = QtWidgets.QPushButton("Browse Folder...")
        self.browse_folder_btn.clicked.connect(self._select_folder)
        self.browse_folder_btn.setVisible(False)
        input_layout.addWidget(self.file_path_input)
        input_layout.addWidget(browse_btn)
        input_layout.addWidget(self.browse_folder_btn)
        top_layout.addWidget(input_group)

        analysis_group = QtWidgets.QGroupBox("Analysis Configuration")
        analysis_layout = QtWidgets.QFormLayout(analysis_group)
        self.analysis_mode_combo = QtWidgets.QComboBox()
        self.analysis_modes = ["Time-based Grouping", "Pattern Recognition", "Statistical Gap Analysis",
                               "Shortest Chapter Analysis", "Manual Episode Count", "Remove Chapters from End",
                               core.MANUAL_CHAPTERS_MODE, core.MANUAL_TIMESTAMPS_MODE]
        self.analysis_mode_combo.addItems(self.analysis_modes)
        self.analysis_mode_combo.currentTextChanged.connect(self._on_mode_changed)
        analysis_layout.addRow("Analysis Mode:", self.analysis_mode_combo)

        self.params_stack = QtWidgets.QStackedWidget()
        self.target_duration_input = QtWidgets.QLineEdit()
        self.target_duration_input.setPlaceholderText("mm:ss, e.g. 23:40")
        self.target_duration_input.setValidator(QtGui.QRegularExpressionValidator(QtCore.QRegularExpression(r"\d{1,3}(:[0-5]?\d?|\.\d{0,3})?")))
        self.min_duration_input = QtWidgets.QDoubleSpinBox(); self.min_duration_input.setSuffix(" min"); self.min_duration_input.setRange(0.1, 240)
        self.num_episodes_input = QtWidgets.QSpinBox(); self.num_episodes_input.setRange(2, 100)
        self.chapters_from_end_input = QtWidgets.QSpinBox(); self.chapters_from_end_input.setRange(1, 100)

        param_layout1 = QtWidgets.QFormLayout(); param_layout1.addRow("Target Episode Duration (mm:ss):", self.target_duration_input); w1 = QtWidgets.QWidget(); w1.setLayout(param_layout1)
        param_layout2 = QtWidgets.QFormLayout(); param_layout2.addRow("Min Content Duration:", self.min_duration_input); w2 = QtWidgets.QWidget(); w2.setLayout(param_layout2)
        param_layout3 = QtWidgets.QFormLayout(); param_layout3.addRow("Expected # of Episodes:", self.num_episodes_input); w3 = QtWidgets.QWidget(); w3.setLayout(param_layout3)
        param_layout4 = QtWidgets.QFormLayout(); param_layout4.addRow("Chapters to Remove from End:", self.chapters_from_end_input); w4 = QtWidgets.QWidget(); w4.setLayout(param_layout4)

        # Page 5: Before Chapters (Manual) - the picking happens in the chapter table
        param_layout5 = QtWidgets.QVBoxLayout()
        chapters_hint = QtWidgets.QLabel(
            "Analyze the file, then tick chapters in the Chapters table to split before them."
        )
        chapters_hint.setWordWrap(True)
        chapter_btn_row = QtWidgets.QHBoxLayout()
        self.chapters_all_btn = QtWidgets.QPushButton("Select All Chapters")
        self.chapters_all_btn.clicked.connect(lambda: self._set_all_chapter_checks(True))
        self.chapters_none_btn = QtWidgets.QPushButton("Clear Selection")
        self.chapters_none_btn.clicked.connect(lambda: self._set_all_chapter_checks(False))
        chapter_btn_row.addWidget(self.chapters_all_btn)
        chapter_btn_row.addWidget(self.chapters_none_btn)
        chapter_btn_row.addStretch()
        param_layout5.addWidget(chapters_hint)
        param_layout5.addLayout(chapter_btn_row)
        w5 = QtWidgets.QWidget(); w5.setLayout(param_layout5)

        # Page 6: After Timestamps (Manual)
        param_layout6 = QtWidgets.QFormLayout()
        self.timestamps_input = QtWidgets.QLineEdit()
        self.timestamps_input.setPlaceholderText("e.g. 21:30, 44:10.500  (HH:MM:SS, MM:SS, or 90s)")
        self.timestamps_input.setToolTip(
            "mkvmerge starts a new file once the stream reaches each timestamp, at the\n"
            "next key frame. Same as mkvtoolnix's 'After specific timestamps'.\n"
            "Double-click a row in the Chapters table to paste that chapter's start time."
        )
        self.timestamps_input.textChanged.connect(self._on_timestamps_changed)
        param_layout6.addRow("Split after timestamps:", self.timestamps_input)
        w6 = QtWidgets.QWidget(); w6.setLayout(param_layout6)

        self.params_stack.addWidget(w1); self.params_stack.addWidget(w2); self.params_stack.addWidget(w3); self.params_stack.addWidget(w4)
        self.params_stack.addWidget(w5); self.params_stack.addWidget(w6)
        analysis_layout.addRow(self.params_stack)

        self.analyze_button = QtWidgets.QPushButton("Analyze File")
        self.analyze_button.clicked.connect(self.start_analysis)
        analysis_layout.addRow(self.analyze_button)
        top_layout.addWidget(analysis_group)
        main_splitter.addWidget(top_pane)

        # --- Bottom Pane (Results) ---
        bottom_pane = QtWidgets.QWidget()
        bottom_layout = QtWidgets.QVBoxLayout(bottom_pane)

        results_group = QtWidgets.QGroupBox("Results")
        results_layout = QtWidgets.QVBoxLayout(results_group)

        results_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)

        tracks_widget = QtWidgets.QWidget()
        tracks_layout = QtWidgets.QVBoxLayout(tracks_widget)
        tracks_layout.setContentsMargins(0,0,0,0)
        tracks_layout.addWidget(QtWidgets.QLabel("Detected Tracks:"))
        self.track_table = QtWidgets.QTableWidget()
        self.track_table.setColumnCount(4)
        self.track_table.setHorizontalHeaderLabels(["ID", "Type", "Codec", "Language"])
        self.track_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.track_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.track_table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.track_table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.track_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        tracks_layout.addWidget(self.track_table)
        results_splitter.addWidget(tracks_widget)

        self.chapters_widget = QtWidgets.QWidget()
        chapters_layout = QtWidgets.QVBoxLayout(self.chapters_widget)
        chapters_layout.setContentsMargins(0, 0, 0, 0)
        self.chapters_label = QtWidgets.QLabel("Chapters:")
        chapters_layout.addWidget(self.chapters_label)
        self.chapter_table = QtWidgets.QTableWidget()
        self.chapter_table.setColumnCount(4)
        self.chapter_table.setHorizontalHeaderLabels(["Chapter", "Start", "Duration", "Title"])
        ch_hdr = self.chapter_table.horizontalHeader()
        ch_hdr.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        ch_hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        ch_hdr.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        ch_hdr.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.chapter_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.chapter_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.chapter_table.itemChanged.connect(self._on_chapter_item_changed)
        self.chapter_table.itemDoubleClicked.connect(self._on_chapter_double_clicked)
        chapters_layout.addWidget(self.chapter_table)
        self.chapters_widget.setVisible(False)
        results_splitter.addWidget(self.chapters_widget)

        log_widget = QtWidgets.QWidget()
        log_layout = QtWidgets.QVBoxLayout(log_widget)
        log_layout.setContentsMargins(0,0,0,0)
        log_layout.addWidget(QtWidgets.QLabel("Log / Output:"))
        self.log_output = QtWidgets.QPlainTextEdit(); self.log_output.setReadOnly(True)
        log_layout.addWidget(self.log_output)
        results_splitter.addWidget(log_widget)

        results_splitter.setSizes([250, 320, 400])
        results_layout.addWidget(results_splitter)

        command_row = QtWidgets.QHBoxLayout()
        self.final_command_output = QtWidgets.QLineEdit(); self.final_command_output.setReadOnly(True)
        self.generate_btn = QtWidgets.QPushButton("Generate Command"); self.generate_btn.clicked.connect(self._generate_command)
        self.execute_btn = QtWidgets.QPushButton("Execute Command"); self.execute_btn.clicked.connect(self._execute_command)
        copy_btn = QtWidgets.QPushButton("Copy"); copy_btn.clicked.connect(self._copy_command)

        command_row.addWidget(self.final_command_output, 1)
        command_row.addWidget(self.generate_btn)
        command_row.addWidget(self.execute_btn)
        command_row.addWidget(copy_btn)

        form_layout = QtWidgets.QFormLayout()
        form_layout.addRow("Generated Command:", command_row)
        results_layout.addLayout(form_layout)

        bottom_layout.addWidget(results_group)
        main_splitter.addWidget(bottom_pane)

        layout.addWidget(main_splitter)
        self._on_mode_changed(self.analysis_modes[0])

    def _set_controls_enabled(self, enabled):
        """Helper to enable/disable controls during operations."""
        self.analyze_button.setEnabled(enabled)
        self.generate_btn.setEnabled(enabled)
        self.execute_btn.setEnabled(enabled)

    def _is_manual_mode(self, mode=None):
        mode = mode if mode is not None else self.analysis_mode_combo.currentText()
        return mode in (core.MANUAL_CHAPTERS_MODE, core.MANUAL_TIMESTAMPS_MODE)

    def _on_mode_changed(self, mode):
        if mode == "Time-based Grouping": self.params_stack.setCurrentIndex(0)
        elif mode == "Manual Episode Count": self.params_stack.setCurrentIndex(2)
        elif mode == "Remove Chapters from End": self.params_stack.setCurrentIndex(3)
        elif mode == core.MANUAL_CHAPTERS_MODE: self.params_stack.setCurrentIndex(4)
        elif mode == core.MANUAL_TIMESTAMPS_MODE: self.params_stack.setCurrentIndex(5)
        else: self.params_stack.setCurrentIndex(1)

        # Show/hide folder browse and update placeholder based on mode
        is_remove_mode = mode == "Remove Chapters from End"
        self.browse_folder_btn.setVisible(is_remove_mode)
        if is_remove_mode:
            self.file_path_input.setPlaceholderText("Drag an MKV or folder here, or select/paste a path...")
            self.analyze_button.setText("Analyze")
        else:
            self.file_path_input.setPlaceholderText("Drag an MKV here, or select/paste a path...")
            self.analyze_button.setText("Analyze File")

        # The chapter table only means something in the manual modes.
        self.chapters_widget.setVisible(self._is_manual_mode(mode))
        if mode == core.MANUAL_CHAPTERS_MODE:
            self.chapters_label.setText("Chapters (tick to split before):")
        elif mode == core.MANUAL_TIMESTAMPS_MODE:
            self.chapters_label.setText("Chapters (double-click to use a start time):")
        self._apply_chapter_check_mode()

    def _on_paths_dropped(self, paths):
        """A file or folder was dragged onto the path box."""
        path = paths[0]
        notes = []
        if len(paths) > 1:
            notes.append(f"{len(paths)} items dropped - using the first: {os.path.basename(path)}")
        if os.path.isdir(path) and self.analysis_mode_combo.currentText() != "Remove Chapters from End":
            notes.append("This is a folder. Folder input only works in 'Remove Chapters from End' mode.")
        elif not os.path.isdir(path) and not path.lower().endswith('.mkv'):
            notes.append(f"'{os.path.basename(path)}' is not an .mkv - analysis will probably fail.")
        if not notes:
            what = "folder" if os.path.isdir(path) else "file"
            notes.append(f"Loaded {what}: {os.path.basename(path) or path}")
        # Always replace - a log about the previous input is stale now.
        self.log_output.setPlainText("\n".join(notes))

    # ---- chapter table -----------------------------------------------------
    def _populate_chapter_table(self, mkv_info):
        self._chapter_rows = core.build_chapter_rows(mkv_info) if mkv_info else []
        self._populating_chapters = True
        try:
            self.chapter_table.setRowCount(0)
            for chapter in self._chapter_rows:
                row = self.chapter_table.rowCount()
                self.chapter_table.insertRow(row)

                num_item = QtWidgets.QTableWidgetItem(str(chapter['num']))
                num_item.setData(QtCore.Qt.ItemDataRole.UserRole, chapter['start_str'])
                self.chapter_table.setItem(row, 0, num_item)
                self.chapter_table.setItem(row, 1, QtWidgets.QTableWidgetItem(chapter['start_str']))
                self.chapter_table.setItem(row, 2, QtWidgets.QTableWidgetItem(f"{chapter['duration_min']:.2f} min"))
                self.chapter_table.setItem(row, 3, QtWidgets.QTableWidgetItem(chapter['title'] or ""))
        finally:
            self._populating_chapters = False
        self._apply_chapter_check_mode()

    def _apply_chapter_check_mode(self):
        """Checkboxes only exist in 'Before Chapters' mode, and never on a 0:00 chapter."""
        checkable = self.analysis_mode_combo.currentText() == core.MANUAL_CHAPTERS_MODE
        self._populating_chapters = True
        try:
            for row, chapter in enumerate(self._chapter_rows):
                item = self.chapter_table.item(row, 0)
                if item is None:
                    continue
                flags = item.flags() & ~QtCore.Qt.ItemFlag.ItemIsUserCheckable
                if checkable and chapter['start_min'] > 0:
                    item.setFlags(flags | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                    if item.checkState() not in (QtCore.Qt.CheckState.Checked,
                                                 QtCore.Qt.CheckState.Unchecked):
                        item.setCheckState(QtCore.Qt.CheckState.Unchecked)
                else:
                    item.setFlags(flags)
                    item.setData(QtCore.Qt.ItemDataRole.CheckStateRole, None)
                    if checkable:
                        item.setToolTip("Starts at 0:00 - mkvmerge never splits here.")
        finally:
            self._populating_chapters = False

    def _set_all_chapter_checks(self, checked):
        if self.analysis_mode_combo.currentText() != core.MANUAL_CHAPTERS_MODE:
            return
        state = QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked
        self._populating_chapters = True
        try:
            for row in range(self.chapter_table.rowCount()):
                item = self.chapter_table.item(row, 0)
                if item is not None and item.flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable:
                    item.setCheckState(state)
        finally:
            self._populating_chapters = False
        self._generate_command()

    def _checked_chapters(self):
        nums = []
        for row in range(self.chapter_table.rowCount()):
            item = self.chapter_table.item(row, 0)
            if item is None or not (item.flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable):
                continue
            if item.checkState() == QtCore.Qt.CheckState.Checked:
                try:
                    nums.append(int(item.text()))
                except ValueError:
                    continue
        return nums

    def _on_chapter_item_changed(self, item):
        if self._populating_chapters or item.column() != 0:
            return
        self._generate_command()

    def _on_chapter_double_clicked(self, item):
        """In timestamps mode, append the double-clicked chapter's start time."""
        if self.analysis_mode_combo.currentText() != core.MANUAL_TIMESTAMPS_MODE:
            return
        row_item = self.chapter_table.item(item.row(), 0)
        stamp = row_item.data(QtCore.Qt.ItemDataRole.UserRole) if row_item else None
        if not stamp:
            return
        existing = self.timestamps_input.text().strip()
        self.timestamps_input.setText(f"{existing}, {stamp}" if existing else stamp)

    def _on_timestamps_changed(self, _text):
        if self.analysis_mode_combo.currentText() == core.MANUAL_TIMESTAMPS_MODE:
            self._generate_command()

    def _select_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select MKV File", "", "MKV Files (*.mkv)")
        if path: self.file_path_input.setText(path)

    def _select_folder(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Folder of MKV Files")
        if path: self.file_path_input.setText(path)

    def start_analysis(self):
        path = self.file_path_input.text()
        if not path or not os.path.exists(path):
            self.log_output.setPlainText("Error: Please select a valid path.")
            return

        current_mode = self.analysis_mode_combo.currentText()

        # Handle folder input for "Remove Chapters from End" mode
        if os.path.isdir(path):
            if current_mode != "Remove Chapters from End":
                self.log_output.setPlainText("Error: Folder input is only supported in 'Remove Chapters from End' mode.")
                return
            mkv_files = sorted([
                os.path.join(path, f) for f in os.listdir(path)
                if f.lower().endswith('.mkv')
            ])
            if not mkv_files:
                self.log_output.setPlainText("Error: No MKV files found in the selected folder.")
                return
            self._start_batch_analysis(mkv_files)
            return

        self.log_output.setPlainText("Analyzing, please wait...")
        self.final_command_output.clear()
        self.track_table.setRowCount(0)
        self._populate_chapter_table(None)
        self._batch_commands = []
        self._set_controls_enabled(False)

        # Pass chapters_from_end via num_episodes slot for the new mode
        num_episodes_val = self.chapters_from_end_input.value() if current_mode == "Remove Chapters from End" else self.num_episodes_input.value()

        try:
            target_duration_min = parse_target_duration(self.target_duration_input.text())
        except ValueError as e:
            if current_mode == "Time-based Grouping":
                self.log_output.setPlainText(f"Error: {e}")
                self._set_controls_enabled(True)
                return
            target_duration_min = config.DEFAULTS['target_duration']

        self.analysis_worker = AnalysisWorker(
            path, self.min_duration_input.value(),
            num_episodes_val,
            current_mode,
            target_duration_min,
            manual_chapters=(self._checked_chapters()
                             if current_mode == core.MANUAL_CHAPTERS_MODE else None),
            manual_timestamps=(self.timestamps_input.text()
                               if current_mode == core.MANUAL_TIMESTAMPS_MODE else ""),
        )
        self.analysis_worker.result.connect(self._on_analysis_result)
        self.analysis_worker.error.connect(self._on_analysis_error)
        # --- FIX: Ensure the worker thread is cleaned up properly ---
        self.analysis_worker.finished.connect(self._on_analysis_finished)
        self.analysis_worker.start()

    def _on_analysis_finished(self):
        """Safely cleans up the analysis worker thread."""
        self._set_controls_enabled(True)
        if self.analysis_worker:
            self.analysis_worker.quit()
            self.analysis_worker.wait()

    def _start_batch_analysis(self, mkv_files):
        self.log_output.setPlainText(f"Analyzing {len(mkv_files)} MKV files, please wait...")
        self.final_command_output.clear()
        self.track_table.setRowCount(0)
        self._batch_commands = []
        self.analysis_results = {}
        self._set_controls_enabled(False)

        self.analysis_worker = BatchAnalysisWorker(
            mkv_files, self.chapters_from_end_input.value()
        )
        self.analysis_worker.result.connect(self._on_batch_analysis_result)
        self.analysis_worker.error.connect(self._on_analysis_error)
        self.analysis_worker.finished.connect(self._on_analysis_finished)
        self.analysis_worker.start()

    def _on_batch_analysis_result(self, log, commands):
        self._batch_commands = commands
        self.log_output.setPlainText(log)
        if commands:
            self.final_command_output.setText(f"[Batch: {len(commands)} commands ready — click Execute to run all]")
        else:
            self.final_command_output.setText("")

    def _on_analysis_result(self, mkv_info, log, split_points):
        self.analysis_results = {'mkv_info': mkv_info, 'split_points': split_points}
        self._batch_commands = []
        self.log_output.setPlainText(log)
        self._populate_track_table(mkv_info.get('tracks', []))
        self._populate_chapter_table(mkv_info)
        self._generate_command()

    def _populate_track_table(self, tracks):
        self.track_table.setRowCount(0)
        for track in tracks:
            row = self.track_table.rowCount()
            self.track_table.insertRow(row)

            tid = track.get('id', -1)
            ttype = track.get('type', 'unknown').capitalize()
            codec = track.get('codec', 'N/A')
            lang = track.get('properties', {}).get('language', 'und')

            id_item = QtWidgets.QTableWidgetItem(str(tid)); id_item.setFlags(id_item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
            self.track_table.setItem(row, 0, id_item)
            type_item = QtWidgets.QTableWidgetItem(ttype); type_item.setFlags(type_item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
            self.track_table.setItem(row, 1, type_item)
            codec_item = QtWidgets.QTableWidgetItem(codec); codec_item.setFlags(codec_item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
            self.track_table.setItem(row, 2, codec_item)
            lang_item = QtWidgets.QTableWidgetItem(lang)
            self.track_table.setItem(row, 3, lang_item)

    def _on_analysis_error(self, error_msg):
        self.log_output.setPlainText(error_msg)

    def _current_split_points(self):
        """
        Split points for the current mode, as (points, split_kind, error).

        The manual modes read live from the chapter table / timestamps box so
        ticking a chapter or editing a timestamp updates the command without
        re-analysing the file.
        """
        mode = self.analysis_mode_combo.currentText()
        kind = core.split_kind_for_mode(mode)

        if mode == core.MANUAL_CHAPTERS_MODE:
            return self._checked_chapters(), kind, None

        if mode == core.MANUAL_TIMESTAMPS_MODE:
            text = self.timestamps_input.text().strip()
            if not text:
                return [], kind, None
            try:
                stamps, _seconds, _warnings = core.parse_timestamp_list(text)
            except ValueError as e:
                return [], kind, str(e)
            return stamps, kind, None

        return self.analysis_results.get('split_points', []), kind, None

    def _generate_command(self):
        if not self.analysis_results: return
        track_mods = []
        original_tracks = self.analysis_results.get('mkv_info', {}).get('tracks', [])
        for row in range(self.track_table.rowCount()):
            try:
                tid = int(self.track_table.item(row, 0).text())
                new_lang = self.track_table.item(row, 3).text().strip()
                original_lang = next((t.get('properties', {}).get('language', 'und') for t in original_tracks if t.get('id') == tid), 'und')
                if new_lang != original_lang:
                    track_mods.append({'tid': tid, 'language': new_lang})
            except (ValueError, AttributeError): continue

        split_points, split_kind, error = self._current_split_points()
        if error:
            self.final_command_output.setText("")
            self.final_command_output.setPlaceholderText(error)
            return
        self.final_command_output.setPlaceholderText("")

        command = core.generate_mkvmerge_command(
            self.file_path_input.text(),
            split_points,
            track_mods,
            split_kind
        )
        self.final_command_output.setText(command)

    def _execute_command(self):
        # Batch mode execution
        if self._batch_commands:
            self.log_output.setPlainText(f"--- EXECUTING BATCH ({len(self._batch_commands)} files) ---\n")
            self._set_controls_enabled(False)

            self.execution_worker = BatchExecutionWorker(self._batch_commands)
            self.execution_worker.line_ready.connect(lambda line: self.log_output.appendPlainText(line))
            self.execution_worker.finished.connect(self._on_execution_finished)
            self.execution_worker.start()
            return

        # Single file execution
        command = self.final_command_output.text()
        if not command:
            self.log_output.setPlainText("No command to execute. Please analyze a file and generate a command first.")
            return

        self.log_output.setPlainText(f"--- EXECUTING COMMAND ---\n{command}\n\n")
        self._set_controls_enabled(False)

        self.execution_worker = ExecutionWorker(command)
        self.execution_worker.line_ready.connect(lambda line: self.log_output.appendPlainText(line))
        self.execution_worker.finished.connect(self._on_execution_finished)
        self.execution_worker.start()

    def _on_execution_finished(self, return_code):
        self.log_output.appendPlainText(f"\n--- EXECUTION FINISHED (Exit Code: {return_code}) ---")
        self._set_controls_enabled(True)
        # --- FIX: Properly quit and wait for the thread to terminate before it's garbage collected ---
        if self.execution_worker:
            self.execution_worker.quit()
            self.execution_worker.wait()

    def _copy_command(self):
        if self.final_command_output.text():
            QtWidgets.QApplication.clipboard().setText(self.final_command_output.text())

    def _load_settings(self):
        settings = self.app_manager.load_config(self.tool_name, config.DEFAULTS)
        self.file_path_input.setText(settings.get('file_path', ''))
        self.analysis_mode_combo.setCurrentText(settings.get('analysis_mode', config.DEFAULTS['analysis_mode']))
        self.target_duration_input.setText(format_target_duration(settings.get('target_duration', config.DEFAULTS['target_duration'])))
        self.min_duration_input.setValue(settings.get('min_duration', config.DEFAULTS['min_duration']))
        self.num_episodes_input.setValue(settings.get('num_episodes', config.DEFAULTS['num_episodes']))
        self.chapters_from_end_input.setValue(settings.get('chapters_from_end', config.DEFAULTS['chapters_from_end']))
        self.timestamps_input.setText(settings.get('timestamps', config.DEFAULTS['timestamps']))
        self._on_mode_changed(self.analysis_mode_combo.currentText())

    def save_settings(self):
        try:
            target_duration_min = parse_target_duration(self.target_duration_input.text())
        except ValueError:
            target_duration_min = config.DEFAULTS['target_duration']
        settings = {
            'file_path': self.file_path_input.text(),
            'analysis_mode': self.analysis_mode_combo.currentText(),
            'target_duration': target_duration_min,
            'min_duration': self.min_duration_input.value(),
            'num_episodes': self.num_episodes_input.value(),
            'chapters_from_end': self.chapters_from_end_input.value(),
            'timestamps': self.timestamps_input.text(),
        }
        self.app_manager.save_config(self.tool_name, settings)

    def shutdown(self):
        if self.analysis_worker and self.analysis_worker.isRunning():
            if hasattr(self.analysis_worker, 'stop'):
                self.analysis_worker.stop()
            self.analysis_worker.quit()
            if not self.analysis_worker.wait(3000):
                self.analysis_worker.terminate()
                self.analysis_worker.wait()
        if self.execution_worker and self.execution_worker.isRunning():
            self.execution_worker.stop()
            self.execution_worker.quit()
            if not self.execution_worker.wait(3000):
                self.execution_worker.terminate()
                self.execution_worker.wait()
        self.analysis_worker = None
        self.execution_worker = None
