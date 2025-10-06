# remux_toolkit/gui/main_window.py

from PyQt6 import QtWidgets, QtGui, QtCore
from remux_toolkit.core.managers import AppManager
from remux_toolkit.tools.silence_checker.silence_checker_gui import SilenceCheckerWidget
from remux_toolkit.tools.media_comparator.media_comparator_gui import MediaComparatorWidget
from remux_toolkit.tools.video_renamer.video_renamer_gui import VideoRenamerWidget
from remux_toolkit.tools.mkv_splitter.mkv_splitter_gui import MKVSplitterWidget
from remux_toolkit.tools.makemkvcon_gui.makemkvcon_gui_gui import MakeMKVConGUIWidget
from remux_toolkit.tools.ifo_reader.ifo_reader_gui import IfoReaderWidget
from remux_toolkit.tools.video_ab_comparator.video_ab_comparator_gui import VideoABComparatorWidget
from remux_toolkit.tools.delay_inspector.delay_inspector_gui import DelayInspectorWidget
from remux_toolkit.tools.contact_sheet_maker.contact_sheet_maker_gui import ContactSheetMakerWidget
# --- NEW IMPORT ---
from remux_toolkit.tools.telecine_detector.telecine_detector_gui import TelecineDetectorWidget

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Remux-Toolkit")
        self.resize(1400, 900)
        self.app_manager = AppManager()
        self.open_tools = {}
        self.tab_widget = QtWidgets.QTabWidget()
        self.tab_widget.setTabsClosable(True)
        self.tab_widget.setMovable(True)
        self.tab_widget.tabCloseRequested.connect(self._close_tab)
        self.setCentralWidget(self.tab_widget)
        self._create_actions()
        self._create_menus()

    def _create_actions(self):
        self.open_silence_checker_action = QtGui.QAction("Leading Silence Checker", self)
        self.open_silence_checker_action.triggered.connect(self.open_silence_checker)

        self.open_media_comparator_action = QtGui.QAction("Media Comparator", self)
        self.open_media_comparator_action.triggered.connect(self.open_media_comparator)

        self.open_video_renamer_action = QtGui.QAction("Video Episode Renamer", self)
        self.open_video_renamer_action.triggered.connect(self.open_video_renamer)

        self.open_mkv_splitter_action = QtGui.QAction("MKV Episode Splitter", self)
        self.open_mkv_splitter_action.triggered.connect(self.open_mkv_splitter)

        self.open_makemkvcon_gui_action = QtGui.QAction("MakeMKVCon GUI", self)
        self.open_makemkvcon_gui_action.triggered.connect(self.open_makemkvcon_gui)

        self.open_ifo_reader_action = QtGui.QAction("IFO Reader", self)
        self.open_ifo_reader_action.triggered.connect(self.open_ifo_reader)

        self.open_video_ab_comparator_action = QtGui.QAction("Video A/B Comparator", self)
        self.open_video_ab_comparator_action.triggered.connect(self.open_video_ab_comparator)

        self.open_delay_inspector_action = QtGui.QAction("Delay Inspector", self)
        self.open_delay_inspector_action.triggered.connect(self.open_delay_inspector)

        self.open_contact_sheet_maker_action = QtGui.QAction("Contact Sheet Maker", self)
        self.open_contact_sheet_maker_action.triggered.connect(self.open_contact_sheet_maker)

        # --- NEW ACTION ---
        self.open_telecine_detector_action = QtGui.QAction("Telecine Detector", self)
        self.open_telecine_detector_action.triggered.connect(self.open_telecine_detector)


    def _create_menus(self):
        menu_bar = self.menuBar()
        tools_menu = menu_bar.addMenu("&Tools")
        tools_menu.addAction(self.open_silence_checker_action)
        tools_menu.addAction(self.open_media_comparator_action)
        tools_menu.addAction(self.open_video_renamer_action)
        tools_menu.addAction(self.open_mkv_splitter_action)
        tools_menu.addAction(self.open_makemkvcon_gui_action)
        tools_menu.addAction(self.open_ifo_reader_action)
        tools_menu.addAction(self.open_video_ab_comparator_action)
        tools_menu.addAction(self.open_delay_inspector_action)
        tools_menu.addAction(self.open_contact_sheet_maker_action)
        # --- NEW MENU ITEM ---
        tools_menu.addAction(self.open_telecine_detector_action)


    def open_silence_checker(self): self._open_tool("SilenceChecker", "Leading Silence Checker", SilenceCheckerWidget)
    def open_media_comparator(self): self._open_tool("MediaComparator", "Media Comparator", MediaComparatorWidget)
    def open_video_renamer(self): self._open_tool("VideoRenamer", "Video Episode Renamer", VideoRenamerWidget)
    def open_mkv_splitter(self): self._open_tool("MKVSplitter", "MKV Episode Splitter", MKVSplitterWidget)
    def open_makemkvcon_gui(self): self._open_tool("MakeMKVConGUI", "MakeMKVCon GUI", MakeMKVConGUIWidget)
    def open_ifo_reader(self): self._open_tool("IfoReader", "IFO Reader", IfoReaderWidget)
    def open_video_ab_comparator(self): self._open_tool("VideoABComparator", "Video A/B Comparator", VideoABComparatorWidget)
    def open_delay_inspector(self): self._open_tool("DelayInspector", "Delay Inspector", DelayInspectorWidget)
    def open_contact_sheet_maker(self): self._open_tool("ContactSheetMaker", "Contact Sheet Maker", ContactSheetMakerWidget)
    # --- NEW METHOD ---
    def open_telecine_detector(self): self._open_tool("TelecineDetector", "Telecine Detector", TelecineDetectorWidget)


    def _open_tool(self, tool_name, tab_title, widget_class):
        if tool_name in self.open_tools:
            self.tab_widget.setCurrentWidget(self.open_tools[tool_name])
            return

        tool_widget = widget_class(app_manager=self.app_manager)
        index = self.tab_widget.addTab(tool_widget, tab_title)
        self.tab_widget.setCurrentIndex(index)
        self.open_tools[tool_name] = tool_widget

    def _close_tab(self, index: int):
        widget_to_close = self.tab_widget.widget(index)
        if not widget_to_close: return

        tool_name_to_remove = next((name for name, widget in self.open_tools.items() if widget == widget_to_close), None)

        if hasattr(widget_to_close, 'save_settings'):
            print(f"Saving settings for {tool_name_to_remove}...")
            widget_to_close.save_settings()

        if hasattr(widget_to_close, 'shutdown'):
            print(f"Closing tab for {tool_name_to_remove}. Shutting down worker...")
            widget_to_close.shutdown()

        self.tab_widget.removeTab(index)
        if tool_name_to_remove:
            del self.open_tools[tool_name_to_remove]
        widget_to_close.deleteLater()

    def closeEvent(self, event: QtGui.QCloseEvent):
        print("Main window is closing. Saving all settings and shutting down threads...")
        for tool_widget in list(self.open_tools.values()):
            if hasattr(tool_widget, 'save_settings'):
                tool_widget.save_settings()
            if hasattr(tool_widget, 'shutdown'):
                tool_widget.shutdown()
        event.accept()
