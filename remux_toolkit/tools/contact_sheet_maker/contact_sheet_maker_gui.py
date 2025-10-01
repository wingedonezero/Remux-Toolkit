# remux_toolkit/tools/contact_sheet_maker/contact_sheet_maker_gui.py

import os
from PyQt6 import QtWidgets, QtCore, QtGui
from . import contact_sheet_maker_core as core
from . import contact_sheet_maker_config as config

class ContactSheetMakerWidget(QtWidgets.QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'contact_sheet_maker'
        self.worker_thread = None
        self._init_ui()
        self._load_settings()

    def _init_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)

        # --- Left Pane (Controls) ---
        controls_widget = QtWidgets.QWidget()
        controls_layout = QtWidgets.QVBoxLayout(controls_widget)
        controls_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)

        # Inputs
        input_group = QtWidgets.QGroupBox("I/O")
        input_form = QtWidgets.QFormLayout(input_group)
        self.dir_input = QtWidgets.QLineEdit()
        self.dir_input.setPlaceholderText("Select folder containing PNGs, JPGs...")
        btn_browse_dir = QtWidgets.QPushButton("Browse...")
        btn_browse_dir.clicked.connect(self._browse_dir)
        row1 = QtWidgets.QHBoxLayout(); row1.addWidget(self.dir_input); row1.addWidget(btn_browse_dir)
        input_form.addRow("Input Folder:", row1)
        self.output_input = QtWidgets.QLineEdit()
        self.output_input.setPlaceholderText("Select output path for contact_sheet.png")
        btn_browse_out = QtWidgets.QPushButton("Browse...")
        btn_browse_out.clicked.connect(self._browse_out)
        row2 = QtWidgets.QHBoxLayout(); row2.addWidget(self.output_input); row2.addWidget(btn_browse_out)
        input_form.addRow("Output File:", row2)
        controls_layout.addWidget(input_group)

        # Settings
        settings_group = QtWidgets.QGroupBox("Settings")
        settings_form = QtWidgets.QFormLayout(settings_group)
        self.cols_spin = QtWidgets.QSpinBox(); self.cols_spin.setRange(1, 32)
        self.limit_spin = QtWidgets.QSpinBox(); self.limit_spin.setRange(0, 10000); self.limit_spin.setSpecialValueText("Unlimited")
        settings_form.addRow("Columns:", self.cols_spin)
        settings_form.addRow("Image Limit:", self.limit_spin)
        controls_layout.addWidget(settings_group)

        # Advanced Settings
        adv_group = QtWidgets.QGroupBox("Advanced Appearance")
        adv_group.setCheckable(True); adv_group.setChecked(False)
        adv_form = QtWidgets.QFormLayout(adv_group)
        self.thumb_w_spin = QtWidgets.QSpinBox(); self.thumb_w_spin.setRange(50, 2000); self.thumb_w_spin.setSuffix(" px")
        self.thumb_h_spin = QtWidgets.QSpinBox(); self.thumb_h_spin.setRange(50, 2000); self.thumb_h_spin.setSuffix(" px")
        self.label_h_spin = QtWidgets.QSpinBox(); self.label_h_spin.setRange(10, 100); self.label_h_spin.setSuffix(" px")
        self.pad_spin = QtWidgets.QSpinBox(); self.pad_spin.setRange(0, 100); self.pad_spin.setSuffix(" px")
        adv_form.addRow("Thumbnail Width:", self.thumb_w_spin)
        adv_form.addRow("Thumbnail Height:", self.thumb_h_spin)
        adv_form.addRow("Label Height:", self.label_h_spin)
        adv_form.addRow("Padding:", self.pad_spin)
        controls_layout.addWidget(adv_group)

        # Actions
        self.start_button = QtWidgets.QPushButton("Generate Contact Sheet")
        self.start_button.clicked.connect(self.start_generation)
        self.progress_bar = QtWidgets.QProgressBar(); self.progress_bar.setVisible(False)
        self.status_label = QtWidgets.QLabel("Ready")
        controls_layout.addWidget(self.start_button)
        controls_layout.addWidget(self.progress_bar)
        controls_layout.addWidget(self.status_label)
        splitter.addWidget(controls_widget)

        # --- Right Pane (Preview) ---
        preview_group = QtWidgets.QGroupBox("Preview")
        preview_layout = QtWidgets.QVBoxLayout(preview_group)
        self.preview_label = QtWidgets.QLabel("Preview will appear here after generation.")
        self.preview_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        scroll_area = QtWidgets.QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(self.preview_label)
        preview_layout.addWidget(scroll_area)
        splitter.addWidget(preview_group)

        splitter.setSizes([400, 800])
        layout.addWidget(splitter)

    def _browse_dir(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Image Folder")
        if path:
            self.dir_input.setText(path)
            self.output_input.setText(os.path.join(path, "contact_sheet.png"))

    def _browse_out(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Contact Sheet", self.output_input.text(), "PNG Images (*.png)")
        if path:
            # --- FIX ---
            # Automatically append .png if the user doesn't type it.
            if not path.lower().endswith('.png'):
                path += '.png'
            # --- END FIX ---
            self.output_input.setText(path)

    def _gather_params(self):
        return {
            'png_dir': self.dir_input.text(),
            'out': self.output_input.text(),
            'cols': self.cols_spin.value(),
            'limit': self.limit_spin.value(),
            'thumb_w': self.thumb_w_spin.value(),
            'thumb_h': self.thumb_h_spin.value(),
            'label_h': self.label_h_spin.value(),
            'pad': self.pad_spin.value(),
        }

    def start_generation(self):
        params = self._gather_params()
        if not params['png_dir'] or not params['out']:
            QtWidgets.QMessageBox.warning(self, "Input Missing", "Please specify both an input folder and an output file.")
            return

        self.start_button.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.status_label.setText("Starting...")

        worker = core.Worker()
        self.worker_thread = QtCore.QThread()
        worker.moveToThread(self.worker_thread)
        worker.progress.connect(self.update_progress)
        worker.finished.connect(self.on_finished)
        self.worker_thread.started.connect(lambda: worker.make_sheet(params))
        self.worker_thread.start()

    def update_progress(self, current, total):
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(current)
        self.status_label.setText(f"Processing image {current} of {total}...")

    def on_finished(self, result_path, success):
        self.status_label.setText(result_path if success else f"Error: {result_path}")
        self.start_button.setEnabled(True)
        self.progress_bar.setVisible(False)

        if success:
            pixmap = QtGui.QPixmap(result_path)
            self.preview_label.setPixmap(pixmap.scaled(self.preview_label.size(),
                                                       QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                                                       QtCore.Qt.TransformationMode.SmoothTransformation))
        else:
            self.preview_label.setText(f"Failed to generate sheet.\n\nError:\n{result_path}")

        self.worker_thread.quit()
        self.worker_thread.wait()

    def _load_settings(self):
        settings = self.app_manager.load_config(self.tool_name, config.DEFAULTS)
        self.dir_input.setText(settings.get('input_dir', config.DEFAULTS['input_dir']))
        self.output_input.setText(settings.get('output_file', config.DEFAULTS['output_file']))
        self.cols_spin.setValue(settings.get('cols', config.DEFAULTS['cols']))
        self.limit_spin.setValue(settings.get('limit', config.DEFAULTS['limit']))
        self.thumb_w_spin.setValue(settings.get('thumb_w', config.DEFAULTS['thumb_w']))
        self.thumb_h_spin.setValue(settings.get('thumb_h', config.DEFAULTS['thumb_h']))
        self.label_h_spin.setValue(settings.get('label_h', config.DEFAULTS['label_h']))
        self.pad_spin.setValue(settings.get('pad', config.DEFAULTS['pad']))

    def save_settings(self):
        settings = {
            'input_dir': self.dir_input.text(),
            'output_file': self.output_input.text(),
            'cols': self.cols_spin.value(),
            'limit': self.limit_spin.value(),
            'thumb_w': self.thumb_w_spin.value(),
            'thumb_h': self.thumb_h_spin.value(),
            'label_h': self.label_h_spin.value(),
            'pad': self.pad_spin.value(),
        }
        self.app_manager.save_config(self.tool_name, settings)

    def shutdown(self):
        if self.worker_thread and self.worker_thread.isRunning():
            self.worker_thread.quit()
            self.worker_thread.wait(2000)
