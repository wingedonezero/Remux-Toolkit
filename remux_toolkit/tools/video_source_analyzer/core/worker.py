"""Worker thread for async video source analysis."""

from __future__ import annotations

import os
import time
from collections import Counter
from typing import Optional

from PyQt6 import QtCore

from .models import (
    AnalysisResult, StreamInfo, Segment, BitstreamResult, PixelResult,
    FILM_PCT_HIGH, FILM_PCT_LOW, SEGMENT_WINDOW,
    TELECINE_COMBED_MIN, TELECINE_CADENCE5_MIN,
    INTERLACED_COMBED_MIN, PROGRESSIVE_COMBED_MAX,
)
from .stream_info import get_stream_info
from .bitstream import run_layer1
from .pixel_analysis import run_layer2
from .field_swap import run_layer3
from .classifier import classify


def _classify_window(
    combed_pct: float,
    cadence5_pct: float,
    cycling_pct: float,
    file_type: str,
) -> str:
    """
    Classify a single window using cadence-5 as the primary signal.

    Cadence-5 is the structural fingerprint of NTSC 3:2 pulldown — if
    a window has period-5 combed pairs, it IS telecine, regardless of
    combing %. This same logic works at file and window level.

    Quiet/static windows have very few combed frames so cadence-5 isn't
    meaningful — those default to the file type.
    """
    if cycling_pct > 20:
        return "FILM"

    # ── Strong telecine cadence → TELECINE (truth signal) ────────────
    # Cadence is structural; if it's there, it's telecine
    if cadence5_pct >= 25 and combed_pct >= 8:
        return "TELECINE"

    # ── High combing without cadence → INTERLACED ────────────────────
    # True interlaced video has combing on most motion frames
    if combed_pct >= 30 and cadence5_pct < 10:
        return "INTERLACED"

    # ── Very low combing → PROGRESSIVE or static section ─────────────
    if combed_pct < 5 and cadence5_pct < 8:
        # In a telecine/interlaced file, this could just be a static scene
        # — fall back to file type rather than calling it progressive
        if file_type in ("hard_telecine", "interlaced"):
            return "TELECINE" if file_type == "hard_telecine" else "INTERLACED"
        return "PROGRESSIVE"

    # ── Moderate signals — defer to file type ────────────────────────
    # Window data is ambiguous, trust the file-level classification
    if file_type == "hard_telecine":
        return "TELECINE"
    if file_type == "interlaced":
        return "INTERLACED"
    if file_type == "progressive":
        return "PROGRESSIVE"

    # ── Standalone fallback for soft_telecine_mixed / unknown files ──
    if combed_pct >= 20 and cadence5_pct >= 15:
        return "TELECINE"
    if combed_pct >= 30:
        return "INTERLACED"
    if combed_pct < 10:
        return "PROGRESSIVE"
    return "MIXED"


def _window_cadence5(combed_indices: list[int]) -> float:
    """
    Compute telecine cadence percentage for a list of combed frame indices.

    Counts only the true 3:2 pulldown signature: consecutive gap pairs
    that are exactly (1,4) or (4,1). The (2,3) pattern that occurs in
    interlaced content with motion is correctly excluded.
    """
    if len(combed_indices) < 3:
        return 0.0
    gaps = [combed_indices[j+1] - combed_indices[j] for j in range(len(combed_indices) - 1)]
    telecine_pairs = sum(
        1 for j in range(len(gaps) - 1)
        if (gaps[j] == 1 and gaps[j+1] == 4) or (gaps[j] == 4 and gaps[j+1] == 1)
    )
    total_pairs = len(gaps) - 1
    return (telecine_pairs / total_pairs * 100) if total_pairs > 0 else 0.0


