"""Worker thread for async video source analysis."""

from __future__ import annotations

import os
import time
from typing import Optional

from PyQt6 import QtCore

from .models import AnalysisResult, StreamInfo, FILM_PCT_HIGH, FILM_PCT_LOW
from .stream_info import get_stream_info
from .bitstream import run_layer1
from .pixel_analysis import run_layer2
from .field_swap import run_layer3
from .classifier import classify


class AnalysisWorker(QtCore.QObject):
    """Worker that runs the 3-layer classification pipeline on a background thread."""

    # Signals
    file_started = QtCore.pyqtSignal(str)
    layer_progress = QtCore.pyqtSignal(str, str)  # (file_path, message)
    file_finished = QtCore.pyqtSignal(str, object)  # (file_path, AnalysisResult)
    file_error = QtCore.pyqtSignal(str, str)  # (file_path, error_message)
    batch_finished = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stopped = False
        self._auto_layer2 = True
        self._auto_layer3 = True

    def stop(self):
        self._stopped = True

    def _is_cancelled(self) -> bool:
        return self._stopped

    def _progress(self, filepath: str, msg: str):
        self.layer_progress.emit(filepath, msg)

    @QtCore.pyqtSlot(list, bool, bool)
    def analyze_files(self, file_paths: list[str], auto_layer2: bool = True,
                      auto_layer3: bool = True):
        """Analyze a list of files sequentially."""
        self._stopped = False
        self._auto_layer2 = auto_layer2
        self._auto_layer3 = auto_layer3

        for filepath in file_paths:
            if self._stopped:
                break

            try:
                self.file_started.emit(filepath)
                result = self._analyze_single(filepath)
                self.file_finished.emit(filepath, result)
            except Exception as e:
                self.file_error.emit(filepath, str(e))

        self.batch_finished.emit()

    def _analyze_single(self, filepath: str) -> AnalysisResult:
        """Run the full pipeline on a single file."""
        t_total = time.time()

        result = AnalysisResult(
            file_path=filepath,
            file_name=os.path.basename(filepath),
        )

        def on_progress(msg: str):
            self._progress(filepath, msg)

        # ── Layer 0: Stream Info ───────────────────────────────────────
        on_progress("Layer 0: Getting stream info...")
        result.stream_info = get_stream_info(filepath)
        si = result.stream_info

        on_progress(
            f"{si.codec} {si.width}x{si.height} "
            f"{si.fps}fps {si.fps_mode} "
            f"{si.scan_type} {si.scan_order}"
        )

        if self._stopped:
            result.total_elapsed_sec = round(time.time() - t_total, 2)
            return result

        # ── Layer 1: Bitstream Flags ───────────────────────────────────
        if si.is_mpeg2:
            on_progress("Layer 1: Bitstream analysis...")
            result.bitstream = run_layer1(
                filepath, si,
                include_per_frame=True,
                on_progress=on_progress,
            )
            result.layer1_ran = True

            if self._stopped:
                result.total_elapsed_sec = round(time.time() - t_total, 2)
                return result

            # Quick classify after Layer 1
            result.classification = classify(si, result.bitstream)

            # Decide if Layer 2 is needed
            needs_layer2 = self._auto_layer2 and self._needs_pixel_analysis(
                si, result.bitstream
            )
        else:
            needs_layer2 = False
            result.classification = classify(si, None)

        # ── Layer 2: Pixel Analysis ────────────────────────────────────
        if needs_layer2 and not self._stopped:
            on_progress("Layer 2: Pixel analysis (Naranjo + autocorrelation)...")
            result.pixel = run_layer2(
                filepath, si,
                include_per_frame=True,
                on_progress=on_progress,
                check_cancelled=self._is_cancelled,
            )
            result.layer2_ran = True

            if self._stopped or result.pixel.error:
                result.classification = classify(
                    si, result.bitstream, result.pixel
                )
                result.total_elapsed_sec = round(time.time() - t_total, 2)
                return result

            # Reclassify with Layer 2 data
            result.classification = classify(
                si, result.bitstream, result.pixel
            )

            # Decide if Layer 3 is needed
            needs_layer3 = self._auto_layer3 and self._needs_field_swap(
                result.pixel
            )
        else:
            needs_layer3 = False

        # ── Layer 3: Field-Swap Validation ─────────────────────────────
        if needs_layer3 and result.pixel and not self._stopped:
            on_progress("Layer 3: Field-swap validation...")
            result.field_swap = run_layer3(
                filepath, si, result.pixel,
                on_progress=on_progress,
                check_cancelled=self._is_cancelled,
            )
            result.layer3_ran = True

            # Final classification with all layers
            result.classification = classify(
                si, result.bitstream, result.pixel, result.field_swap
            )

        result.total_elapsed_sec = round(time.time() - t_total, 2)
        on_progress(
            f"Complete: {result.classification.classification} "
            f"({result.classification.confidence}) in {result.total_elapsed_sec:.1f}s"
        )

        return result

    def _needs_pixel_analysis(self, si: StreamInfo, bs) -> bool:
        """Determine if Layer 2 pixel analysis is needed."""
        if bs is None or bs.error:
            return False
        # Layer 2 needed when bitstream alone can't classify confidently:
        # Low Film% + high interlaced → could be hard telecine or native interlaced
        if bs.interlaced_pct > 80 and bs.film_pct < FILM_PCT_LOW:
            return True
        return False

    def _needs_field_swap(self, px: PixelResult) -> bool:
        """Determine if Layer 3 field-swap validation is needed."""
        if px is None or px.error:
            return False
        # Gray zone: duplicate field % between thresholds
        from .models import DUP_FIELD_TELECINE_PCT, DUP_FIELD_INTERLACED_PCT
        dup_pct = px.dup_field_pct
        if DUP_FIELD_INTERLACED_PCT < dup_pct < DUP_FIELD_TELECINE_PCT:
            return True
        # Also run if high dup but no period-5 (shifted cadence)
        if dup_pct >= DUP_FIELD_TELECINE_PCT and not px.dup_field_has_period5:
            return True
        return False
