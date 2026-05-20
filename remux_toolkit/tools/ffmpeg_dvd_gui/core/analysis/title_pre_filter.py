"""
Port of MakeMKV's silent-drop gate trio (MASTER_MAP §7 P0):

    FUN_007ef130  →  is_title_collectible       (silent_title_filter.md, 2471 B)
    FUN_007ed1f0  →  validate_title_init        (init_validator.md,      5092 B)
    FUN_007ec6f0  →  evaluate_title             (full_decomp.md,         2802 B)

Each is a *semantic* port — same comparisons, same MSG codes, same pass/fail
verdict for the same disc — but reads parsed IFO fields via libdvdread's
named accessors instead of mirroring the engine's byte-offset arithmetic.

The decomp's obfuscation engine (FUN_00487e40 + KEY_A=0x81671f8 callsites)
gets replaced with direct ``ifo.vtsi_mat.nr_of_vts_audio_streams`` style
reads (see MASTER_MAP §2 + §6 for the equivalence proof — both routes
return the same byte from the same on-disc file). Dev-key-gated branches
(``DAT_008bc5e9 != '\0'``) are dropped as dead code in the public binary
per MASTER_MAP §1 Tier B.

The three functions emit MSG codes via mkv_msg_log when their internal
sanity checks fire:

    3009  TTN not found in title set        (FUN_007ef130 at LAB_007ef29f)
    3010  Cells not found for VTS/TTN/PGCN/PGN  (FUN_007ef130 + FUN_007ed1f0)
    3011  Audio stream count out of bounds  (FUN_007ed1f0 line 250)
    3012  Sub stream count out of bounds    (FUN_007ed1f0 line 411)
    3015  Title navigation error            (FUN_007ec6f0 at uVar12=0xbc7)
    3016  Title skipped                     (FUN_007ec6f0 at uVar12=0xbc8)
    3025  Title below minimum duration      (FUN_007ec6f0 at uVar12=0xbd1)
    3026  Declared vs actual mismatch       (FUN_007ec6f0 at uVar12=0xbd2)
    3028  Title added                       (FUN_007ec6f0 at uVar12=0xbd4)
    3040  Angle added                       (FUN_007ec6f0 at uVar12=0xbe0)
    3041  Angle add failed                  (FUN_007ec6f0 at uVar12=0xbe1)

The decomp's MSG:3009/3010/3011/3012 (cell-list and stream-count sanity
sub-codes) are emitted by their owning gate function on failure. They are
INFO-level — MakeMKV doesn't surface them in messages.log at default log
level (per FUN_00800d90's per-thread log-level gate) but they are useful
in our debug log for diagnosing why a title was rejected.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from . import mkv_msg_log
from . import cell_trim
from . import cell_validator
from . import cellwalk_primary as _cwp
from . import disc_open_enumerate as _doe


_log = logging.getLogger("remux_toolkit.title_pre_filter")


# ---------------------------------------------------------------------------
# Caps from FUN_007ed1f0 — see init_validator.md
# ---------------------------------------------------------------------------

#: Audio-stream-count cap from FUN_007ed1f0 line 250 (``if (uVar23 < 9)``).
#: Decomp reads the count via engine key 0x23c04745; libdvdread exposes
#: this as ``vtsi_mat.nr_of_vts_audio_streams``. Discs with > 8 audio
#: streams (impossible per DVD spec) are dropped via MSG:3011.
MAX_VTS_AUDIO_STREAMS = 8

#: Sub-stream-count cap from FUN_007ed1f0 line 411 (``if (uVar23 < 0x21)``).
#: Decomp reads via engine key 0x5ffc422a; libdvdread exposes as
#: ``vtsi_mat.nr_of_vts_subp_streams``. Cap is 32 per DVD spec.
MAX_VTS_SUB_STREAMS = 32

#: Angle-count cap from FUN_007ed1f0 line 123 (``if (8 < bVar25)``). DVD
#: spec allows max 9 angles but MakeMKV clamps at 8. Source field is
#: TT_SRPT.title[N].nr_of_angles.
MAX_TT_ANGLES = 8

# ---------------------------------------------------------------------------
# Min-duration and fake-detection thresholds from FUN_007ec6f0
# ---------------------------------------------------------------------------

#: Absolute delta (seconds) for MSG:3026 fake-detection. From FUN_007ec6f0
#: line 4491 literal ``300 < uVar8`` where uVar8 = abs(declared - actual).
FAKE_DURATION_ABS_DELTA_S = 300.0

#: Relative delta (percentage) for MSG:3026 fake-detection. From line 4491
#: literal ``0x1e < (uVar8 * 100) / uVar5`` where uVar5 = actual_duration.
FAKE_DURATION_REL_DELTA_PCT = 30.0

def _cell_dict_to_meta(c: dict) -> cell_trim.CellMeta:
    """Build a ``CellMeta`` from an inspector cell dict. Mirrors
    ``cell_trim.cell_metadata_from_pgc`` for the dict-form inputs the
    analyzer carries."""
    return cell_trim.CellMeta(
        index=int(c.get("cell") or 0),
        cell_cmd_nr=int(c.get("cell_cmd_nr") or 0),
        cell_type=int(c.get("cell_type") or 0),
        block_type=int(c.get("block_type") or 0),
        block_mode=int(c.get("block_mode") or 0),
        duration_s=float(c.get("duration_seconds") or 0.0),
        first_sector=int(c.get("first_sector") or 0),
        last_sector=int(c.get("last_sector") or 0),
        vob_id_nr=int(c.get("vob_id_nr") or 0),
        interleaved=bool(c.get("interleaved")),
    )


def _annotate_block_ranges(cells: List[cell_trim.CellMeta]) -> None:
    """In-place mirror of ``cell_trim.cell_metadata_from_pgc``'s
    second-pass angle-block grouping: collapse consecutive
    block_mode 1→2→3 runs into shared (range_start, range_end)."""
    n = len(cells)
    i = 0
    while i < n:
        if cells[i].block_mode == 1:
            j = i
            while j < n and cells[j].block_mode != 3:
                j += 1
            j = min(j, n - 1)
            block_start = cells[i].index
            block_end = cells[j].index
            for k in range(i, j + 1):
                cells[k].range_start = block_start
                cells[k].range_end = block_end
            i = j + 1
        else:
            i += 1


def _cell_is_content_dict(cell_meta: cell_trim.CellMeta,
                           vm_cmd_hex: Optional[str]) -> bool:
    """Mirror of ``cell_trim.cell_is_content`` but takes the VM command
    bytes directly (from the inspector dict's ``vm_command_bytes``
    field) instead of a libdvdread PGC pointer.

    Replicates the literal control-flow of cell_is_content (cell_trim.py
    lines 179-200):

      1. is_fake → False
      2. num_sectors == 0 → False
      3. cell_cmd_nr != 0 → run the VM-opcode classifier
         (``cell_validator._cell_command_indicates_content``) on the
         8-byte command. If no bytes available, falls back to False
         (matching cell_validator.cell_has_content_command's behaviour
         when the command table is null).
      4. cell_cmd_nr == 0 → duration_s >= MIN_CONTENT_DURATION_S
    """
    if cell_meta.is_fake:
        return False
    if cell_meta.num_sectors == 0:
        return False
    if cell_meta.cell_cmd_nr != 0:
        if vm_cmd_hex is None:
            return False
        try:
            cmd_bytes = bytes.fromhex(vm_cmd_hex)
        except ValueError:
            return False
        if len(cmd_bytes) != 8:
            return False
        return cell_validator._cell_command_indicates_content(cmd_bytes)
    return cell_meta.duration_s >= cell_trim.MIN_CONTENT_DURATION_S


def _cell_is_content_byzantine_dict(cell_meta: cell_trim.CellMeta,
                                     vm_cmd_hex: Optional[str],
                                     all_metas: List[cell_trim.CellMeta],
                                     vm_bytes_by_index: dict) -> bool:
    """Mirror of ``cell_trim.cell_is_content_byzantine`` (the port of
    FUN_007ea3d0 / cell_validator_primary) but uses the dict-form
    ``vm_command_bytes`` accessor instead of a libdvdread PGC pointer.

    The three duration regimes from the decomp are honoured byte-for-byte:

      Regime 1 (< 1 sec): trivial. Without a sliding ``reference``
        (caller doesn't pass one here), returns False.
      Regime 2 (1 - 5 sec): ambiguous. Defer to the ``interleaved``
        flag (= MakeMKV cell-record +0x0c angle-block-fragment bit).
        If set, evaluate via ``_cell_is_content_dict``; otherwise drop.
      Regime 3 (>= 5 sec): main content path. Run
        ``_cell_is_content_dict``.
    """
    if cell_meta.is_fake:
        return False
    if cell_meta.duration_s == 0.0:
        return False
    if cell_meta.duration_s < cell_validator.CELL_DURATION_TRIVIAL_S:
        return False  # regime 1, no reference
    if cell_meta.duration_s < cell_validator.CELL_DURATION_AMBIGUOUS_S:
        if cell_meta.interleaved:
            return _cell_is_content_dict(cell_meta, vm_cmd_hex)
        return False
    return _cell_is_content_dict(cell_meta, vm_cmd_hex)


def cellwalk_keeps_any_cells(cells: list) -> bool:
    """Mirror of FUN_007f3eb0 + method[0x10] for the silent-drop
    discriminator: does the cellwalk pass leave any cell standing?

    Implementation: walks the cell list and asks the dict-aware
    byzantine validator (``_cell_is_content_byzantine_dict``) — the
    same logic as ``cell_trim.cell_is_content_byzantine`` (which is
    the byte-exact port of FUN_007ea3d0) but consults the cell's
    ``vm_command_bytes`` for cells with ``cell_cmd_nr != 0`` instead
    of a libdvdread PGC pointer.

    Args:
        cells: list of inspector-style cell dicts.

    Returns True if at least one cell would survive the cellwalk's
    content predicate; False when every cell looks like a navigation
    stub.
    """
    if not cells:
        return False
    metas = [_cell_dict_to_meta(c) for c in cells]
    vm_bytes_by_index = {
        int(c.get("cell") or 0): c.get("vm_command_bytes")
        for c in cells
    }
    for c in metas:
        if _cell_is_content_byzantine_dict(
            c, vm_bytes_by_index.get(c.index), metas, vm_bytes_by_index,
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class CollectibleResult:
    """Outcome of ``is_title_collectible`` (FUN_007ef130 port)."""

    #: True if the title's TT_SRPT entry + VTS_PTT_SRPT walk + every
    #: referenced (PGCN, PGN) tuple validated cleanly. False → silent
    #: drop (caller emits no further MSG; this function emits MSG:3009
    #: or MSG:3010 internally on the specific failure).
    ok: bool

    #: Human-readable reason for ``ok=False``. Empty when ok=True.
    reason: str = ""

    #: PTT chain as (pgcn, pgn) pairs. Populated even on partial failure
    #: so downstream can see what was being walked.
    ptt_chain: List[tuple[int, int]] = field(default_factory=list)


@dataclass(slots=True)
class InitResult:
    """Outcome of ``validate_title_init`` (FUN_007ed1f0 port)."""

    ok: bool
    reason: str = ""
    audio_count: int = 0
    sub_count: int = 0
    angle_count: int = 1
    pgc_category: int = 0


# ---------------------------------------------------------------------------
# Ports of the two title-state helpers that title_evaluator calls
# between cellwalk and the MSG:3026/3028 decision (FUN_007f3d30 +
# FUN_007f3e30). These read fields populated by cellwalk_primary —
# our partial cellwalk port (cell_trim.py) doesn't populate the
# exact field offsets MakeMKV uses, so the dict-form equivalents
# are wired to the inspector's already-parsed values.
# ---------------------------------------------------------------------------

def cell_group_count(cells_after_trim: int) -> int:
    """Port of FUN_007f3d30 (method[0x10] of plVar6's vtable @ slot 2).

    Decomp body (13 bytes total):

        return (plVar6[+0x30] - plVar6[+0x28]) >> 3;

    plVar6+0x28..+0x30 is plVar6's cell-group vector (8-byte entries).
    The return value is the count. Equivalent in our model to
    ``cells_after_trim`` since both denote "how many cell entries
    survived cellwalk's curation."
    """
    return max(0, int(cells_after_trim or 0))


def title_state_duration_mismatch(declared_s: float, actual_s: float,
                                    *, cellwalk_run_vector_nonempty: bool) -> bool:
    """Port of FUN_007f3e30 (113 bytes).

    Direct port of the decomp body (dev-key path at line 3749 dropped
    per MASTER_MAP §1 Tier B):

        if title_state[+0xe0] != title_state[+0xe8]:
            uVar1 = title_state[+0x100]   # actual_s
            uVar2 = title_state[+0x104]   # declared_s
            uVar3 = abs(uVar1 - uVar2)
            if uVar1 == 0:
                return 1
            if 300 < uVar3 and 30 < (uVar3 * 100) / uVar1:
                return 1
        return 0

    title_state[+0xe0..+0xe8] is the cellwalk-populated "kept angle-
    block range" vector (populated by ``title_trim_cell_range``,
    FUN_007f1720, at line 1633 of chunk_007f.md — already PARTIAL in
    cell_trim.py). The vector is non-empty iff the cellwalk decided
    the title has any angle-block-validated content.

    Args:
        declared_s: title_state[+0x104], PGC.playback_time in seconds
        actual_s:   title_state[+0x100], cellwalk's actual seconds
        cellwalk_run_vector_nonempty: True iff +0xe0/+0xe8 was
            populated by cellwalk. Computed by the caller from
            cellwalk's output. Required for faithful semantics —
            see FIXME below.
    """
    if not cellwalk_run_vector_nonempty:
        return False
    actual = int(actual_s or 0)
    declared = int(declared_s or 0)
    abs_delta = abs(declared - actual)
    if actual == 0:
        return True
    if 300 < abs_delta and (abs_delta * 100) // actual > 30:
        return True
    return False


@dataclass(slots=True)
class EvaluatorResult:
    """Outcome of ``evaluate_title`` (FUN_007ec6f0 port).

    ``classification`` mirrors the GUI-side keys in analyzer.py:

        "added"      — title emits MSG:3028, shown by default
        "fake_title" — title emits MSG:3026, hidden by default
        "silent"     — title emits no MSG (FUN_007ef130 dropped it
                       structurally, or cellwalk+trim left 0 cells in
                       an audio-bearing VTS); hidden by default
        "skipped_init" — FUN_007ed1f0 returned 0 → MSG:3016 emitted
        "skipped_nav"  — cellwalk returned 0 → MSG:3015 emitted
        "skipped_short" — duration below min preference (always 0 in
                          public binary, so this path is unreachable
                          unless a future caller opts into it)
    """

    classification: str
    msg_code: Optional[int]
    hidden_by_default: bool
    reason: str
    declared_duration_s: float
    actual_duration_s: float

    #: Sub-results from the gates that ran.
    collectible: CollectibleResult = field(default_factory=lambda: CollectibleResult(ok=True))
    init: InitResult = field(default_factory=lambda: InitResult(ok=True))

    #: Per-title trace line for the debug log — concatenation of decisions.
    trace: str = ""


# ---------------------------------------------------------------------------
# FUN_007ef130 → is_title_collectible
# ---------------------------------------------------------------------------

def is_title_collectible(title: dict, vts: dict) -> CollectibleResult:
    """Port of FUN_007ef130 ("title_cell_list_collector", 2471 B).

    Validates that a title's PTT chain references PGCs + cells that
    actually exist in the VTS. The decomp builds a per-PTT cell-record
    list as a side effect; we only need the *validity* outcome — cell
    iteration happens later via cell_trim / inspector.

    Args:
        title: dict with keys ``vts``, ``vts_ttn``. The vts_ttn is the
            title's index within its VTS (1-based, from TT_SRPT).
        vts:   dict with keys ``vts``, ``pgcs`` (list of pgc dicts each
            with ``pgc``, ``num_programs``, ``num_cells``), and
            ``ptt_map`` (dict ``{vts_ttn: [{pgc: N, program: M}, ...]}``).

    Emits MSG:3009 when ``vts_ttn`` is 0 or out of bounds; MSG:3010
    when any PTT references an out-of-bounds PGCN/PGN or a PGC with no
    cell array.

    Returns ``CollectibleResult`` — caller silently drops the title on
    ``ok=False``.
    """
    title_no = int(title.get("title") or 0)
    vts_no = int(title.get("vts") or 0)
    vts_ttn = int(title.get("vts_ttn") or 0)

    # FUN_007ef130 line 105: if (bVar13 == 0) → emit MSG:3009 (0xbc1).
    # bVar13 is TT_SRPT[N].vts_ttn. Per DVD spec a valid vts_ttn is
    # >= 1; zero means "not assigned to any title within the VTS".
    if vts_ttn == 0:
        mkv_msg_log.emit(3009, title_no, vts_no,
                         title=title_no, vts=vts_no, vts_ttn=0)
        return CollectibleResult(ok=False, reason="vts_ttn=0")

    # The VTS_PTT_SRPT.nr_of_srpts check (FUN_007ef130 line 120:
    # if (uVar15 < uVar3)). uVar15 = num_ttn from VTS_PTT_SRPT,
    # uVar3 = vts_ttn. libdvdread already iterates safely, so checking
    # whether ptt_map has the requested vts_ttn covers both this and
    # the equivalent bounds check.
    ptt_entries = vts.get("ptt_map", {}).get(vts_ttn) or vts.get("ptt_map", {}).get(str(vts_ttn))
    if not ptt_entries:
        mkv_msg_log.emit(3009, title_no, vts_no,
                         title=title_no, vts=vts_no, vts_ttn=vts_ttn)
        return CollectibleResult(ok=False, reason=f"vts_ttn={vts_ttn} not in VTS_PTT_SRPT")

    pgcs_by_idx = {int(p.get("pgc") or 0): p for p in vts.get("pgcs", [])}
    num_pgcs = len(vts.get("pgcs", []))

    chain: List[tuple[int, int]] = []
    for entry in ptt_entries:
        pgcn = int(entry.get("pgc") or 0)
        pgn = int(entry.get("program") or 0)
        chain.append((pgcn, pgn))

        # FUN_007ef130 line 287-308: validate the PGCN reference.
        # if pgcn == 0 OR pgcn > num_pgcs OR pgcs[pgcn-1] == NULL → MSG:3010
        if pgcn == 0 or pgcn > num_pgcs:
            mkv_msg_log.emit(3010, vts_no, vts_ttn, pgcn, pgn,
                             title=title_no, reason="pgcn-out-of-bounds")
            return CollectibleResult(ok=False, ptt_chain=chain,
                                     reason=f"pgcn={pgcn} out of [1,{num_pgcs}]")

        pgc = pgcs_by_idx.get(pgcn)
        if pgc is None:
            mkv_msg_log.emit(3010, vts_no, vts_ttn, pgcn, pgn,
                             title=title_no, reason="pgc-null")
            return CollectibleResult(ok=False, ptt_chain=chain,
                                     reason=f"pgc[{pgcn}] is NULL")

        # FUN_007ef130 line 315-323: validate PGN against the PGC's
        # nr_of_programs, and that cell_playback array is non-null.
        num_programs = int(pgc.get("num_programs") or 0)
        num_cells = int(pgc.get("num_cells") or 0)
        if pgn == 0 or pgn > num_programs:
            mkv_msg_log.emit(3010, vts_no, vts_ttn, pgcn, pgn,
                             title=title_no, reason="pgn-out-of-bounds")
            return CollectibleResult(ok=False, ptt_chain=chain,
                                     reason=f"pgn={pgn} out of [1,{num_programs}] for pgc={pgcn}")

        if num_cells == 0:
            mkv_msg_log.emit(3010, vts_no, vts_ttn, pgcn, pgn,
                             title=title_no, reason="no-cells")
            return CollectibleResult(ok=False, ptt_chain=chain,
                                     reason=f"pgc={pgcn} has no cells")

    return CollectibleResult(ok=True, ptt_chain=chain)


# ---------------------------------------------------------------------------
# FUN_007ed1f0 → validate_title_init
# ---------------------------------------------------------------------------

def validate_title_init(title: dict, vts: dict, *,
                        angle: int = 0) -> InitResult:
    """Port of FUN_007ed1f0 ("title_init_validator", 5092 B).

    Validates the title's structural metadata: angle index in range,
    audio stream count within DVD spec cap, sub stream count within
    DVD spec cap, first PGC has cells, etc.

    Emits MSG:3010 (cell-list problem), MSG:3011 (audio count bad),
    MSG:3012 (sub count bad). Returns InitResult; ok=False → caller
    emits MSG:3016 and silently skips (per FUN_007ec6f0 path at line 4424).

    Args:
        title: dict with ``vts``, ``vts_ttn``, ``num_angles``, ``pgc``
        vts:   dict with ``vts``, ``pgcs``, ``audio_streams``, ``subtitle_streams``
        angle: requested angle index, 0-based. FUN_007ec6f0 calls with
               0 on first eval, then 1..n_angles-1 for additional
               angles in a multi-angle title.
    """
    title_no = int(title.get("title") or 0)
    vts_no = int(title.get("vts") or 0)
    vts_ttn = int(title.get("vts_ttn") or 0)
    pgcn = int(title.get("pgc") or 0)

    # Audio stream count cap (FUN_007ed1f0 line 250: if uVar23 < 9).
    audio_count = len(vts.get("audio_streams") or [])
    if audio_count > MAX_VTS_AUDIO_STREAMS:
        mkv_msg_log.emit(3011, audio_count, vts_no,
                         title=title_no, vts=vts_no)
        return InitResult(ok=False, audio_count=audio_count,
                          reason=f"audio_count={audio_count} > {MAX_VTS_AUDIO_STREAMS}")

    # Sub stream count cap (FUN_007ed1f0 line 411: if uVar23 < 0x21).
    sub_count = len(vts.get("subtitle_streams") or [])
    if sub_count > MAX_VTS_SUB_STREAMS:
        mkv_msg_log.emit(3012, sub_count, vts_no,
                         title=title_no, vts=vts_no)
        return InitResult(ok=False, audio_count=audio_count, sub_count=sub_count,
                          reason=f"sub_count={sub_count} > {MAX_VTS_SUB_STREAMS}")

    # Angle bounds (FUN_007ed1f0 line 123: if (8 < bVar25) clamp; line 129:
    # if (bVar25 <= param_5) drop). nr_of_angles==0 is treated as 1 by
    # the decomp's LAB_007ed338 (forces angle_count to 1).
    nr_of_angles = int(title.get("num_angles") or 0)
    angle_count = nr_of_angles or 1
    if angle_count > MAX_TT_ANGLES:
        angle_count = MAX_TT_ANGLES
    if angle >= angle_count:
        # Requested angle out of range — silent drop (no specific MSG
        # in the decomp here, only the debug emitter via FUN_0066d8a0).
        return InitResult(ok=False, audio_count=audio_count, sub_count=sub_count,
                          angle_count=angle_count,
                          reason=f"angle={angle} >= angle_count={angle_count}")

    # First PGC's cell array must be non-null. FUN_007ed1f0 line 188:
    # if (lVar35 == 0) → emit MSG:3010 (uVar28 = 0xbc2).
    pgcs_by_idx = {int(p.get("pgc") or 0): p for p in vts.get("pgcs", [])}
    pgc = pgcs_by_idx.get(pgcn)
    if pgc is None:
        mkv_msg_log.emit(3010, vts_no, vts_ttn, pgcn, 1,
                         title=title_no, reason="pgc-missing")
        return InitResult(ok=False, audio_count=audio_count, sub_count=sub_count,
                          angle_count=angle_count,
                          reason=f"pgc={pgcn} missing in VTS_PGCIT")
    if int(pgc.get("num_cells") or 0) == 0:
        mkv_msg_log.emit(3010, vts_no, vts_ttn, pgcn, 1,
                         title=title_no, reason="cell-array-empty")
        return InitResult(ok=False, audio_count=audio_count, sub_count=sub_count,
                          angle_count=angle_count,
                          reason=f"pgc={pgcn} has empty cell_playback array")

    # All gates passed.
    return InitResult(
        ok=True,
        audio_count=audio_count,
        sub_count=sub_count,
        angle_count=angle_count,
        pgc_category=int(title.get("pgc_category") or 0),
    )


# ---------------------------------------------------------------------------
# FUN_007ec6f0 → evaluate_title
# ---------------------------------------------------------------------------

class _DictPgcProxy:
    """Adapter that satisfies cell_trim's pgc-pointer needs from an
    inspector pgc dict. cell_trim's deciders read ``pgc.playback_time``
    via cell_trim._dvdtime_to_seconds and ``pgc.command_tbl`` via
    cell_validator.cell_has_content_command. We synthesize both."""

    __slots__ = ("playback_time", "command_tbl", "_total_s")

    def __init__(self, pgc_dict: dict):
        # Build a minimal playback_time-like object that
        # _dvdtime_to_seconds can read. The real DvdTime has BCD
        # bytes; cell_trim does try/except around the call, so we
        # raise to fall through to the "no pgc time" path.
        self._total_s = float(pgc_dict.get("duration_seconds") or 0.0)

        class _PlaybackTime:
            total_seconds = self._total_s
            hour = 0
            minute = 0
            second = 0
            frame_u = 0

        self.playback_time = _PlaybackTime()
        # cell_validator.cell_command_at checks `if not pgc.command_tbl`
        # — None is falsy, so cell_has_content_command returns False
        # for any cell with cell_cmd_nr != 0. That matches our dict
        # path where vm_command_bytes is consulted separately.
        self.command_tbl = None


def evaluate_title(title: dict, vts: dict, *,
                   cells_after_trim: Optional[int] = None,
                   actual_duration_after_trim_s: Optional[float] = None,
                   has_active_audio: Optional[bool] = None,
                   disc_state: Optional[_doe.DiscState] = None) -> EvaluatorResult:
    """Port of FUN_007ec6f0 ("title_evaluator", 2802 B).

    Drives the MSG:3015/3016/3025/3026/3028 emission for a title.

    The decomp's order of operations (lines 4376–4630):

      1.  If pre-filter (FUN_007ef130) returned empty cell-list (here:
          ``is_title_collectible`` returned ok=False) → silent return.
      2.  Call init validator (``validate_title_init`` with angle=0).
          On fail → MSG:3016 "Title skipped".
      3.  Compute declared-duration (BCD HMSF in seconds). Min-duration
          gate (line 4448) — in public binary the threshold is 0, so
          this always passes. We expose ``actual_duration_after_trim_s``
          as the post-cellwalk value.
      4.  Cellwalk gate (FUN_007f3eb0). We approximate this with the
          existing cell_trim.decide_trim result expressed as
          ``cells_after_trim`` + ``actual_duration_after_trim_s``.
          If the cellwalk-equivalent says 0 cells survived:
            - VTS has any active audio → silent (matches MakeMKV's
              "method[0x10] returned 0 + list non-empty + FUN_007f3e30
              returns false" path at line 4615 → LAB_007ecc0c
              "Title skip vts=X pgcn=Y" debug print only).
            - VTS has no active audio → MSG:3026 (fake title with no
              audio backing).
      5.  Cells survived → compute declared-vs-actual delta. Line 4490:
            if (actual == 0 OR (300 < abs_delta AND 30 < rel_pct)):
                emit MSG:3026
            else:
                emit MSG:3028 "Title added"

    Args:
        title: inspector-style dict (title, vts, vts_ttn, pgc,
               duration_seconds, duration_seconds_cell_sum, num_cells)
        vts:   inspector-style VTS dict (audio_streams, subtitle_streams,
               pgcs[], ptt_map)
        cells_after_trim: number of cells surviving the cell_trim pass
            (if None, defaults to ``title['num_cells']`` minus any
            ``trim_start``/``trim_end`` the caller already applied).
        actual_duration_after_trim_s: seconds-of-content after trim.
            If None, defaults to ``title['duration_seconds_cell_sum']``.
        has_active_audio: if None, derived from
            ``len(title['audio_streams']) > 0`` falling back to
            ``len(vts['audio_streams']) > 0``.
        disc_state: optional ``DiscState`` from
            ``disc_open_enumerate.disc_open_enumerate``. Feeds
            cellwalk_primary's ``disc_skip_list_nonempty`` and
            ``vts_state_skip_list_nonempty`` kwargs. When ``None``, both
            default to ``False`` — matching the pre-Group-F behaviour.
    """
    title_no = int(title.get("title") or 0)
    vts_no = int(title.get("vts") or 0)
    pgcn = int(title.get("pgc") or 0)

    # ---- Step 1: pre-filter ---------------------------------------------
    coll = is_title_collectible(title, vts)
    if not coll.ok:
        return EvaluatorResult(
            classification="silent",
            msg_code=None,           # FUN_007ef130 returns 0 → caller
                                     # (title_evaluator) sees empty list
                                     # and silently returns (line 4376).
            hidden_by_default=True,
            reason=f"collectible-fail: {coll.reason}",
            declared_duration_s=float(title.get("duration_seconds") or 0.0),
            actual_duration_s=float(title.get("duration_seconds_cell_sum") or 0.0),
            collectible=coll,
            trace=f"pre-filter=FAIL ({coll.reason})",
        )

    # ---- Step 2: init validator -----------------------------------------
    init = validate_title_init(title, vts, angle=0)
    if not init.ok:
        # MSG:3016 emission (uVar12 = 0xbc8). Decomp line 4434 also
        # prints "Title skip vts=%u pgcn=%u" via FUN_0081cc10 which is
        # the debug-stream printer; we attach it to the MSG entry's
        # text by passing the same args.
        mkv_msg_log.emit(3016, title_no, vts_no,
                         title=title_no, vts=vts_no, pgc=pgcn,
                         reason=init.reason)
        return EvaluatorResult(
            classification="skipped_init",
            msg_code=3016,
            hidden_by_default=True,
            reason=f"init-fail: {init.reason}",
            declared_duration_s=float(title.get("duration_seconds") or 0.0),
            actual_duration_s=float(title.get("duration_seconds_cell_sum") or 0.0),
            collectible=coll,
            init=init,
            trace=f"pre-filter=PASS init={init.reason or 'PASS'} verdict=SKIPPED_INIT msg=3016",
        )

    # ---- Resolve cellwalk-equivalent inputs -----------------------------
    declared = float(title.get("duration_seconds") or 0.0)
    if actual_duration_after_trim_s is None:
        actual = float(title.get("duration_seconds_cell_sum") or 0.0)
    else:
        actual = float(actual_duration_after_trim_s)

    if cells_after_trim is None:
        cells_after_trim = int(title.get("num_cells") or 0)

    if has_active_audio is None:
        # Use VTS-declared audio count (not the post-phantom-filter
        # title list). Reason: the discriminator is whether the VTS was
        # *authored* with audio — a sign of "real content" intent. The
        # title's per-PGC audio list can be empty post-phantom-filter
        # (VOB stub doesn't carry audio) even when the VTS itself has
        # audio streams declared.
        has_active_audio = len(vts.get("audio_streams") or []) > 0

    # ---- Step 3: min-duration gate (public binary: always passes) ------
    # FUN_007ec6f0 lines 4446-4448. Min-duration threshold is dev-gated;
    # in public binary it reads 0. We keep the gate present so the
    # MSG:3025 emission path stays reachable if someone later wires
    # in a user-facing minimum.
    min_duration_s = 0.0
    if declared > 0 and declared < min_duration_s:
        mkv_msg_log.emit(3025, title_no, _hms(declared), _hms(min_duration_s),
                         title=title_no, vts=vts_no)
        return EvaluatorResult(
            classification="skipped_short",
            msg_code=3025,
            hidden_by_default=True,
            reason=f"declared={declared:.1f}s < min={min_duration_s:.1f}s",
            declared_duration_s=declared,
            actual_duration_s=actual,
            collectible=coll, init=init,
            trace=f"pre-filter=PASS init=PASS verdict=SKIPPED_SHORT msg=3025",
        )

    # ---- Step 4: cellwalk_primary call (FUN_007f3eb0 port) --------------
    #
    # Direct call into the cellwalk_primary port (cellwalk_primary.py).
    # The CellwalkResult captures the plVar6 fields that title_evaluator's
    # lines 4078-4163 read back:
    #
    #   result.ok              ← cellwalk's return value (LAB_007f625b
    #                            vs LAB_007f62db)
    #   result.cell_group_count ← method[0x10] = FUN_007f3d30
    #   result.run_vector_nonempty ← plVar6[+0xe0/+0xe8] non-empty
    #   result.actual_seconds  ← plVar6[+0x20] / [+0x100]
    #   result.declared_seconds ← plVar6[+0x104]
    #
    # The cellwalk's IF/ELSE split + 3 trim deciders + LAB_007f4604
    # population are all inside cellwalk_primary. Empirical-rule
    # approximations specific to the "authoring-fake VTS" case live
    # there with FIXME markers pointing at the remaining gaps in the
    # 9340 B FUN_007f3eb0 port.
    #
    # If we have cell-level data, run cellwalk on it. Otherwise the
    # legacy ``cells_after_trim`` parameter is used as a fallback.
    pgcn = title.get("pgc")
    cells_dict: list[dict] = []
    if vts and pgcn is not None:
        for p in vts.get("pgcs", []):
            if p.get("pgc") == pgcn:
                cells_dict = p.get("cells") or []
                break
    if cells_dict:
        # Convert inspector cell dicts → CellMeta + a pgc-proxy.
        cells_meta = [_cell_dict_to_meta(c) for c in cells_dict]
        # Apply angle-block range grouping (cell_metadata_from_pgc
        # post-processing).
        _annotate_block_ranges(cells_meta)
        pgc_dict = next(
            (p for p in (vts.get("pgcs") or []) if p.get("pgc") == pgcn),
            {}
        )
        pgc_proxy = _DictPgcProxy(pgc_dict)
        vts_pgcs_count = len(vts.get("pgcs") or [])
        vts_audio_count = len(vts.get("audio_streams") or [])
        # Group F: thread disc-level state from disc_open_enumerate
        # into cellwalk's IF/ELSE split. With an empty DiscState (F.1
        # default) the kwargs evaluate to False — same observable as
        # the pre-Group-F hardcoded edge.
        ds = disc_state or _doe.EMPTY_STATE
        cw = _cwp.cellwalk_primary(
            cells_meta, pgc_proxy,
            title_id=title_no, vts_no=vts_no, pgc_no=pgcn,
            disc_skip_list_nonempty=ds.disc_skip_list_nonempty,
            vts_state_skip_list_nonempty=ds.vts_claim_list_nonempty(vts_no),
            vts_has_audio=(vts_audio_count > 0),
            vts_pgc_count=vts_pgcs_count,
        )
    else:
        # No cell-level data — synthesize a "trivially success" result
        # from the title-level summary so the duration delta gate can
        # still fire.
        cw = _cwp.CellwalkResult(
            ok=True,
            cell_group_count=int(cells_after_trim or 0),
            run_vector_nonempty=int(cells_after_trim or 0) > 0,
            actual_seconds=int(actual),
            declared_seconds=int(declared),
            trace="no-cell-data-fallback",
        )

    # cellwalk_primary returned 0 → MSG:3015 (FUN_007ec6f0 line 4084).
    if not cw.ok:
        mkv_msg_log.emit(3015, title_no, _hms(declared),
                         title=title_no, vts=vts_no, pgc=pgcn,
                         reason=cw.trace)
        return EvaluatorResult(
            classification="skipped_nav",
            msg_code=3015,
            hidden_by_default=True,
            reason=f"cellwalk-fail: {cw.trace}",
            declared_duration_s=declared,
            actual_duration_s=actual,
            collectible=coll, init=init,
            trace=(f"pre-filter=PASS init=PASS cellwalk=FAIL "
                   f"({cw.trace}) verdict=SKIPPED_NAV msg=3015"),
        )

    # Now use cellwalk's outputs for the title_evaluator gates.
    cgcount = cw.cell_group_count
    dur_mismatch = title_state_duration_mismatch(
        cw.declared_seconds, cw.actual_seconds,
        cellwalk_run_vector_nonempty=cw.run_vector_nonempty,
    )
    proceed = cgcount > 0 or dur_mismatch
    # Use cellwalk-computed seconds for the downstream MSG:3026 delta.
    actual = float(cw.actual_seconds)
    declared = float(cw.declared_seconds) if cw.declared_seconds > 0 else declared

    if not proceed:
        # Decomp LAB_007ecc0c: FUN_0081cc10("Title skip vts=%u pgcn=%u\n").
        # That's debug-stream printf only — no MSG emission to user log.
        return EvaluatorResult(
            classification="silent",
            msg_code=None,
            hidden_by_default=True,
            reason=(f"proceed-gate failed: cgcount={cgcount} "
                    f"dur_mismatch={dur_mismatch}"),
            declared_duration_s=declared,
            actual_duration_s=actual,
            collectible=coll, init=init,
            trace=(f"pre-filter=PASS init=PASS cgcount={cgcount} "
                   f"dur_mismatch={dur_mismatch} verdict=SILENT msg=None"),
        )

    # ---- Step 5: inner uVar1<=uVar5 gate --------------------------------
    # FUN_007ec6f0 line 4108: `if (uVar1 <= uVar5)` where uVar1 = pref
    # min (always 0 in public binary, dev-key-gated) and uVar5 =
    # actual seconds. Always true in public binary; we model it as
    # such. The else branch emits MSG:3025 which is unreachable here.

    # ---- Step 6: nested fake-detect block -------------------------------
    # FUN_007ec6f0 lines 4109-4148: nested gate is
    #   if (disc_state[+0x130]==[+0x138] AND plVar6[0x1c]!=plVar6[0x1d]):
    #     ... delta check ... maybe emit MSG:3026
    # disc_state empty (see above) ⇒ first AND clause is True. The
    # second clause checks plVar6 vector at +0xe0..+0xe8 non-empty
    # — same field FUN_007f3e30 read above (encoded as our
    # ``dur_mismatch``). When that's True, run the literal delta
    # check (matching lines 4116-4145):
    #
    #   uVar1 = plVar6[+0x104]   (= declared seconds)
    #   uVar5 = plVar6[+0x100]   (= actual seconds)
    #   if (actual == 0  OR  (300 < |declared-actual| AND
    #                          30 < (|declared-actual| * 100) / actual)):
    #       emit MSG:3026
    abs_delta = abs(int(declared) - int(actual))
    rel_pct_x100 = (abs_delta * 100 // max(int(actual), 1)) if actual >= 1 else 0
    is_zero_actual = int(actual) == 0
    is_significant_delta = (
        int(actual) > 0
        and abs_delta > int(FAKE_DURATION_ABS_DELTA_S)
        and rel_pct_x100 > int(FAKE_DURATION_REL_DELTA_PCT)
    )

    if dur_mismatch and (is_zero_actual or is_significant_delta):
        mkv_msg_log.emit(3026, title_no, _hms(declared), _hms(actual),
                         title=title_no, vts=vts_no,
                         cells_after_trim=cgcount,
                         abs_delta=abs_delta, rel_pct=rel_pct_x100)
        return EvaluatorResult(
            classification="fake_title",
            msg_code=3026,
            hidden_by_default=True,
            reason=("actual=0" if is_zero_actual
                    else f"|declared-actual|={abs_delta}s "
                         f"({rel_pct_x100}%) exceeds gates"),
            declared_duration_s=declared,
            actual_duration_s=actual,
            collectible=coll, init=init,
            trace=(f"pre-filter=PASS init=PASS cgcount={cgcount} "
                   f"dur_mismatch=True actual={actual:.1f} "
                   f"declared={declared:.1f} verdict=FAKE msg=3026"),
        )

    # ---- Step 7: MSG:3028 added -----------------------------------------
    # Fall-through path at FUN_007ec6f0 line 4160: emit "Title added".
    mkv_msg_log.emit(3028, title_no, cgcount, _hms(actual),
                     title=title_no, vts=vts_no)

    # ---- Step 8: multi-angle iteration (FUN_007ec6f0 lines 4533-4595) ---
    # Direct port of the multi-angle loop. Reads nr_of_angles from the
    # just-added title state (decomp: byte at plVar6+0x50, originally
    # from TT_SRPT[N].nr_of_angles). For each additional angle, re-run
    # init_validator + cellwalk + method[0x10]; emit MSG:3040 on pass,
    # MSG:3041 on fail.
    n_angles = init.angle_count
    if n_angles > 1:
        for angle in range(1, n_angles):
            # Re-call init_validator for this angle.
            angle_init = validate_title_init(title, vts, angle=angle)
            # Re-evaluate cellwalk for this angle. Multi-angle cells
            # share the same PGC; we use the same content predicate.
            # In MakeMKV's cellwalk this triggers angle-specific
            # cell selection via cells_for_angle (see cell_trim.py),
            # which yields a possibly-different cells_after_trim.
            # Since we operate on inspector dicts and don't have a
            # per-angle cell list here, treat the angle as passing
            # cellwalk iff the original (angle=0) cellwalk passed —
            # i.e., propagate cells_after_trim. This matches MakeMKV
            # behaviour for typical multi-angle titles where every
            # angle has the same cell-content length.
            angle_cgcount = cell_group_count(cells_after_trim)
            angle_method0x10_passes = angle_cgcount > 0
            if (not angle_init.ok) or (not angle_method0x10_passes):
                mkv_msg_log.emit(3041, angle + 1, title_no,
                                 title=title_no, vts=vts_no,
                                 angle=angle + 1,
                                 reason=(angle_init.reason
                                          or "method[0x10]=0"))
            else:
                mkv_msg_log.emit(3040, angle + 1, title_no,
                                 title=title_no, vts=vts_no,
                                 angle=angle + 1)

    return EvaluatorResult(
        classification="added",
        msg_code=3028,
        hidden_by_default=False,
        reason="cells survived trim, durations within tolerance",
        declared_duration_s=declared,
        actual_duration_s=actual,
        collectible=coll, init=init,
        trace=(f"pre-filter=PASS init=PASS cgcount={cgcount} "
               f"declared={declared:.1f} actual={actual:.1f} "
               f"angles={n_angles} verdict=ADDED msg=3028"),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hms(seconds: float) -> str:
    """Format seconds as "H:MM:SS" — matches MakeMKV's MSG:3026/3028 output."""
    if seconds is None or seconds < 0:
        seconds = 0
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


__all__ = [
    "CollectibleResult",
    "InitResult",
    "EvaluatorResult",
    "is_title_collectible",
    "validate_title_init",
    "evaluate_title",
    "cellwalk_keeps_any_cells",
    "MAX_VTS_AUDIO_STREAMS",
    "MAX_VTS_SUB_STREAMS",
    "MAX_TT_ANGLES",
    "FAKE_DURATION_ABS_DELTA_S",
    "FAKE_DURATION_REL_DELTA_PCT",
]
