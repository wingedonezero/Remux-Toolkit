"""
Native DVD title probe using libdvdread directly (via the ctypes binding
under ../bindings/libdvdread.py) plus the Phase 2 analyzer brain.

Drop-in for the existing `DVDProbeWorker` from info_probe.py — same class
name, same Qt signal signature, same .probe(row, job) entry point — but
faster, more deterministic, and decoupled from any FFmpeg dvdvideo bugs.

The pure-Python probe function `probe_disc()` is split out so it can be
unit-tested without a Qt event loop.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ProbeResult:
    label: Optional[str]
    titles_total: Optional[int]
    titles_info: Optional[dict]
    disc_info: dict
    err: str


def probe_disc(source_path: str | Path,
               default_label: Optional[str] = None) -> ProbeResult:
    """Run the inspector + analyzer on a disc path and produce a result
    that's directly usable by the existing GUI (titles_info dict matches
    the shape `_on_probed` expects)."""
    from .analysis.analyzer import analyze
    from .analysis.inspector import _resolve_disc_path, inspect_disc
    from ..bindings import libdvdread as dr

    try:
        src = _resolve_disc_path(Path(source_path))
        if src is None:
            return ProbeResult(None, None, None, {},
                               "No VIDEO_TS or ISO found at source path")
        if isinstance(src, list):
            return ProbeResult(None, None, None, {},
                               f"Multiple discs found under source; pick one: "
                               f"{[str(p) for p in src]}")

        report = inspect_disc(src, include_cells=False)
        analyzed = analyze(report)

        return ProbeResult(
            label=(analyzed.get("volume_id") or default_label or ""),
            titles_total=analyzed["summary"]["total_titles"],
            titles_info=analyzed["titles"],
            disc_info={
                "type": "DVD",
                "titles_found": analyzed["summary"]["total_titles"],
                "volume_id":   analyzed.get("volume_id", ""),
                "disc_id_md5": analyzed.get("disc_id_md5", ""),
                "analyzer_summary": analyzed["summary"],
                "probe_backend": "libdvdread-native",
            },
            err="",
        )
    except dr.DvdReadError as e:
        return ProbeResult(None, None, None, {}, f"libdvdread error: {e}")
    except Exception as e:
        return ProbeResult(None, None, None, {}, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Qt wrapper for use by the GUI (kept thin; all real work lives in probe_disc)
# ---------------------------------------------------------------------------
# Import PyQt6 lazily so non-GUI consumers (CLI, tests) can import this module
# without Qt installed.

try:
    from PyQt6.QtCore import QObject, pyqtSignal  # type: ignore

    class DVDProbeWorker(QObject):
        """Emits `probed(row, label, titles_total, titles_info, disc_info, err)`.

        `titles_info` is a {title_num: dict} compatible with the existing GUI;
        the dict contains all keys the GUI's `_on_probed` reads, plus analyzer
        extensions (`hidden_by_default`, `duplicate_of`, `classification`, etc.)
        that future GUI work can use without breaking the current code.
        """

        probed = pyqtSignal(int, object, object, object, object, str)

        def __init__(self, settings: dict):
            super().__init__()
            self.settings = settings

        def probe(self, row: int, job) -> None:
            r = probe_disc(job.source_path, default_label=getattr(job, "child_name", None))
            self.probed.emit(row, r.label, r.titles_total, r.titles_info,
                             r.disc_info, r.err)

except ImportError:
    pass  # GUI not available in this environment (e.g., test venv without PyQt)
