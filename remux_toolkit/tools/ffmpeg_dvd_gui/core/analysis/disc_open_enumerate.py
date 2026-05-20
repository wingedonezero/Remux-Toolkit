"""
disc_open_enumerate — port of MakeMKV's ``FUN_007d98d0`` (28,812 B).

The 28-KB top-level DVD-open orchestrator that runs UDF mount + VMG/per-VTS
IFO loading + state-vector population + post-walk dedup. Emits MSG:3004,
3005, 3006, 3008, 3027, 4041 from its inner loops.

This module is the Group F port (AUDIT.md §4). It exists to populate the
two ``param_1`` state vectors that cellwalk_primary's IF/ELSE split and
title_init_validator's ``+0xd8`` binsearch consult:

  - ``+0x130 / +0x138`` — disc-level skip-list (vector start/end pair).
    Cellwalk_primary's iVar30 selection consults this; non-empty disables
    the iVar29 = 2 fallback in the IF branch.

  - ``+0x1f8 / +0x200`` — per-VTS title-claim list (vector start/end
    pair). title_init_validator binsearches this to write
    ``*(title_state + 0xd8)``; the resulting flag drives cellwalk_primary
    and structural_validator's deferred sub-codes 0x473a5c8c +
    0x4cb427d9.

This is the F.1 scaffold: ``DiscState`` is the data contract, and
``disc_open_enumerate()`` returns an empty state. F.2–F.5 fill in:

  - F.2 — UDF mount + region check + CSS init verification.
  - F.3 — per-VTS IFO load loop (decomp lines 1442–1597).
  - F.4 — disc-skip-list populator (``+0x130 / +0x138``).
  - F.5 — title-claim-list populator (``+0x1f8 / +0x200``) +
    title_init_validator binsearch wire-up writing
    ``title_state[+0xd8]``.

Behaviour with the empty state matches the pre-F.1 hardcoded defaults
(``disc_skip_list_nonempty=False`` + ``vts_state_skip_list_nonempty=False``
in cellwalk_primary), so wiring F.1 introduces no observable cross-val
delta.
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
    # Read by vts_title_scan @ decomp line 4181 and cellwalk_primary @
    # decomp lines 5306, 5320, 5363, 6615. Cellwalk's IF branch sets
    # ``iVar30 = 2`` when this vector is empty, otherwise picks
    # ``iVar30 = iVar29 = (claim_list_empty) + 1``.
    disc_skip_list_nonempty: bool = False

    # ``param_1[+0x1f8 / +0x200]`` — per-VTS title-claim list vector
    # pair. Holds per-VTS title-claim records that title_init_validator
    # binsearches to write ``*(title_state + 0xd8)``.
    #
    # Read by vts_title_scan @ decomp lines 4010-4011, 4149-4150 and
    # title_init_validator (FUN_007ed1f0 — port at
    # ``title_pre_filter.validate_title_init``, currently missing the
    # binsearch — see AUDIT.md U4/U5).
    #
    # Keyed by 1-based VTS number; missing key OR empty list means the
    # claim list is empty for that VTS (cellwalk's iVar29 = 2).
    #
    # Element shape will be defined when F.5 ports the populator. For
    # F.1 the dict stays empty.
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
# Port entry point (F.1 stub — F.2-F.5 fill in the body)
# ---------------------------------------------------------------------------

def disc_open_enumerate(
    dvd_path: Optional[str] = None,
    *,
    dvd_handle: Any = None,
    report: Optional[dict] = None,
) -> DiscState:
    """Port of ``FUN_007d98d0`` — UDF mount, VMG read, per-VTS IFO walk,
    state-vector population.

    **F.2 status:** populates UDF / region / CSS state. F.3-F.5 will
    add per-VTS IFO walk + state-vector populators.

    Resolution priority for state population:

      1. ``report`` (inspector dict) is the cheapest source — IFO parse
         already happened. F.2 reads ``report['css']``, ``report['vmg']``,
         ``report['volume_id']``, ``report['disc_id_md5']``.
      2. ``dvd_path`` triggers a fresh open via
         ``libdvdread.open_disc`` + a single VMG IFO read for the
         category byte. Used when callers (e.g. unit tests) bypass the
         inspector.
      3. Neither → empty state (matches pre-Group-F behaviour exactly).

    The ``dvd_handle`` kwarg is accepted but not consumed in F.2; F.3+
    will use it to avoid double-open during the per-VTS IFO walk.

    Args:
        dvd_path: filesystem path to the disc, ISO, or VIDEO_TS folder.
        dvd_handle: optional pre-opened libdvdread ``DVDReader *``.
            ``analyzer.analyze`` opens one for the phantom-scan loop;
            F.3+ will reuse it for the per-VTS walk.
        report: optional inspector report dict. When supplied, F.2 reads
            CSS / VMG / volume info from it instead of re-walking the
            IFOs.

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
