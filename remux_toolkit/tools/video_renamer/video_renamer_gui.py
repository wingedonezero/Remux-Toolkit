from pathlib import Path
from typing import List
import os

from PyQt6 import QtWidgets
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QFileDialog, QHeaderView, QProgressBar,
    QMessageBox, QComboBox, QSlider, QSpinBox, QGroupBox, QDialog, QDialogButtonBox,
    QTreeWidget, QTreeWidgetItem
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QColor, QBrush

from .core.pipeline import MatchingPipeline
from .core.cache import MediaCache
from . import video_renamer_config as config

class MatcherThread(QThread):
    progress = pyqtSignal(str, int)
    match_list_found = pyqtSignal(dict)
    unused_ref_found = pyqtSignal(dict)
    finished = pyqtSignal()

    def __init__(self, pipeline, references, remuxes):
        super().__init__()
        self.pipeline = pipeline
        self.references = references
        self.remuxes = remuxes
        self._stop_requested = False

    def run(self):
        try:
            for result in self.pipeline.match(self.references, self.remuxes):
                if self._stop_requested: break
                if result['type'] == 'progress': self.progress.emit(result['message'], result['value'])
                elif result['type'] == 'match_list': self.match_list_found.emit(result['data'])
                elif result['type'] == 'unused_ref': self.unused_ref_found.emit(result['data'])
        except Exception as e: self.progress.emit(f"Error: {str(e)}", 0)
        finally: self.finished.emit()

    def stop(self):
        self._stop_requested = True
        self.pipeline.stop()

class SettingsDialog(QDialog):
    def __init__(self, current_settings, parent=None):
        super().__init__(parent)
        self.current_settings = current_settings
        self.setWindowTitle("Renamer Settings")
        self.setMinimumWidth(500)
        layout = QVBoxLayout(self)
        general_group = QGroupBox("General Matching Settings")
        form_layout = QtWidgets.QFormLayout(general_group)
        self.offset_spinbox = QSpinBox()
        self.offset_spinbox.setRange(0, 40)
        self.offset_spinbox.setSuffix(" %")
        form_layout.addRow("Analysis Start Offset (%):", self.offset_spinbox)
        self.workers_spinbox = QSpinBox()
        max_threads = os.cpu_count() * 2 if os.cpu_count() else 24
        self.workers_spinbox.setRange(1, max_threads)
        self.workers_spinbox.setSuffix(" threads")
        form_layout.addRow("Number of Workers:", self.workers_spinbox)
        layout.addWidget(general_group)
        panako_group = QGroupBox("Panako Fingerprinter (Deprecated)")
        panako_layout = QHBoxLayout(panako_group)
        self.panako_path_edit = QLineEdit()
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(lambda: self._browse("Select Panako JAR", "JAR Files (*.jar)", self.panako_path_edit))
        panako_layout.addWidget(QtWidgets.QLabel("panako.jar Path:"))
        panako_layout.addWidget(self.panako_path_edit)
        panako_layout.addWidget(browse_btn)
        layout.addWidget(panako_group)
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
        self._load_settings()
    def _browse(self, title, file_filter, line_edit):
        file_path, _ = QFileDialog.getOpenFileName(self, title, "", file_filter)
        if file_path: line_edit.setText(file_path)
    def _load_settings(self):
        self.panako_path_edit.setText(self.current_settings.get('panako_jar', ''))
        self.offset_spinbox.setValue(self.current_settings.get('analysis_start_percent', 15))
        self.workers_spinbox.setValue(self.current_settings.get('num_workers', 8))
    def get_settings(self) -> dict:
        return {'panako_jar': self.panako_path_edit.text(), 'analysis_start_percent': self.offset_spinbox.value(), 'num_workers': self.workers_spinbox.value()}

