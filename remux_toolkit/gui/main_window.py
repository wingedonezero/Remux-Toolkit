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
from remux_toolkit.tools.telecine_detector.telecine_detector_gui import TelecineDetectorWidget
# --- NEW IMPORT ---
from remux_toolkit.tools.media_info.media_info_gui import MediaInfoWidget
from remux_toolkit.tools.mkv_combiner.mkv_combiner_gui import MKVCombinerWidget
from remux_toolkit.tools.video_sync.video_sync_gui import VideoSyncWidget
from remux_toolkit.tools.audio_comparison_analysis.audio_comparison_analysis_gui import (
    AudioComparisonAnalysisWidget,
)
from remux_toolkit.tools.ffmpeg_dvd_gui.ffmpeg_dvd_gui_gui import FFmpegDVDGUIWidget
from remux_toolkit.tools.video_source_analyzer.video_source_analyzer_gui import VideoSourceAnalyzerWidget

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

        self.open_telecine_detector_action = QtGui.QAction("Telecine Detector", self)
        self.open_telecine_detector_action.triggered.connect(self.open_telecine_detector)

        # --- NEW ACTION ---
        self.open_media_info_action = QtGui.QAction("Media Info", self)
        self.open_media_info_action.triggered.connect(self.open_media_info)

        self.open_mkv_combiner_action = QtGui.QAction("MKV Combiner", self)
        self.open_mkv_combiner_action.triggered.connect(self.open_mkv_combiner)

        self.open_video_sync_action = QtGui.QAction("Video Sync (Audio Alignment)", self)
        self.open_video_sync_action.triggered.connect(self.open_video_sync)

        self.open_audio_comparison_analysis_action = QtGui.QAction("Audio Comparison Analysis", self)
        self.open_audio_comparison_analysis_action.triggered.connect(self.open_audio_comparison_analysis)

        self.open_ffmpeg_dvd_gui_action = QtGui.QAction("FFmpeg DVD Remuxer", self)
        self.open_ffmpeg_dvd_gui_action.triggered.connect(self.open_ffmpeg_dvd_gui)

        self.open_video_source_analyzer_action = QtGui.QAction("Video Source Analyzer", self)
        self.open_video_source_analyzer_action.triggered.connect(self.open_video_source_analyzer)

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
        tools_menu.addAction(self.open_telecine_detector_action)
        # --- NEW MENU ITEM ---
        tools_menu.addAction(self.open_media_info_action)
        tools_menu.addAction(self.open_mkv_combiner_action)
        tools_menu.addAction(self.open_video_sync_action)
        tools_menu.addAction(self.open_audio_comparison_analysis_action)
        tools_menu.addAction(self.open_ffmpeg_dvd_gui_action)
        tools_menu.addAction(self.open_video_source_analyzer_action)

    def open_silence_checker(self): self._open_tool("SilenceChecker", "Leading Silence Checker", SilenceCheckerWidget)
    def open_media_comparator(self): self._open_tool("MediaComparator", "Media Comparator", MediaComparatorWidget)
    def open_video_renamer(self): self._open_tool("VideoRenamer", "Video Episode Renamer", VideoRenamerWidget)
    def open_mkv_splitter(self): self._open_tool("MKVSplitter", "MKV Episode Splitter", MKVSplitterWidget)
    def open_makemkvcon_gui(self): self._open_tool("MakeMKVConGUI", "MakeMKVCon GUI", MakeMKVConGUIWidget)
    def open_ifo_reader(self): self._open_tool("IfoReader", "IFO Reader", IfoReaderWidget)
    def open_video_ab_comparator(self): self._open_tool("VideoABComparator", "Video A/B Comparator", VideoABComparatorWidget)
    def open_delay_inspector(self): self._open_tool("DelayInspector", "Delay Inspector", DelayInspectorWidget)
    def open_contact_sheet_maker(self): self._open_tool("ContactSheetMaker", "Contact Sheet Maker", ContactSheetMakerWidget)
    def open_telecine_detector(self): self._open_tool("TelecineDetector", "Telecine Detector", TelecineDetectorWidget)
    # --- NEW METHOD ---
    def open_media_info(self): self._open_tool("MediaInfo", "Media Info", MediaInfoWidget)
    def open_mkv_combiner(self): self._open_tool("MKVCombiner", "MKV Combiner", MKVCombinerWidget)
    def open_video_sync(self): self._open_tool("VideoSync", "Video Sync", VideoSyncWidget)
    def open_audio_comparison_analysis(self): self._open_tool(
        "AudioComparisonAnalysis", "Audio Comparison Analysis", AudioComparisonAnalysisWidget
    )
    def open_ffmpeg_dvd_gui(self): self._open_tool("FFmpegDVDGUI", "FFmpeg DVD Remuxer", FFmpegDVDGUIWidget)
    def open_video_source_analyzer(self): self._open_tool("VideoSourceAnalyzer", "Video Source Analyzer", VideoSourceAnalyzerWidget)

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

        try:
            if hasattr(widget_to_close, 'save_settings'):
                print(f"Saving settings for {tool_name_to_remove}...")
                widget_to_close.save_settings()
        except Exception as e:
            print(f"Error saving settings for {tool_name_to_remove}: {e}")

        try:
            if hasattr(widget_to_close, 'shutdown'):
                print(f"Closing tab for {tool_name_to_remove}. Shutting down worker...")
                widget_to_close.shutdown()
        except Exception as e:
            print(f"Error shutting down {tool_name_to_remove}: {e}")

        self.tab_widget.removeTab(index)
        if tool_name_to_remove:
            del self.open_tools[tool_name_to_remove]
        widget_to_close.deleteLater()

    def closeEvent(self, event: QtGui.QCloseEvent):
        print("Main window is closing. Saving all settings and shutting down threads...")
        # Close all tabs properly - iterate in reverse to avoid index shifting
        while self.tab_widget.count() > 0:
            widget = self.tab_widget.widget(0)
            tool_name = next((name for name, w in self.open_tools.items() if w == widget), None)
            try:
                if hasattr(widget, 'save_settings'):
                    widget.save_settings()
            except Exception as e:
                print(f"Error saving settings for {tool_name}: {e}")
            try:
                if hasattr(widget, 'shutdown'):
                    widget.shutdown()
            except Exception as e:
                print(f"Error shutting down {tool_name}: {e}")
            self.tab_widget.removeTab(0)
            if tool_name:
                self.open_tools.pop(tool_name, None)
            widget.deleteLater()
        self.open_tools.clear()
        event.accept()
