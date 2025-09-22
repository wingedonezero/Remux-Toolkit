# main.py

import sys
from PyQt6.QtWidgets import QApplication
from remux_toolkit.gui.main_window import MainWindow

def main():
    """Application entry point."""
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    main()