class VideoRenamerWidget(QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'video_renamer'
        class TempConfig:
            def get(self, key, default=None): return default
        self.app_data_dir = Path(self.app_manager.get_temp_dir(self.tool_name))
        self.cache = MediaCache()
        self.pipeline = MatchingPipeline(self.cache, TempConfig(), self.app_data_dir)
        self.matcher_thread = None
        self._init_ui()
        self._load_settings()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        folder_group = QGroupBox("Folder Selection")
        folder_layout = QtWidgets.QFormLayout(folder_group)
        self.ref_folder = QLineEdit()
        ref_btn = QPushButton("Browse...")
        ref_btn.clicked.connect(lambda: self._select_folder(self.ref_folder))
        ref_row = QHBoxLayout(); ref_row.addWidget(self.ref_folder); ref_row.addWidget(ref_btn)
        self.remux_folder = QLineEdit()
        remux_btn = QPushButton("Browse...")
        remux_btn.clicked.connect(lambda: self._select_folder(self.remux_folder))
        remux_row = QHBoxLayout(); remux_row.addWidget(self.remux_folder); remux_row.addWidget(remux_btn)
        folder_layout.addRow("Reference (Correctly Named):", ref_row)
        folder_layout.addRow("Remux (To Rename):", remux_row)
        layout.addWidget(folder_group)
        config_group = QGroupBox("Matching Configuration")
        config_layout = QHBoxLayout(config_group)
        config_layout.addWidget(QLabel("Mode:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems([
            "VideoHash (Video)", "Correlation (Audio)", "Chromaprint (Audio)",
            "Peak Matcher (Audio)", "Invariant Matcher (Audio)", "MFCC (Audio)",
            "Perceptual Hash (Video)", "Scene Detection (Video)"
        ])
        config_layout.addWidget(self.mode_combo)
        self.lang_label = QLabel("Language:"); config_layout.addWidget(self.lang_label)
        self.lang_input = QLineEdit(); self.lang_input.setMaximumWidth(60); self.lang_input.setPlaceholderText("jpn"); config_layout.addWidget(self.lang_input)
        config_layout.addWidget(QLabel("Min Confidence:"))
        self.confidence_slider = QSlider(Qt.Orientation.Horizontal); self.confidence_slider.setRange(50, 95); self.confidence_slider.setValue(75); self.confidence_slider.setTickPosition(QSlider.TickPosition.TicksBelow); self.confidence_slider.setTickInterval(5); config_layout.addWidget(self.confidence_slider)
        self.confidence_label = QLabel("75%"); self.confidence_slider.valueChanged.connect(lambda v: self.confidence_label.setText(f"{v}%")); config_layout.addWidget(self.confidence_label)
        config_layout.addStretch()
        layout.addWidget(config_group)
        control_layout = QHBoxLayout()
        self.start_btn = QPushButton("Start Matching"); self.start_btn.clicked.connect(self.start_matching)
        self.stop_btn = QPushButton("Stop"); self.stop_btn.clicked.connect(self.stop_matching); self.stop_btn.setEnabled(False)
        self.settings_btn = QPushButton("Settings..."); self.settings_btn.clicked.connect(self._open_settings_dialog)
        self.clear_cache_btn = QPushButton("Clear Cache"); self.clear_cache_btn.clicked.connect(self._clear_cache)
        self.rename_btn = QPushButton("Rename Matched Files"); self.rename_btn.clicked.connect(self.rename_files); self.rename_btn.setEnabled(False)
        control_layout.addWidget(self.start_btn); control_layout.addWidget(self.stop_btn); control_layout.addWidget(self.settings_btn); control_layout.addWidget(self.clear_cache_btn); control_layout.addStretch(); control_layout.addWidget(self.rename_btn)
        layout.addLayout(control_layout)
        self.results_tree = QTreeWidget()
        self.results_tree.setColumnCount(4)
        self.results_tree.setHeaderLabels(["Original Name / Proposed Name", "Confidence", "Match Info", "Status"])
        self.results_tree.setSortingEnabled(True)
        header = self.results_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.results_tree, 1)
        status_layout = QHBoxLayout()
        self.status_label = QLabel("Ready")
        self.progress = QProgressBar(); self.progress.setTextVisible(True)
        status_layout.addWidget(self.status_label, 1)
        status_layout.addWidget(self.progress)
        layout.addLayout(status_layout)

    def start_matching(self):
        if self.matcher_thread and self.matcher_thread.isRunning(): return
        if not self.ref_folder.text() or not self.remux_folder.text():
            QMessageBox.warning(self, "Error", "Please select both folders"); return
        ref_files = self._get_video_files(Path(self.ref_folder.text()))
        remux_files = self._get_video_files(Path(self.remux_folder.text()))
        if not ref_files or not remux_files:
            QMessageBox.warning(self, "Error", "No video files found in one or both folders."); return
        self.status_label.setText(f"Starting {self.mode_combo.currentText()}..."); self.progress.setValue(0)
        self.start_btn.setEnabled(False); self.stop_btn.setEnabled(True); self.rename_btn.setEnabled(False)
        self.results_tree.clear()

        mode_map = {
            "VideoHash (Video)": "videohash", "Correlation (Audio)": "correlation",
            "Chromaprint (Audio)": "chromaprint", "Peak Matcher (Audio)": "peak_matcher",
            "Invariant Matcher (Audio)": "invariant_matcher", "MFCC (Audio)": "mfcc",
            "Perceptual Hash (Video)": "phash", "Scene Detection (Video)": "scene"
        }
        mode = mode_map.get(self.mode_combo.currentText(), "videohash")

        settings = self.app_manager.load_config(self.tool_name, config.DEFAULTS)
        self.pipeline.set_mode(mode)
        self.pipeline.set_language(self.lang_input.text() or None)
        self.pipeline.set_threshold(self.confidence_slider.value() / 100.0)
        self.pipeline.set_num_workers(settings.get('num_workers', 8))
        self.matcher_thread = MatcherThread(self.pipeline, ref_files, remux_files)
        self.matcher_thread.progress.connect(self.update_progress)
        self.matcher_thread.match_list_found.connect(self.add_match_list_result)
        self.matcher_thread.unused_ref_found.connect(self.add_unused_ref_result)
        self.matcher_thread.finished.connect(self.matching_finished)
        self.matcher_thread.start()

    def add_match_list_result(self, data):
        remux_path = Path(data['remux_path'])
        matches = data['matches']
        threshold = self.confidence_slider.value() / 100.0

        parent_item = QTreeWidgetItem(self.results_tree)
        parent_item.setText(0, remux_path.name)
        parent_item.setData(0, Qt.ItemDataRole.UserRole, str(remux_path))

        if not matches:
            parent_item.setText(3, "Unmatched")
            parent_item.setForeground(0, QColor('black'))
            parent_item.setBackground(0, QColor(255, 182, 193))
            return

        best_match_score = matches[0]['score']
        status = "Matched" if best_match_score >= threshold else "Low Confidence"
        color = QColor(144, 238, 144) if status == "Matched" else QColor(255, 200, 150)

        parent_item.setText(3, status)
        parent_item.setForeground(0, QColor('black'))
        parent_item.setBackground(0, color)

        for match in matches:
            ref_path = Path(match['ref'])
            conf = match['score']
            info = match.get('info', f"{self.mode_combo.currentText().split(' ')[0]} similarity")

            child_item = QTreeWidgetItem(parent_item)
            child_item.setText(0, f"  └─ {ref_path.name}")
            child_item.setText(1, f"{conf:.1%}")
            child_item.setText(2, f"{info}: {conf:.1%}")
            child_item.setData(0, Qt.ItemDataRole.UserRole, str(ref_path))

        parent_item.setExpanded(True)
        self.rename_btn.setEnabled(True)

    def add_unused_ref_result(self, data):
        ref_path = Path(data['reference_path'])
        parent_item = QTreeWidgetItem(self.results_tree)
        parent_item.setText(0, ref_path.name)
        parent_item.setText(3, "Unused")
        parent_item.setForeground(0, QColor('black'))
        parent_item.setBackground(0, QColor(220, 220, 220))

    def rename_files(self):
        rename_list = []
        for i in range(self.results_tree.topLevelItemCount()):
            parent_item = self.results_tree.topLevelItem(i)
            if parent_item.childCount() > 0:
                original_full_path = Path(parent_item.data(0, Qt.ItemDataRole.UserRole))
                best_match_child = parent_item.child(0)
                proposed_full_path = Path(best_match_child.data(0, Qt.ItemDataRole.UserRole))

                new_path = original_full_path.parent / (proposed_full_path.stem + original_full_path.suffix)
                if parent_item.text(3) == "Matched":
                    rename_list.append((original_full_path, new_path))

        if not rename_list:
            QMessageBox.information(self, "Info", "No files with a 'Matched' status to rename."); return

        msg = f"Rename {len(rename_list)} files?\n\nExamples:\n"
        for orig, new in rename_list[:3]: msg += f"{orig.name} → {new.name}\n"
        if len(rename_list) > 3: msg += f"... and {len(rename_list) - 3} more"

        reply = QMessageBox.question(self, "Confirm Rename", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes: return

        success, errors = 0, []
        for orig, new in rename_list:
            try:
                if new.exists():
                    errors.append(f"{new.name} already exists"); continue
                orig.rename(new)
                success += 1
            except Exception as e:
                errors.append(f"Error renaming {orig.name}: {e}")

        self.results_tree.clear()
        self.status_label.setText(f"Successfully renamed {success} files.")
        if errors:
            error_details = "\n".join(errors)
            QMessageBox.warning(self, "Rename Errors", f"Could not rename {len(errors)} file(s):\n{error_details}")

    def matching_finished(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_label.setText("Matching complete")
        self.progress.setValue(100)

    def _clear_cache(self):
        self.cache.clear(); self.results_tree.clear(); self.status_label.setText("Cache cleared")
    def _open_settings_dialog(self):
        current_settings = self.app_manager.load_config(self.tool_name, config.DEFAULTS)
        dialog = SettingsDialog(current_settings, self)
        if dialog.exec():
            new_settings = dialog.get_settings()
            current_settings.update(new_settings)
            self.app_manager.save_config(self.tool_name, current_settings)
            self.status_label.setText("Settings saved.")
            self._load_settings()
    def _on_mode_changed(self, mode_text):
        is_audio = "Audio" in mode_text
        self.lang_label.setVisible(is_audio); self.lang_input.setVisible(is_audio)
    def _select_folder(self, line_edit):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder")
        if folder: line_edit.setText(folder)
    def stop_matching(self):
        if self.matcher_thread:
            self.matcher_thread.stop(); self.stop_btn.setEnabled(False); self.status_label.setText("Stopping...")
    def update_progress(self, message, value):
        self.status_label.setText(message); self.progress.setValue(value)
    def _get_video_files(self, folder: Path) -> List[Path]:
        extensions = {'.mkv', '.mp4', '.avi', '.mov', 'ts', '.m2ts'}
        return sorted([p for ext in extensions for p in folder.glob(f"**/*{ext}")])
    def _load_settings(self):
        settings = self.app_manager.load_config(self.tool_name, config.DEFAULTS)
        self.ref_folder.setText(settings.get('ref_folder', ''))
        self.remux_folder.setText(settings.get('remux_folder', ''))
        self.lang_input.setText(settings.get('language', 'jpn'))
        mode_idx = self.mode_combo.findText(settings.get('mode', 'Correlation (Audio)'))
        if mode_idx >= 0: self.mode_combo.setCurrentIndex(mode_idx)
        self.confidence_slider.setValue(settings.get('confidence', 75))
        class LoadedConfig:
            def __init__(self, data): self._data = data
            def get(self, key, default=None): return self._data.get(key, default)
        self.pipeline.config = LoadedConfig(settings)
        self.pipeline.set_num_workers(settings.get('num_workers', 8))
    def save_settings(self):
        current_settings = self.app_manager.load_config(self.tool_name, config.DEFAULTS)
        dialog_settings = SettingsDialog(current_settings).get_settings()
        current_settings.update(dialog_settings)
        current_settings.update({'ref_folder': self.ref_folder.text(), 'remux_folder': self.remux_folder.text(),'language': self.lang_input.text(), 'mode': self.mode_combo.currentText(),'confidence': self.confidence_slider.value()})
        self.app_manager.save_config(self.tool_name, current_settings)
    def shutdown(self):
        if self.matcher_thread and self.matcher_thread.isRunning():
            self.matcher_thread.stop()
            self.matcher_thread.wait()