def _refine_segments_with_layer2(
    bitstream: BitstreamResult,
    pixel: PixelResult,
    fps: float,
    file_type: str = "",
) -> list[Segment]:
    """
    Rebuild segment map using Layer 2 per-frame combing data.

    Uses the file's overall classification as dominant context. Combing %
    varies hugely with motion, so a quiet window in interlaced content
    will look different from a busy one — but it's still interlaced.
    """
    per_frame = pixel.per_frame
    if not per_frame:
        return bitstream.segments

    total = len(per_frame)
    if total == 0:
        return bitstream.segments
    if fps <= 0:
        fps = 29.97

    l1_per_frame = bitstream.per_frame if bitstream else []
    window = SEGMENT_WINDOW

    # Pass 1: classify each window AND mark low-signal windows
    # Low-signal windows have too few combed frames for cadence analysis
    # to be meaningful — they need to inherit from neighbors.
    LOW_SIGNAL_COMBED_PCT = 5.0  # below this, we don't trust the cadence

    window_data: list[dict] = []
    for win_start in range(0, total, window):
        win_end = min(win_start + window, total)
        chunk = per_frame[win_start:win_end]
        chunk_size = len(chunk)

        combed_count = sum(1 for f in chunk if f.get("combed", False))
        combed_pct = combed_count / chunk_size * 100 if chunk_size else 0

        local_combed = [f["idx"] for f in chunk if f.get("combed", False)]
        cadence5 = _window_cadence5(local_combed)

        cycling_pct = 0.0
        if l1_per_frame and win_end <= len(l1_per_frame):
            l1_chunk = l1_per_frame[win_start:win_end]
            cycling_count = sum(1 for f in l1_chunk if f.get("cycling") is True)
            cycling_pct = cycling_count / chunk_size * 100

        win_type = _classify_window(
            combed_pct, cadence5, cycling_pct, file_type,
        )

        # Low-signal: not enough combed frames to trust the verdict
        low_signal = combed_pct < LOW_SIGNAL_COMBED_PCT and cycling_pct < 20

        window_data.append({
            "start": win_start, "end": win_end,
            "type": win_type, "low_signal": low_signal,
            "combed_pct": combed_pct, "cadence5": cadence5,
        })

    # Pass 2a: carry-forward through low-signal stretches
    # If a low-signal window is between two high-confidence windows of the
    # same type, inherit that type. Walk forwards filling gaps between
    # confident windows of the same type.
    n = len(window_data)
    if n >= 3:
        # Find runs of low-signal windows and check their neighbors
        i = 0
        while i < n:
            if window_data[i]["low_signal"]:
                # Find the end of this low-signal run
                j = i
                while j < n and window_data[j]["low_signal"]:
                    j += 1
                # i..j-1 is the low-signal run
                # Look at the high-signal neighbors before and after
                prev_type = None
                next_type = None
                if i > 0 and not window_data[i - 1]["low_signal"]:
                    prev_type = window_data[i - 1]["type"]
                if j < n and not window_data[j]["low_signal"]:
                    next_type = window_data[j]["type"]

                # If both neighbors are same type → inherit
                # If only one neighbor exists → inherit
                inherited = None
                if prev_type and next_type:
                    if prev_type == next_type:
                        inherited = prev_type
                elif prev_type:
                    inherited = prev_type
                elif next_type:
                    inherited = next_type

                if inherited:
                    for k in range(i, j):
                        window_data[k]["type"] = inherited
                i = j
            else:
                i += 1

    # Pass 2b: absorb single isolated different windows
    if n >= 3:
        for i in range(1, n - 1):
            prev_type = window_data[i - 1]["type"]
            curr_type = window_data[i]["type"]
            next_type = window_data[i + 1]["type"]
            if prev_type == next_type and curr_type != prev_type:
                window_data[i]["type"] = prev_type

    # Pass 2c: absorb 2-window anomalies surrounded by same type
    # e.g. TELECINE TELECINE INTERLACED INTERLACED TELECINE TELECINE
    if n >= 5:
        for i in range(1, n - 3):
            if (window_data[i - 1]["type"] == window_data[i + 3]["type"]
                    and window_data[i]["type"] == window_data[i + 1]["type"]
                    and window_data[i]["type"] != window_data[i - 1]["type"]):
                # Only absorb if the pair is short relative to surrounding context
                # (don't absorb genuine 2-window OPs/EDs which are usually 3+ windows)
                surround_type = window_data[i - 1]["type"]
                window_data[i]["type"] = surround_type
                window_data[i + 1]["type"] = surround_type

    # Convert window_data back to (start, end, type) tuples for Pass 3
    window_types = [(w["start"], w["end"], w["type"]) for w in window_data]

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
            # Non-MPEG-2: always run Layer 2 if enabled
            needs_layer2 = self._auto_layer2
            result.classification = classify(si, None)

        # ── Layer 2: Pixel Analysis ────────────────────────────────────
        if needs_layer2 and not self._stopped:
            on_progress("Layer 2: Laplacian combing detection...")
            try:
                result.pixel = run_layer2(
                    filepath, si,
                    include_per_frame=True,
                    on_progress=on_progress,
                    check_cancelled=self._is_cancelled,
                )
                result.layer2_ran = True
            except ImportError as e:
                on_progress(f"Layer 2 skipped: {e} (vapoursynth required)")
                result.pixel = PixelResult(error=f"import error: {e}")
                result.layer2_ran = False
            except Exception as e:
                on_progress(f"Layer 2 error: {e}")
                result.pixel = PixelResult(error=str(e))
                result.layer2_ran = True

            if result.pixel and (self._stopped or result.pixel.error):
                result.classification = classify(
                    si, result.bitstream, result.pixel
                )
                result.total_elapsed_sec = round(time.time() - t_total, 2)
                return result

            # Classify first so we know the file type for context-aware refinement
            result.classification = classify(
                si, result.bitstream, result.pixel
            )

            # Refine segment map with Layer 2 per-frame data
            if result.bitstream and result.pixel:
                on_progress("Refining segment map with combing data...")
                fps = si.fps if si.fps > 0 else 29.97
                result.bitstream.segments = _refine_segments_with_layer2(
                    result.bitstream, result.pixel, fps,
                    file_type=result.classification.classification,
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
        # Run Layer 3 in gray zones where combing/cadence is ambiguous
        combed = px.combed_pct
        cadence5 = px.cadence5_pct
        # Moderate combing but weak cadence — could be telecine with broken cadence
        if PROGRESSIVE_COMBED_MAX <= combed < INTERLACED_COMBED_MIN and cadence5 < TELECINE_CADENCE5_MIN:
            return True
        # High combing with some cadence but not conclusive
        if combed >= TELECINE_COMBED_MIN and 5 < cadence5 < TELECINE_CADENCE5_MIN:
            return True
        return False
