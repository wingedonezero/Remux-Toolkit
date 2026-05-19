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
    3002: MsgSpec(3002, "ifo-bup-offset-mismatch",
                  "Calculated %s offset for VTS #%d does not match one in IFO header.",
                  severity="warn"),
    3003: MsgSpec(3003, "ifo-bup-fallback",
                  "Using BUP for VTS #%d (main IFO corrupt or missing).",
                  severity="warn"),
    3015: MsgSpec(3015, "title-skip-nav-error",
                  "Title %d (%s) was skipped due to a navigation error.",
                  severity="warn"),
    3016: MsgSpec(3016, "title-skip",
                  "Title %d was skipped.",
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
    3037: MsgSpec(3037, "title-trim-start",
                  "Cells 1-%d were removed from title start",
                  severity="info"),
    3038: MsgSpec(3038, "title-trim-end",
                  "Cells %d-%d were removed from title end",
                  severity="info"),
    3039: MsgSpec(3039, "fake-cell-percentage",
                  "Fake cells are %d%% of title - assuming fake title",
                  severity="warn"),
    3041: MsgSpec(3041, "angle-added",
                  "Added angle %d to title %d",
                  severity="info"),
    3042: MsgSpec(3042, "ifo-bup-repair",
                  "IFO/BUP repair: %s",
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
