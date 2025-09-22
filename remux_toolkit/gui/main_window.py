# remux_toolkit/gui/main_window.py

from PyQt6 import QtWidgets, QtGui, QtCore

# Import the manager
from remux_toolkit.core.managers import AppManager

# Import the widgets for all tools
from remux_toolkit.tools.silence_checker.silence_checker_gui import SilenceCheckerWidget
from remux_toolkit.tools.media_comparator.media_comparator_gui import MediaComparatorWidget

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Remux-Toolkit")
        self.resize(1200, 800)

        # Create a single instance of the AppManager for the whole application
        self.app_manager = AppManager()

        # Keep track of open tool widgets by a unique name
        self.open_tools = {}

        # --- Tab Widget Setup ---
        self.tab_widget = QtWidgets.QTabWidget()
        self.tab_widget.setTabsClosable(True)
        self.tab_widget.setMovable(True)
        self.tab_widget.tabCloseRequested.connect(self._close_tab)
        self.setCentralWidget(self.tab_widget)

        # --- Menu Bar Setup ---
        self._create_actions()
        self._create_menus()

    def _create_actions(self):
        """Create the menu actions for opening tools."""
        self.open_silence_checker_action = QtGui.QAction("Leading Silence Checker", self)
        self.open_silence_checker_action.triggered.connect(self.open_silence_checker)

        self.open_media_comparator_action = QtGui.QAction("Media Comparator", self)
        self.open_media_comparator_action.triggered.connect(self.open_media_comparator)

    def _create_menus(self):
        """Create the main menu bar."""
        menu_bar = self.menuBar()
        tools_menu = menu_bar.addMenu("&Tools")
        tools_menu.addAction(self.open_silence_checker_action)
        tools_menu.addAction(self.open_media_comparator_action)

    def open_silence_checker(self):
        """Opens the silence checker tool in a new tab if not already open."""
        tool_name = "SilenceChecker"
        if tool_name in self.open_tools:
            self.tab_widget.setCurrentWidget(self.open_tools[tool_name])
            return

        tool_widget = SilenceCheckerWidget(app_manager=self.app_manager)

        index = self.tab_widget.addTab(tool_widget, "Leading Silence Checker")
        self.tab_widget.setCurrentIndex(index)
        self.open_tools[tool_name] = tool_widget

    def open_media_comparator(self):
        """Opens the media comparator tool in a new tab."""
        tool_name = "MediaComparator"
        if tool_name in self.open_tools:
            self.tab_widget.setCurrentWidget(self.open_tools[tool_name])
            return

        tool_widget = MediaComparatorWidget(app_manager=self.app_manager)

        index = self.tab_widget.addTab(tool_widget, "Media Comparator")
        self.tab_widget.setCurrentIndex(index)
        self.open_tools[tool_name] = tool_widget

    def _close_tab(self, index: int):
        """Handles the closing of a tab."""
        widget_to_close = self.tab_widget.widget(index)

        if widget_to_close:
            tool_name_to_remove = None
            for name, widget in self.open_tools.items():
                if widget == widget_to_close:
                    tool_name_to_remove = name
                    break

            # Save settings before shutting down the tool
            print(f"Saving settings for {tool_name_to_remove}...")
            widget_to_close.save_settings()

            print(f"Closing tab for {tool_name_to_remove}. Shutting down worker...")
            widget_to_close.shutdown()

            self.tab_widget.removeTab(index)
            if tool_name_to_remove:
                del self.open_tools[tool_name_to_remove]

            widget_to_close.deleteLater()

    def closeEvent(self, event: QtGui.QCloseEvent):
        """Ensures all open tool threads are shut down when the main window closes."""
        print("Main window is closing. Saving all settings and shutting down threads...")
        for tool_widget in self.open_tools.values():
            tool_widget.save_settings()
            tool_widget.shutdown()
        event.accept()
