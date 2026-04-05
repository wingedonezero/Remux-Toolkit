"""Worker thread for async video source analysis."""

from __future__ import annotations

import os
import time
from typing import Optional

from PyQt6 import QtCore

from .models import (
    AnalysisResult, StreamInfo, Segment, BitstreamResult, PixelResult,
    FILM_PCT_HIGH, FILM_PCT_LOW, SEGMENT_WINDOW,
    DUP_FIELD_SAD_THRESHOLD, COMBING_RATIO_THRESHOLD,
    DUP_FIELD_TELECINE_PCT, DUP_FIELD_INTERLACED_PCT,
)
from .stream_info import get_stream_info
from .bitstream import run_layer1
from .pixel_analysis import run_layer2
from .field_swap import run_layer3
from .classifier import classify


def _compute_window_metrics(
    per_frame: list[dict],
    l1_per_frame: list[dict],
    win_start: int,
    win_end: int,
) -> tuple[float, float, float, float]:
    """Compute dup_pct, combed_pct, cycling_pct, intl_pct for a window."""
    chunk = per_frame[win_start:win_end]
    chunk_size = len(chunk)

    combed_count = sum(1 for f in chunk if f.get("combed", False))
    combed_pct = combed_count / chunk_size * 100 if chunk_size else 0

    dup_count = 0
    dup_total = 0
    for f in chunk:
        top = f.get("top_fsad")
        bot = f.get("bot_fsad")
        if top is not None and top >= 0:
            dup_total += 1
            if top < DUP_FIELD_SAD_THRESHOLD or (bot is not None and bot < DUP_FIELD_SAD_THRESHOLD):
                dup_count += 1
    dup_pct = (dup_count / dup_total * 100) if dup_total > 0 else 0

    cycling_pct = 0.0
    intl_pct = 100.0
    if l1_per_frame and win_end <= len(l1_per_frame):
        l1_chunk = l1_per_frame[win_start:win_end]
        cycling_count = sum(1 for f in l1_chunk if f.get("cycling") is True)
        intl_count = sum(1 for f in l1_chunk if f.get("interlaced_frame") == 1)
        cycling_pct = cycling_count / chunk_size * 100
        intl_pct = intl_count / chunk_size * 100

    return dup_pct, combed_pct, cycling_pct, intl_pct


def _classify_window(
    dup_pct: float, combed_pct: float, cycling_pct: float,
    global_dup_pct: float,
) -> str:
    """Classify a single window, using global context to reduce noise."""
    if cycling_pct > 20:
        return "FILM"

    # Use a relaxed threshold for windows when the global file is clearly
    # telecine. Scene changes and static sections temporarily drop dup%
    # below the full-file threshold, but they're still telecine content.
    if global_dup_pct >= DUP_FIELD_TELECINE_PCT:
        # Global file is telecine — use a lower per-window threshold
        # Only classify as non-telecine if dup% drops very low
        if dup_pct >= 5.0:
            return "TELECINE"
        elif combed_pct < 2:
            return "PROGRESSIVE"
        else:
            return "INTERLACED"

    # Global file is NOT clearly telecine — use standard thresholds
    if dup_pct >= DUP_FIELD_TELECINE_PCT:
        return "TELECINE"
    elif dup_pct <= DUP_FIELD_INTERLACED_PCT and combed_pct < 5:
        return "PROGRESSIVE"
    elif dup_pct <= DUP_FIELD_INTERLACED_PCT:
        return "INTERLACED"
    else:
        return "MIXED"


def _refine_segments_with_layer2(
    bitstream: BitstreamResult,
    pixel: PixelResult,
    fps: float,
) -> list[Segment]:
    """
    Rebuild segment map using Layer 2 per-frame data.

    Layer 1 segments are based purely on trf cycling. For hard telecine
    (no RFF flags), Layer 1 sees everything as VIDEO even though the content
    is telecine. This function re-segments using per-frame duplicate field
    and combing data from Layer 2.

    Uses the global dup field % to set context-aware thresholds: when the
    whole file is clearly telecine, individual windows need much lower dup%
    to be classified as non-telecine (avoids false MIXED on scene changes).
    """
    per_frame = pixel.per_frame
    if not per_frame:
        return bitstream.segments

    total = len(per_frame)
    if total == 0:
        return bitstream.segments
    if fps <= 0:
        fps = 29.97

    # Global dup field % from full-file analysis (for context)
    global_dup_pct = pixel.dup_field_pct

    l1_per_frame = bitstream.per_frame if bitstream else []
    window = SEGMENT_WINDOW

    # Pass 1: classify each window
    window_types: list[tuple[int, int, str]] = []  # (start, end, type)
    for win_start in range(0, total, window):
        win_end = min(win_start + window, total)
        dup_pct, combed_pct, cycling_pct, _ = _compute_window_metrics(
            per_frame, l1_per_frame, win_start, win_end,
        )
        win_type = _classify_window(dup_pct, combed_pct, cycling_pct, global_dup_pct)
        window_types.append((win_start, win_end, win_type))

    # Pass 2: absorb tiny isolated segments (1 window surrounded by same type)
    if len(window_types) >= 3:
        for i in range(1, len(window_types) - 1):
            prev_type = window_types[i - 1][2]
            curr_type = window_types[i][2]
            next_type = window_types[i + 1][2]
            if prev_type == next_type and curr_type != prev_type:
                # Isolated different window — absorb into neighbors
                window_types[i] = (window_types[i][0], window_types[i][1], prev_type)

    # Pass 3: merge adjacent same-type windows into segments
    segments: list[Segment] = []
    current_type: str | None = None
    seg_start = 0

    for win_start, win_end, win_type in window_types:
        if win_type != current_type:
            if current_type is not None:
                seg = _make_refined_segment(
                    seg_start, win_start, current_type,
                    per_frame, bitstream, fps,
                )
                segments.append(seg)
            seg_start = win_start
            current_type = win_type

    # Close final segment
    if current_type is not None:
        seg = _make_refined_segment(
            seg_start, total, current_type,
            per_frame, bitstream, fps,
        )
        segments.append(seg)

    return segments


