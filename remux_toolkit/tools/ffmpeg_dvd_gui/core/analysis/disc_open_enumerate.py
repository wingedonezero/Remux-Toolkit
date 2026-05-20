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

    **F.1 status:** stub. Returns an empty ``DiscState`` whose presence
    in the analyzer wiring is observationally indistinguishable from the
    pre-F.1 hardcoded defaults.

    F.2-F.5 progressively fill in:

      * F.2 — verify UDF mount + region + CSS init (most already done by
        ``libdvdread.open_disc`` / ``ifo_validate.detect_css``; this slice
        will route the results into ``DiscState``).
      * F.3 — per-VTS IFO load loop mirroring decomp lines 1442-1597.
      * F.4 — disc-skip-list populator (``+0x130 / +0x138``).
      * F.5 — title-claim-list populator (``+0x1f8 / +0x200``) +
        ``title_state[+0xd8]`` binsearch wire-up.

    Args:
        dvd_path: filesystem path to the disc, ISO, or VIDEO_TS folder.
            Optional — F.1 stub ignores it.
        dvd_handle: optional pre-opened libdvdread ``DVDReader *``.
            ``analyzer.analyze`` opens one for the phantom-scan loop;
            passing it through avoids a double-open.
        report: optional inspector report dict. Once F.3 lands, the
            populator will reuse the inspector's parsed VMG / title_sets
            data instead of re-walking the IFO files.

    Returns:
        A populated ``DiscState``. F.1 returns ``EMPTY_STATE`` (an empty
        instance, not the module singleton — callers may mutate freely).
    """
    _ = (dvd_path, dvd_handle, report)  # consumed by F.2-F.5
    return DiscState()


__all__ = [
    "DiscState",
    "EMPTY_STATE",
    "disc_open_enumerate",
]
