"""
High-level native-rip API.

Wraps the native pipeline (``open_dvd_title`` + ``MkvWriter`` + closed-
captions hookup) so callers (CLI, GUI worker, test fixtures) drive it
with one function call instead of stitching the pieces themselves.

This is the bridge between the low-level mux primitives and the GUI's
job loop. Mirrors ``orchestrator.rip_title`` for the ffmpeg path so the
worker can swap backends with a flag.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from ..bindings import libdvdread as dr
from .analysis.cell_trim import (
    TrimDecision, cell_metadata_from_pgc, count_angles, decide_trim,
)
from .analysis.closed_captions import (
    add_closed_captions_to_rip, have_ccextractor, have_mkvmerge,
)
from .demux.cell_reader import _resolve_title_to_pgc
from .mux import MkvFormatInfo, MkvWriter
from .mux.dvd_frame_source import open_dvd_title


_logger = logging.getLogger(__name__)


@dataclass
class NativeRipOptions:
    """Configuration for one native-rip invocation.

    Mirrors fields from ``orchestrator.RipOptions`` where they apply to
    the native path. The native path doesn't need ``use_dvdvideo_for_subs``
    (it does subs inline via SP_DCSQ) or ``write_color_metadata`` (always
    written when present on the plan).
    """
    include_subpictures: bool = True
    include_closed_captions: bool = True
    write_chapters: bool = True
    angle: int = 1                     # 1-indexed; for multi-angle titles
    apply_trim: bool = False           # opt-in: run decide_trim and skip cells
    cc_language: str = "eng"
    log_callback: Optional[Callable[[str, str], None]] = None
    progress_callback: Optional[Callable[[int, int], None]] = None  # (cur, total)
    cancel_check: Optional[Callable[[], bool]] = None


@dataclass
class NativeRipResult:
    output_path: Path
    success: bool
    elapsed_s: float = 0.0
    track_count: int = 0
    duration_ns: int = 0
    cluster_count: int = 0
    bytes_written: int = 0
    frames_written: int = 0
    angle_count: int = 1
    angle_used: int = 1
    trim_decision: Optional[TrimDecision] = None
    cc_declared: bool = False
    cc_merged: bool = False
    error_message: Optional[str] = None
    warnings: List[str] = field(default_factory=list)


def rip_title_native(disc_path: str, title_num: int, output_path: Path,
                     *, options: Optional[NativeRipOptions] = None,
                     title_name: Optional[str] = None,
                     ) -> NativeRipResult:
    """Rip one DVD title to MKV via the native pipeline.

    ``disc_path`` is the path passed to ``dr.open_disc`` (DVD folder /
    ISO / device).

    Workflow:
      1. Open the disc; resolve title → (vts_no, pgc_no).
      2. Optionally compute trim decision; pre-compute angle count.
      3. Drive ``open_dvd_title`` → ``MkvWriter.write_track``.
      4. Post-rip: if CCs declared + ccextractor available, extract +
         merge via mkvmerge.
    """
    opts = options or NativeRipOptions()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    result = NativeRipResult(output_path=output_path, success=False,
                             angle_used=opts.angle)
    t0 = time.time()

    def _log(severity: str, msg: str) -> None:
        if opts.log_callback:
            opts.log_callback(severity, msg)
        else:
            getattr(_logger, severity, _logger.info)(msg)

    try:
        with dr.open_disc(disc_path) as disc:
            vts_no, pgc_no = _resolve_title_to_pgc(disc, title_num)

            # Inspect for angle + trim BEFORE opening producer.
            with dr.open_ifo(disc, vts_no) as vts:
                pgc = vts.contents.vts_pgcit.contents.pgci_srp[pgc_no - 1].pgc.contents
                cells_meta = cell_metadata_from_pgc(pgc)
            result.angle_count = count_angles(cells_meta)
            if opts.apply_trim:
                result.trim_decision = decide_trim(cells_meta, pgc)

            if opts.cancel_check and opts.cancel_check():
                result.error_message = "cancelled before rip"
                return result

            _log("info", f"native rip T{title_num} angle={opts.angle}/{result.angle_count}, "
                          f"trim={result.trim_decision.any_trim if result.trim_decision else False}")

            track, title_info = open_dvd_title(
                disc, title_num,
                title_name=title_name or f"Title {title_num}",
                include_subpictures=opts.include_subpictures,
                angle=opts.angle,
                trim=result.trim_decision,
            )

            writer = MkvWriter(output_path, format_info=MkvFormatInfo())
            rip_result = writer.write_track(track, title_info)

            # Wait for the producer to drain before closing the disc.
            track.producer_thread().join(timeout=600)

        # Populate result from rip_result
        result.success = rip_result.success
        result.elapsed_s = rip_result.elapsed_s
        result.track_count = rip_result.track_count
        result.duration_ns = rip_result.duration_ns
        result.cluster_count = rip_result.cluster_count
        result.bytes_written = sum(s.bytes_written for s in rip_result.per_stream_stats)
        result.frames_written = sum(s.frames_written for s in rip_result.per_stream_stats)
        result.error_message = rip_result.error_message

        # Closed captions post-pass (uses ccextractor + mkvmerge on the
        # finished MKV; skips silently if either tool is missing).
        if (opts.include_closed_captions and result.success and
                have_ccextractor() and have_mkvmerge()):
            # Re-open disc just for the CC declaration check (cheap).
            with dr.open_disc(disc_path) as disc:
                cc_outcome = add_closed_captions_to_rip(
                    output_path, disc, vts_no, lang=opts.cc_language,
                )
            result.cc_declared = cc_outcome.cc_declared
            result.cc_merged = cc_outcome.merged
            if cc_outcome.cc_declared and not cc_outcome.merged:
                result.warnings.append(
                    f"CC pipeline: {cc_outcome.error or 'no CCs found'}"
                )
            elif cc_outcome.merged:
                _log("info", f"closed captions merged ({cc_outcome.srt_bytes} bytes SRT)")

    except Exception as e:
        result.success = False
        result.error_message = f"{type(e).__name__}: {e}"
        _logger.exception("native rip crashed")

    result.elapsed_s = time.time() - t0
    return result


__all__ = ["NativeRipOptions", "NativeRipResult", "rip_title_native"]
