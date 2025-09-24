# remux_toolkit/tools/ffmpeg_dvd_remuxer/core/worker.py
import threading
import time
from pathlib import Path
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from .orchestrator import Orchestrator
from ..utils.paths import create_output_folder

class Worker(QObject):
    log = pyqtSignal(str)
    analysis_finished = pyqtSignal(object, list)
    title_progress = pyqtSignal(object, int) # job, percent
    queue_progress = pyqtSignal(int, int) # current_job, total_jobs
    finished = pyqtSignal()

    def __init__(self, config, app_manager):
        super().__init__()
        self.config = config
        self.app_manager = app_manager
        self.stop_event = threading.Event()
        self.temp_dir = Path(self.app_manager.get_temp_dir('ffmpeg_dvd_remuxer'))
        self.orchestrator = Orchestrator(config, self.temp_dir)

    @pyqtSlot(object)
    def run_analysis(self, job):
        # This function is unchanged
        self.stop_event.clear()
        try:
            titles, message = self.orchestrator.analyze_disc(job.source_path, self.log.emit, self.stop_event)
            self.log.emit(f"For '{job.base_name}': {message}")
            self.analysis_finished.emit(job, titles)
        except Exception as e:
            self.log.emit(f"!! ANALYSIS ERROR for '{job.base_name}': {e}")
            self.analysis_finished.emit(job, [])
        finally:
            self.finished.emit()

    @pyqtSlot(list)
    def run_processing(self, jobs_to_run: list):
        self.stop_event.clear()
        total_jobs = len(jobs_to_run)
        self.queue_progress.emit(0, total_jobs)

        try:
            output_root = Path(self.config.get("default_output_directory"))
            if not output_root: raise ValueError("Output directory is not set.")

            for i, job in enumerate(jobs_to_run):
                self.queue_progress.emit(i, total_jobs)
                if self.stop_event.is_set():
                    self.log.emit("\n>> Processing stopped by user. <<")
                    break

                output_folder = create_output_folder(output_root, job.base_name, job.group_name)
                log_file_path = output_folder / f"{job.base_name}_process_{time.strftime('%Y%m%d-%H%M%S')}.log"
                with open(log_file_path, "w", encoding="utf-8", buffering=1) as log_fh:
                    def log_and_write(message: str):
                        self.log.emit(message)
                        log_fh.write(message + '\n')

                    log_and_write(f"▶ Starting job for '{job.base_name}'. Output: {output_folder}")

                    for title_num in sorted(list(job.selected_titles)):
                        if self.stop_event.is_set(): break

                        title_info = next((t for t in job.titles_info if t['title'] == str(title_num)), None)
                        context = {
                            'input_path': job.source_path, 'title_num': title_num,
                            'out_folder': output_folder, 'config': self.config,
                            'field_order': title_info.get('field_order') if title_info else None,
                            'title_info': title_info,
                        }

                        # Consume progress updates from the pipeline generator
                        for progress_update in self.orchestrator.run_pipeline(context, log_and_write, self.stop_event):
                            if isinstance(progress_update, int):
                                self.title_progress.emit(job, progress_update)

                self.title_progress.emit(job, 100)
                self.queue_progress.emit(i + 1, total_jobs)

            if not self.stop_event.is_set():
                self.log.emit("\n🎉 All jobs finished. 🎉")
        except Exception as e:
            self.log.emit(f"!! PROCESSING ERROR: {e}")
        finally:
            self.finished.emit()

    def stop(self):
        self.stop_event.set()
