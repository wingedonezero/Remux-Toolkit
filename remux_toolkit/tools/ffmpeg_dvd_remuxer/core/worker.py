# remux_toolkit/tools/ffmpeg_dvd_remuxer/core/worker.py
import threading
from pathlib import Path
from PyQt6.QtCore import QObject, pyqtSignal

from .orchestrator import Orchestrator
from ..utils.paths import create_output_folder

class Worker(QObject):
    log = pyqtSignal(str)
    analysis_finished = pyqtSignal(list)
    progress = pyqtSignal(int, int)
    finished = pyqtSignal()

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.stop_event = threading.Event()
        self.orchestrator = Orchestrator(config, self.log)

    def run_analysis(self, input_path_str: str):
        self.stop_event.clear()
        try:
            titles, message = self.orchestrator.analyze_disc(Path(input_path_str), self.stop_event)
            self.log.emit(message)
            self.analysis_finished.emit(titles)
        except Exception as e:
            self.log.emit(f"!! ANALYSIS ERROR: {e}")
            self.analysis_finished.emit([])
        self.finished.emit()

    def run_processing(self, jobs: list): # Expects a list of Job objects
        self.stop_event.clear()
        total_jobs = len(jobs)
        self.progress.emit(0, total_jobs)

        try:
            output_root = Path(self.config.get("default_output_directory"))
            if not output_root: raise ValueError("Output directory is not set.")

            for i, job in enumerate(jobs):
                if self.stop_event.is_set():
                    self.log.emit("\n>> Processing stopped by user. <<")
                    break

                output_folder = create_output_folder(output_root, job.base_name, job.group_name)
                self.log.emit(f"▶ Starting job for '{job.base_name}'. Output: {output_folder}")

                for title_num in job.titles_to_process:
                    if self.stop_event.is_set(): break

                    context = {
                        'input_path': job.input_path,
                        'title_num': title_num,
                        'out_folder': output_folder,
                        'config': self.config
                    }
                    self.orchestrator.run_pipeline(context, self.stop_event)

                self.progress.emit(i + 1, total_jobs)

            if not self.stop_event.is_set():
                self.log.emit("\n🎉 All jobs finished. 🎉")

        except Exception as e:
            self.log.emit(f"!! PROCESSING ERROR: {e}")
        finally:
            self.finished.emit()

    def stop(self):
        self.stop_event.set()
