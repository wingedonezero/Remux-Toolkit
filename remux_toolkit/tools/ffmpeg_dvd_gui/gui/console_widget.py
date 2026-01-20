# remux_toolkit/tools/ffmpeg_dvd_gui/gui/console_widget.py

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton, QCheckBox
from PyQt6.QtGui import QTextCharFormat, QColor, QTextCursor
from PyQt6.QtCore import Qt


class FilterableConsole(QWidget):
    """
    Console widget with message filtering and color coding.
    """
    def __init__(self, parent=None):
        super().__init__(parent)

        self.show_info = True
        self.show_warnings = True
        self.show_errors = True

        self.all_messages = []

        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        filter_layout = QHBoxLayout()

        self.chk_errors = QCheckBox("Errors")
        self.chk_errors.setChecked(True)
        self.chk_errors.stateChanged.connect(self._on_filter_changed)

        self.chk_warnings = QCheckBox("Warnings")
        self.chk_warnings.setChecked(True)
        self.chk_warnings.stateChanged.connect(self._on_filter_changed)

        self.chk_info = QCheckBox("Info")
        self.chk_info.setChecked(True)
        self.chk_info.stateChanged.connect(self._on_filter_changed)

        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self.clear)

        filter_layout.addWidget(self.chk_errors)
        filter_layout.addWidget(self.chk_warnings)
        filter_layout.addWidget(self.chk_info)
        filter_layout.addStretch()
        filter_layout.addWidget(self.btn_clear)

        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setPlaceholderText("FFmpeg output will appear here...")

        layout.addLayout(filter_layout)
        layout.addWidget(self.text_edit)

        self.formats = {
            "error": self._create_format(QColor(255, 100, 100)),
            "warning": self._create_format(QColor(255, 200, 100)),
            "info": self._create_format(QColor(200, 200, 200)),
            "success": self._create_format(QColor(100, 255, 100)),
        }

    def _create_format(self, color: QColor) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(color)
        return fmt

    def _on_filter_changed(self):
        self.show_errors = self.chk_errors.isChecked()
        self.show_warnings = self.chk_warnings.isChecked()
        self.show_info = self.chk_info.isChecked()
        self._refresh_display()

    def _refresh_display(self):
        self.text_edit.clear()
        for severity, text in self.all_messages:
            if self._should_show(severity):
                self._append_with_format(text, severity)

    def _should_show(self, severity: str) -> bool:
        if severity == "error":
            return self.show_errors
        elif severity == "warning":
            return self.show_warnings
        else:
            return self.show_info

    def _append_with_format(self, text: str, severity: str):
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text + "\n", self.formats.get(severity, self.formats["info"]))
        self.text_edit.setTextCursor(cursor)
        self.text_edit.ensureCursorVisible()

    def append(self, text: str, severity: str = "info"):
        self.all_messages.append((severity, text))
        if self._should_show(severity):
            self._append_with_format(text, severity)

    def clear(self):
        self.text_edit.clear()
        self.all_messages.clear()

    def setReadOnly(self, readonly: bool):
        self.text_edit.setReadOnly(readonly)

    def setPlaceholderText(self, text: str):
        self.text_edit.setPlaceholderText(text)