def _make_refined_segment(
    start: int, end: int, content_type: str,
    per_frame: list[dict], bitstream: BitstreamResult, fps: float,
) -> Segment:
    """Create a Segment with both Layer 1 and Layer 2 metrics."""
    chunk = per_frame[start:end]
    chunk_size = len(chunk)

    combed_count = sum(1 for f in chunk if f.get("combed", False))
    combed_pct = combed_count / chunk_size * 100 if chunk_size else 0

    dup_count = 0
    dup_total = 0
    for f in chunk:
        top = f.get("top_fsad")
        bot = f.get("bot_fsad")
        if top is not None and top >= 0:
            dup_total += 1
            if top < DUP_FIELD_SAD_THRESHOLD or (bot is not None and bot < DUP_FIELD_SAD_THRESHOLD):
                dup_count += 1
    dup_pct = (dup_count / dup_total * 100) if dup_total > 0 else 0

    # Layer 1 metrics for this range
    cycling_pct = 0.0
    intl_pct = 100.0
    l1_per_frame = bitstream.per_frame if bitstream else []
    if l1_per_frame and end <= len(l1_per_frame):
        l1_chunk = l1_per_frame[start:end]
        cycling_count = sum(1 for f in l1_chunk if f.get("cycling") is True)
        intl_count = sum(1 for f in l1_chunk if f.get("interlaced_frame") == 1)
        cycling_pct = round(cycling_count / chunk_size * 100, 1)
        intl_pct = round(intl_count / chunk_size * 100, 1)

    # Map content_type back to seg_type for Layer 1 compatibility
    seg_type_map = {
        "FILM": "FILM",
        "TELECINE": "FILM",
        "INTERLACED": "VIDEO",
        "PROGRESSIVE": "VIDEO",
        "MIXED": "MIXED",
    }

    return Segment(
        start_frame=start,
        end_frame=end,
        seg_type=seg_type_map.get(content_type, "VIDEO"),
        cycling_pct=cycling_pct,
        interlaced_pct=intl_pct,
        duration_sec=round(chunk_size / fps, 1),
        content_type=content_type,
        dup_field_pct=round(dup_pct, 1),
        combed_pct=round(combed_pct, 1),
    )


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

    @QtCore.pyqtSlot()
    def _run_pending(self):
        """Called by thread.started — runs on the worker thread."""
        files = getattr(self, "_pending_files", [])
        l2 = getattr(self, "_pending_l2", True)
        l3 = getattr(self, "_pending_l3", True)
        self.analyze_files(files, l2, l3)

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
            if needs_layer2:
                on_progress(
                    f"Layer 1 result: {result.classification.classification} "
                    f"({result.classification.confidence}) — running Layer 2..."
                )
            else:
                on_progress(
                    f"Layer 1 classified: {result.classification.classification} "
                    f"({result.classification.confidence}) — Layer 2 not needed"
                )
        else:
            needs_layer2 = False
            result.classification = classify(si, None)

        # ── Layer 2: Pixel Analysis ────────────────────────────────────
        if needs_layer2 and not self._stopped:
            on_progress("Layer 2: Pixel analysis (Naranjo + autocorrelation)...")
            try:
                result.pixel = run_layer2(
                    filepath, si,
                    include_per_frame=True,
                    on_progress=on_progress,
                    check_cancelled=self._is_cancelled,
                )
                result.layer2_ran = True
            except ImportError as e:
                on_progress(f"Layer 2 skipped: {e} (vapoursynth/scipy required)")
                from .models import PixelResult
                result.pixel = PixelResult(error=f"import error: {e}")
                result.layer2_ran = False
            except Exception as e:
                on_progress(f"Layer 2 error: {e}")
                from .models import PixelResult
                result.pixel = PixelResult(error=str(e))
                result.layer2_ran = True

            if result.pixel and (self._stopped or result.pixel.error):
                result.classification = classify(
                    si, result.bitstream, result.pixel
                )
                result.total_elapsed_sec = round(time.time() - t_total, 2)
                return result

            # Refine segment map with Layer 2 per-frame data
            if result.bitstream and result.pixel:
                on_progress("Refining segment map with pixel analysis data...")
                fps = si.fps if si.fps > 0 else 29.97
                result.bitstream.segments = _refine_segments_with_layer2(
                    result.bitstream, result.pixel, fps
                )

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
            try:
                result.field_swap = run_layer3(
                    filepath, si, result.pixel,
                    on_progress=on_progress,
                    check_cancelled=self._is_cancelled,
                )
                result.layer3_ran = True
            except ImportError as e:
                on_progress(f"Layer 3 skipped: {e}")
                from .models import FieldSwapResult
                result.field_swap = FieldSwapResult(error=f"import error: {e}")
            except Exception as e:
                on_progress(f"Layer 3 error: {e}")
                from .models import FieldSwapResult
                result.field_swap = FieldSwapResult(error=str(e))
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
