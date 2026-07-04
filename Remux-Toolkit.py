# main.py

import os
import sys

# Pin all GPU work to the discrete GPU (device 0) before torch loads.
# On dual-GPU ROCm systems (e.g. a discrete Radeon + a Ryzen iGPU) the iGPU
# otherwise gets initialized and SIGSEGVs on the first kernel launch. Every
# subprocess inherits this; setdefault so an explicit override still wins.
os.environ.setdefault("HIP_VISIBLE_DEVICES", "0")

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
