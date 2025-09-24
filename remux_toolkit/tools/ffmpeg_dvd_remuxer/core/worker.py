# remux_toolkit/tools/ffmpeg_dvd_remuxer/core/worker.py
import threading
from pathlib import Path
from PyQt6.QtCore import QObject, pyqtSignal

from .orchestrator import Orchestrator
from ..utils.paths import create_output_folder

class Worker(QObject):
    log = pyqtSignal(str)
    # The signal now sends the original job object back
    analysis_finished = pyqtSignal(object, list)
    progress = pyqtSignal(int, int)
    finished = pyqtSignal()

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.stop_event = threading.Event()
        self.orchestrator = Orchestrator(config, self.log)

    def run_analysis(self, job):
        self.stop_event.clear()
        try:
            titles, message = self.orchestrator.analyze_disc(job.source_path, self.stop_event)
            self.log.emit(f"For '{job.base_name}': {message}")
            self.analysis_finished.emit(job, titles)
        except Exception as e:
            self.log.emit(f"!! ANALYSIS ERROR for '{job.base_name}': {e}")
            self.analysis_finished.emit(job, [])
        finally:
            self.finished.emit()

    def run_processing(self, jobs_to_run: list):
        self.stop_event.clear()
        total_jobs = len(jobs_to_run)
        self.progress.emit(0, total_jobs)

        try:
            output_root = Path(self.config.get("default_output_directory"))
            if not output_root: raise ValueError("Output directory is not set.")

            for i, job in enumerate(jobs_to_run):
                if self.stop_event.is_set():
                    self.log.emit("\n>> Processing stopped by user. <<")
                    break

                output_folder = create_output_folder(output_root, job.base_name, job.group_name)
                self.log.emit(f"▶ Starting job for '{job.base_name}'. Output: {output_folder}")

                # Use the selected_titles set from the job object
                for title_num in sorted(list(job.selected_titles)):
                    if self.stop_event.is_set(): break

                    context = {
                        'input_path': job.source_path,
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
