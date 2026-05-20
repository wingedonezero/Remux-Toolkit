"""
disc_open_enumerate — scaffold for MakeMKV's ``FUN_007d98d0`` (28,812 B).

The 28-KB top-level DVD-open orchestrator that runs UDF mount + VMG /
per-VTS IFO loading + post-walk dedup. Emits MSG:3004, 3005, 3006, 3008,
3027, 4041 from its inner loops.

Group F was originally specified to populate two state vectors at
``param_1[+0x130/+0x138]`` (disc-skip-list) and ``[+0x1f8/+0x200]``
(per-VTS title-claim list) that cellwalk_primary's IF/ELSE split and
title_init_validator's ``+0xd8`` binsearch consult.

**Group F finding (F.4 mid-session correction):** the two state vectors
are *never written* anywhere in disc_open_enumerate's depth-6 call tree
(234 functions audited, including dev-key-gated paths). Both callers of
disc_open_enumerate (FUN_007d9370 / FUN_007e34f0) ``malloc(0x218)`` and
explicitly zero-init the vectors; nothing else touches them. The 14
consumer reads in vts_title_scan + cellwalk_primary all evaluate
``(start == end) == True`` in the public binary.

Consequence: cellwalk_primary's ``disc_skip_list_nonempty`` and
``vts_state_skip_list_nonempty`` parameters are *provably* False in the
public binary. The hardcoded ``enter_if_branch = True`` is correct, not
an approximation. Reproduce the finding via
``research/validate_disc_state_writers.py``.

This module still exists because:

  * The F.1-F.3 scaffold (DiscState + UDF/region/CSS state + per-VTS IFO
    load metadata) is useful as a typed handle for analyzer wiring, and
    F.5+ will hang the cellwalk depth-4/5 helper port off the same
    surface.
  * The disc_state surface is forward-compatible with dev-build use of
    the binary (which might populate the skip-list).
  * Future MSG:3004/3005 emit work (F.8) needs a place to hang.

DiscState's ``disc_skip_list_nonempty`` and ``vts_claim_lists`` fields
stay as part of the public surface (they're observable through
``cellwalk_primary``); their default-empty state is the only state the
public binary ever produces.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Public state contract
# ---------------------------------------------------------------------------

@dataclass
class DiscState:
    """Disc-enumeration state, port of disc_open_enumerate's ``param_1`` struct.

    The fields mirror the offsets the decomp reads / writes from the
    object passed through ``FUN_007d98d0`` → ``FUN_007e8ad0`` (vts_title_scan)
    → ``FUN_007ec6f0`` (title_evaluator) → ``FUN_007f3eb0`` (cellwalk_primary).

    Initial-state semantics:

      * All booleans default to ``False`` (= "vector empty" or "flag not set").
      * Dicts default to empty (= no per-VTS data populated).
      * An empty ``DiscState`` is observationally indistinguishable from the
        pre-F.1 hardcoded defaults in cellwalk_primary / title_pre_filter.
    """

    # ``param_1[+0x130 / +0x138]`` — disc-level skip-list vector pair
    # (start_ptr / end_ptr). Equality ⇒ vector is empty.
    #
    # **Always False in the public binary** — Group F's
    # ``validate_disc_state_writers.py`` confirms zero writers exist
    # anywhere in disc_open_enumerate's depth-6 call tree (234
    # functions, including dev-key-gated paths). Kept on DiscState for
    # surface symmetry with the decomp + future dev-build compatibility.
    #
    # Read by vts_title_scan @ decomp line 4181 and cellwalk_primary @
    # decomp lines 5306, 5320, 5363, 6615. All 14 read sites evaluate
    # ``(start == end) == True``.
    disc_skip_list_nonempty: bool = False

    # ``param_1[+0x1f8 / +0x200]`` — per-VTS title-claim list vector
    # pair. Same Group F finding: no public-binary writers, stays empty
    # forever.
    #
    # Read by vts_title_scan @ decomp lines 4010-4011, 4149-4150 and
    # title_init_validator (FUN_007ed1f0 — port at
    # ``title_pre_filter.validate_title_init``). In the public binary
    # the title_init_validator binsearch loop (init_validator.md
    # lines 200-240) is gated on ``0 < (end - start)``; with the vector
    # empty, the loop is skipped and the binsearch always writes
    # ``title_state[+0xd8] = 0``. Our Python port skipping that write
    # entirely is equivalent.
    #
    # Kept on DiscState for surface symmetry. Element shape is opaque;
    # populating would only matter in a dev-build context.
    vts_claim_lists: Dict[int, List[Any]] = field(default_factory=dict)

    # ``title_state[+0xd8]`` written = 1 by disc_open_enumerate's
    # per-VTS open-failure branch (decomp lines 1469, 1481, 1513, 1551,
    # 1563). Maps 1-based VTS number → True when the VTS IFO failed to
    # load — the title-state placeholder marks "VTS unavailable" so the
    # downstream walks skip cleanly.
    #
    # NOTE: this is a per-VTS flag, not the same ``+0xd8`` that
    # title_init_validator's binsearch writes on the per-title state.
    # The per-title flag is computed by ``validate_title_init`` at gate
    # time using ``vts_claim_lists``; this dict tracks the disc-level
    # failure case from disc_open_enumerate's open loop.
    vts_failed: Dict[int, bool] = field(default_factory=dict)

    # ---- F.2: UDF mount + region + CSS init state --------------------------

    # ``True`` iff a libdvdread handle was successfully obtained for the
    # disc (either passed in via ``dvd_handle`` or opened by F.2). Mirrors
    # the success of decomp line 269's ``(**(code **)*param_2)(param_2,0)``
    # call. ``False`` means the rest of the state is the empty default.
    disc_opened: bool = False

    # Output of ``ifo_validate.detect_css`` — ``{"scrambled": bool|None,
    # "detected_by": "libdvdcss"|"unavailable"}``. The decomp's CSS init
    # at decomp lines 184-200 is handled inside libdvdread/libdvdcss for
    # us; this surface is diagnostic only (matches inspector report's
    # ``css`` field).
    css_state: Optional[Dict[str, Any]] = None

    # VMG IFO ``vmg_category`` field (uint32). The DVD-Video spec packs
    # the region-restrictions byte into bits 16-23 (set bit = region
    # blocked). Exposed raw for diagnostics; we don't enforce a region
    # gate (libdvdread internally surfaces read failures for blocked
    # discs).
    vmg_category: int = 0

    # Volume identifier from ``DVDUDFVolumeInfo`` / ISO fallback. Empty
    # string when the open failed.
    volume_id: str = ""

    # Disc-id MD5 (hex string) from ``DVDDiscID`` — 16 IFO bytes hashed.
    # Empty when unavailable.
    disc_id_md5: str = ""

    # ---- F.3: per-VTS IFO load state ---------------------------------------

    # Total titlesets declared in the VMG (``vmgi_mat.vmg_nr_of_title_sets``).
    # Decomp gates titleset count to 0..99 — MSG:3004 fires above. F.8
    # will wire that emit; for F.3 we simply record the count.
    vts_count: int = 0

    # Per-VTS metadata aggregated from the inspector's title_sets entries.
    # Keyed by 1-based VTS number. Each value carries the small surface
    # F.4/F.5's populators consult:
    #
    #   {
    #       "pgc_count":   number of PGCs in vts_pgcit
    #       "audio_count": declared VTSTT audio streams
    #       "sub_count":   declared VTSTT subtitle streams
    #       "title_count": number of titles claiming this VTS (per tt_srpt)
    #   }
    #
    # Missing key ⇒ VTS not yet observed (or failed to load — see
    # ``vts_failed`` for the explicit-failure flag).
    vts_info: Dict[int, Dict[str, int]] = field(default_factory=dict)

    # ---- Convenience accessors ---------------------------------------------

    def vts_claim_list_nonempty(self, vts_no: int) -> bool:
        """``True`` iff the per-VTS title-claim list for ``vts_no`` has
        any entries. Maps to cellwalk_primary's
        ``vts_state_skip_list_nonempty`` kwarg.
        """
        claims = self.vts_claim_lists.get(vts_no)
        return bool(claims)

    def vts_open_failed(self, vts_no: int) -> bool:
        """``True`` iff disc_open_enumerate's per-VTS open loop set the
        title-state ``+0xd8 = 1`` placeholder for ``vts_no``.
        """
        return bool(self.vts_failed.get(vts_no))

    @property
    def region_restrictions(self) -> int:
        """The 8-bit region-restrictions mask extracted from
        ``vmg_category`` per DVD-Video spec (bits 16-23). A set bit at
        position N means region (N+1) is BLOCKED. ``0`` ⇒ all-regions
        playable.
        """
        return (self.vmg_category >> 16) & 0xFF


# A shared empty-state singleton. Use this when no disc state is available
# (e.g. tests, callers that bypass ``analyze``); the empty state preserves
# the pre-F.1 behaviour of all hardcoded ``False`` defaults.
EMPTY_STATE = DiscState()


# ---------------------------------------------------------------------------
# Port entry point
# ---------------------------------------------------------------------------

def disc_open_enumerate(
    dvd_path: Optional[str] = None,
    *,
    dvd_handle: Any = None,
    report: Optional[dict] = None,
) -> DiscState:
    """Port of ``FUN_007d98d0`` — UDF mount, VMG read, per-VTS IFO walk,
    disc-state surfacing.

    **Current scope:** populates UDF / region / CSS state + per-VTS IFO
    load metadata from the inspector report. The two state-vector
    populators originally specified for Group F (``+0x130/+0x138`` and
    ``+0x1f8/+0x200``) turned out to be never-executed code in the
    public binary — see module docstring for the analysis.

    Resolution priority for state population:

      1. ``report`` (inspector dict) is the cheapest source — IFO parse
         already happened.
      2. ``dvd_path`` triggers a fresh open via
         ``libdvdread.open_disc`` + a single VMG IFO read for the
         category byte. Used when callers (e.g. unit tests) bypass the
         inspector.
      3. Neither → empty state (matches pre-F.1 behaviour exactly).

    The ``dvd_handle`` kwarg is accepted for future expansion (e.g. a
    per-VTS IFO walk that wants to avoid double-open) but currently
    unused.

    Args:
        dvd_path: filesystem path to the disc, ISO, or VIDEO_TS folder.
        dvd_handle: optional pre-opened libdvdread ``DVDReader *``.
            ``analyzer.analyze`` opens one for the phantom-scan loop;
            reserved for future per-VTS walks.
        report: optional inspector report dict. When supplied, reads
            CSS / VMG / volume / per-VTS info from it instead of
            re-walking the IFOs.

    Returns:
        A ``DiscState`` populated as far as the current F-slice allows.
        Empty state when no source is available.
    """
    state = DiscState()

    # ---- Source 1: inspector report (the cheapest path) --------------------
    if report:
        state.disc_opened = True
        css = report.get("css")
        if isinstance(css, dict):
            state.css_state = css
        vmg = report.get("vmg") or {}
        state.vmg_category = int(vmg.get("vmg_category") or 0)
        state.volume_id = str(report.get("volume_id") or "")
        state.disc_id_md5 = str(report.get("disc_id_md5") or "")

        # F.3: walk per-VTS title_sets to mark failed opens + collect
        # the small per-VTS surface F.4/F.5's populators consume.
        state.vts_count = int(vmg.get("num_title_sets") or 0)
        _populate_vts_info_from_report(state, report)
        return state

    # ---- Source 2: fresh libdvdread open ----------------------------------
    if dvd_path:
        from ...bindings import libdvdread as _dr
        from . import ifo_validate as _ifov
        try:
            state.css_state = _ifov.detect_css(dvd_path)
            with _dr.open_disc(dvd_path) as dvd:
                state.disc_opened = True
                vol_id, _vol_set = _dr.get_volume_info(dvd)
                state.volume_id = vol_id
                state.disc_id_md5 = _dr.get_disc_id(dvd)
                try:
                    with _dr.open_ifo(dvd, 0) as vmg:
                        state.vmg_category = int(
                            vmg.contents.vmgi_mat.contents.vmg_category)
                except _dr.DvdReadError:
                    # VMG IFO unreadable — leave vmg_category at 0.
                    pass
        except (_dr.DvdReadError, OSError):
            # Disc open failed entirely; leave defaults.
            state.disc_opened = False
        return state

    # ---- Source 3: nothing to read; return empty defaults ------------------
    _ = dvd_handle  # consumed by F.4+
    return state


# ---------------------------------------------------------------------------
# F.3 helper: per-VTS IFO load state from inspector report
# ---------------------------------------------------------------------------

def _populate_vts_info_from_report(state: DiscState, report: dict) -> None:
    """Walk ``report['title_sets']`` + ``report['titles']`` and populate
    ``state.vts_info`` / ``state.vts_failed``.

    Mirrors disc_open_enumerate's per-VTS open loop (decomp lines
    1442-1597). The decomp's loop allocates a per-VTS ``lVar29`` title
    state, calls ``FUN_007e46b0`` (ifo_bup_reader) to parse VMGI/VTSI,
    and sets ``lVar29[+0xd8] = 1`` when either the open OR the IFO read
    fails. The inspector's ``title_sets[N]['error']`` field captures the
    same condition (libdvdread's ``open_ifo`` raised — IFO unparseable).

    On success the decomp accumulates the VTS's title count via the
    engine call at lines 1587-1591 (key ``0xee1e67ba`` =
    ``nr_of_titles``) to advance its expected-sector cursor. We surface
    the inspector's already-parsed counts directly.
    """
    title_sets = report.get("title_sets") or []
    raw_titles = report.get("titles") or []

    # Count how many titles claim each VTS — mirrors the VMG tt_srpt
    # walk the decomp does at lines 539-606 (the MSG:3005 / MSG:3008
    # per-title loop). Inspector already parsed tt_srpt into
    # ``report['titles']``.
    title_count_by_vts: Dict[int, int] = {}
    for t in raw_titles:
        vno = int(t.get("vts") or 0)
        if vno > 0:
            title_count_by_vts[vno] = title_count_by_vts.get(vno, 0) + 1

    for ts in title_sets:
        vts_no = int(ts.get("vts") or 0)
        if vts_no <= 0:
            continue
        if "error" in ts:
            # title_state[+0xd8] = 1 placeholder. The decomp's
            # LAB_007dba21 path still registers this entry; downstream
            # walks skip it via the flag rather than scanning past it.
            state.vts_failed[vts_no] = True
            state.vts_info[vts_no] = {
                "pgc_count": 0,
                "audio_count": 0,
                "sub_count": 0,
                "title_count": title_count_by_vts.get(vts_no, 0),
            }
            continue

        state.vts_info[vts_no] = {
            "pgc_count": len(ts.get("pgcs") or []),
            "audio_count": len(ts.get("audio_streams") or []),
            "sub_count": len(ts.get("subtitle_streams") or []),
            "title_count": title_count_by_vts.get(vts_no, 0),
        }


__all__ = [
    "DiscState",
    "EMPTY_STATE",
    "disc_open_enumerate",
]
