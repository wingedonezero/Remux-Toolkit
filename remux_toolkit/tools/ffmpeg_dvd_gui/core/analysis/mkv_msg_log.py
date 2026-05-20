"""
MakeMKV-equivalent MSG-code logging.

MakeMKV emits structured ``MSG:<code>,<flags>,<argc>,<text>,<fmt>,<args...>``
lines to its ``--messages`` file. Each MSG code documents a specific
decision the engine made (title skipped, title added, cells trimmed,
stream deduped, IFO/BUP repair, etc). Cross-validation against
MakeMKV's output relies on us emitting equivalent log entries from
*our* decision points so deltas show up as code-level deltas, not
just behavioural surprises.

This module gives our analyzer / trim / dedup / IFO-validate paths
a single entry-point to log a decision in MakeMKV-compatible form:

    from . import mkv_msg_log
    mkv_msg_log.emit("3037", "Cells 1-%d were removed from title start",
                     count, title=title_id)

Each call:
    * writes to a Python ``logging.Logger`` (so existing log handlers
      pick it up)
    * appends to an in-process collector (for cross-validation tooling
      to read back without parsing stderr)
    * records the (code, args) for later count-comparison

The catalog includes only the codes our code actually emits; new codes
get added as we port more decision paths.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional


_logger = logging.getLogger("remux_toolkit.mkv_msg")
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Catalog — keep in sync with research/ghidra_output/msg_catalog.md.
# Only codes we actually emit are listed; commented blocks show codes we
# know about but haven't ported the corresponding decision for yet.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MsgSpec:
    code: int
    name: str
    fmt: str
    severity: str = "info"   # "info" | "warn" | "error"


CATALOG: Dict[int, MsgSpec] = {
    3000: MsgSpec(3000, "dvd-file-corrupted",
                  "DVD file is corrupted.",
                  severity="error"),
    3001: MsgSpec(3001, "dvd-file-inconsistent",
                  "DVD file is inconsistent: structure protection, "
                  "or some files are missing.",
                  severity="error"),
    3002: MsgSpec(3002, "ifo-bup-offset-mismatch",
                  "Calculated %s offset for VTS #%d does not match one in IFO header.",
                  severity="warn"),
    3003: MsgSpec(3003, "ifo-bup-fallback",
                  "Using BUP for VTS #%d (main IFO corrupt or missing).",
                  severity="warn"),
    3004: MsgSpec(3004, "dvd-titleset-count-invalid",
                  "Number of title sets is %d (maximum is 99).",
                  severity="warn"),
    3005: MsgSpec(3005, "titleset-id-invalid",
                  "TitleSet ID %d is invalid.",
                  severity="warn"),
    3006: MsgSpec(3006, "opening-files",
                  "Opening files at %s",
                  severity="info"),
    3007: MsgSpec(3007, "direct-disc-access",
                  "Using direct disc access mode.",
                  severity="info"),
    3008: MsgSpec(3008, "titleset-sector-mismatch",
                  "Titleset start sector mismatch for titleset %d : %d != %d",
                  severity="warn"),
    3009: MsgSpec(3009, "title-ttn-not-found",
                  "Title TTN %d not found in title set %d",
                  severity="warn"),
    3010: MsgSpec(3010, "cell-list-sanity",
                  "Cells not found for VTS %d TTN %d PGCN %d PGN %d",
                  severity="warn"),
    3011: MsgSpec(3011, "audio-stream-count-bad",
                  "Number of audio streams in VTS %d is %d",
                  severity="warn"),
    3012: MsgSpec(3012, "subp-stream-count-bad",
                  "Number of subtitle streams in VTS %d is %d",
                  severity="warn"),
    3013: MsgSpec(3013, "title-skip-special-attr",
                  "Skipped title %d with special attributes.",
                  severity="info"),
    3014: MsgSpec(3014, "title-skip-damaged-titleset",
                  "Skipped title #%d in damaged titleset.",
                  severity="warn"),
    3015: MsgSpec(3015, "title-skip-nav-error",
                  "Title %d (%s) was skipped due to a navigation error.",
                  severity="warn"),
    3016: MsgSpec(3016, "title-skip",
                  "Title %d was skipped.",
                  severity="info"),
    3017: MsgSpec(3017, "title-skip-alt",
                  "Skipped title %d (%s).",
                  severity="info"),
    3018: MsgSpec(3018, "title-multi-pgc",
                  "Title %d in VTS %d is a multi-PGC title.",
                  severity="info"),
    3019: MsgSpec(3019, "cell-skipped-cmd",
                  "Cells %d-%d were skipped due to cell command "
                  "(structure protection?).",
                  severity="warn"),
    3020: MsgSpec(3020, "cell-jumped-cmd",
                  "Jumped from cell %d to cell %d due to cell command "
                  "(structure protection?).",
                  severity="warn"),
    3021: MsgSpec(3021, "loop-detected",
                  "Loop detected. May be due to unknown structure "
                  "protection.",
                  severity="warn"),
    3022: MsgSpec(3022, "angle-block-damaged",
                  "Damaged angle block near cell %d",
                  severity="warn"),
    3023: MsgSpec(3023, "angle-count-invalid",
                  "Angle count invalid near cell %d: %d (title) != %d (PGC)",
                  severity="warn"),
    3024: MsgSpec(3024, "complex-multiplex",
                  "Complex multiplex detected. Need to inspect %d cells and "
                  "%d VOBUs. This takes time — please be patient.",
                  severity="info"),
    3025: MsgSpec(3025, "title-skip-too-short",
                  "Title #%d has length of %s seconds which is less than minimum "
                  "title length of %s seconds and was therefore skipped",
                  severity="info"),
    3026: MsgSpec(3026, "title-fake-declared-actual-mismatch",
                  "Title %d declared length is %s while its real length is %s - "
                  "assuming fake title",
                  severity="warn"),
    3027: MsgSpec(3027, "title-skip-duplicate",
                  "Title %d in VTS %d is equal to title %d and was skipped",
                  severity="info"),
    3028: MsgSpec(3028, "title-added",
                  "Title #%d was added (%d cell(s), %s)",
                  severity="info"),
    3029: MsgSpec(3029, "audio-stream-duplicate",
                  "Audio stream %d is equal to stream %d and was skipped",
                  severity="info"),
    3030: MsgSpec(3030, "subtitle-stream-duplicate",
                  "Subtitle stream %d is equal to stream %d and was skipped",
                  severity="info"),
    3031: MsgSpec(3031, "region-code-unlock-fail",
                  "Drive %s could not unlock region code. Change region, "
                  "or update firmware from http://tdb.rpc1.org. Errors may occur.",
                  severity="warn"),
    3032: MsgSpec(3032, "region-mismatch",
                  "Drive %s:%d region does not match disc. Trying workaround.",
                  severity="warn"),
    3033: MsgSpec(3033, "cell-discarded",
                  "Cell %d discarded (structure protection?).",
                  severity="warn"),
    3034: MsgSpec(3034, "audio-stream-empty",
                  "Audio stream #%d in title #%d looks empty and was skipped",
                  severity="info"),
    3035: MsgSpec(3035, "cellwalk-fallback-celltrim",
                  "CellWalk algorithm failed (strong structure protection?). "
                  "Trying CellTrim algorithm.",
                  severity="warn"),
    3036: MsgSpec(3036, "celltrim-too-few-chapters",
                  "CellTrim algorithm failed because title has only %d "
                  "chapters.",
                  severity="warn"),
    3037: MsgSpec(3037, "title-trim-start",
                  "Cells 1-%d were removed from title start",
                  severity="info"),
    3038: MsgSpec(3038, "title-trim-end",
                  "Cells %d-%d were removed from title end",
                  severity="info"),
    3039: MsgSpec(3039, "fake-cell-percentage",
                  "Fake cells are %d%% of title - assuming fake title",
                  severity="warn"),
    3040: MsgSpec(3040, "angle-added",
                  "Added angle %d to title %d",
                  severity="info"),
    3041: MsgSpec(3041, "angle-eval-failed",
                  "Adding angle %d to title %d failed",
                  severity="warn"),
    3042: MsgSpec(3042, "ifo-bup-repair",
                  "IFO/BUP repair: %s",
                  severity="warn"),
    3043: MsgSpec(3043, "cell-walk-fake",
                  "Cell %d flagged by cellwalk as suspicious.",
                  severity="warn"),
}


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class MsgEntry:
    code: int
    name: str
    severity: str
    text: str          # formatted human-readable text
    fmt: str           # raw format string (matches MakeMKV's %1/%2/... after our %d/%s substitutions)
    args: tuple        # positional args (the substitutions)
    context: dict      # caller-supplied kw (title=N, vts=M, etc.) for filtering


# Per-thread collector. Cross-validation harness reads this after a
# scan to compare with MakeMKV's messages.log.
_thread_local = threading.local()


def get_collector() -> List[MsgEntry]:
    """Return the current thread's MSG collector list. Empty unless
    ``collect()`` is currently active."""
    return getattr(_thread_local, "collector", [])


class collect:
    """Context manager: capture all MSG emits inside the block to a
    list returned by ``__enter__``. Use for cross-validation captures
    and tests."""

    def __enter__(self) -> List[MsgEntry]:
        # Push a new collector list.
        if not hasattr(_thread_local, "stack"):
            _thread_local.stack = []
        self._list: List[MsgEntry] = []
        _thread_local.stack.append(self._list)
        _thread_local.collector = self._list
        return self._list

    def __exit__(self, exc_type, exc, tb) -> None:
        _thread_local.stack.pop()
        _thread_local.collector = (
            _thread_local.stack[-1] if _thread_local.stack else []
        )


def emit(code: int, *args, **context) -> None:
    """Emit a MakeMKV-equivalent MSG. ``code`` is the MSG number; ``args``
    are formatted into the catalog's ``fmt`` string; ``context`` is
    structured metadata (title=N, vts=M, etc.) attached to the entry."""
    spec = CATALOG.get(code)
    if spec is None:
        _logger.warning("mkv_msg.emit: unknown MSG code %d", code)
        return
    try:
        text = spec.fmt % args
    except (TypeError, ValueError) as e:
        _logger.warning("mkv_msg.emit: format error for MSG:%d %r: %s",
                        code, args, e)
        text = f"MSG:{code} (args formatting error: {args!r})"

    entry = MsgEntry(
        code=code, name=spec.name, severity=spec.severity,
        text=text, fmt=spec.fmt, args=args, context=dict(context),
    )

    # Standard Python logger — visible through normal log config.
    level = {"info": logging.INFO, "warn": logging.WARNING,
             "error": logging.ERROR}.get(spec.severity, logging.INFO)
    _logger.log(level, "[MSG:%d] %s%s", code, text,
                f"  ({_format_context(context)})" if context else "")

    # In-process collector (used by cross-validation harness + tests).
    with _lock:
        coll = getattr(_thread_local, "collector", None)
        if coll is not None:
            coll.append(entry)


def _format_context(ctx: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in ctx.items())


def reset_collector() -> None:
    """Clear the current thread's collector (if active)."""
    coll = getattr(_thread_local, "collector", None)
    if coll is not None:
        coll.clear()


__all__ = [
    "CATALOG",
    "MsgEntry",
    "MsgSpec",
    "collect",
    "emit",
    "get_collector",
    "reset_collector",
]
