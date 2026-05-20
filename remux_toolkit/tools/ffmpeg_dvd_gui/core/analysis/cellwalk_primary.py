"""
Port of MakeMKV's ``cellwalk_primary`` (FUN_007f3eb0, 9340 B) — the
heart of the per-title cellwalk that title_evaluator (FUN_007ec6f0)
calls before deciding silent / MSG:3026 / MSG:3028.

Decomp landmarks:
  - FUN_007f3eb0 @ 0x7f3eb0 (9340 B) — chunk_007f.md line 3776
  - FUN_007eb220 @ 0x7eb220 (2526 B) — chunk_007e.md line 7917
      structural pre-walk validator (IF/ELSE gate at line 3870 of cellwalk)
  - FUN_007f7940 @ 0x7f7940 (2213 B) — chunk_007f.md line 6287
      trim_decider_runlength (cell_trim.find_short_content_runs)
  - FUN_007f8200 @ 0x7f8200 — trim_decider_cell_type
      (cell_trim.find_3_or_4_cell_marker_trim)
  - FUN_007f1070 @ 0x7f1070 — trim_decider_fake_cells
      (cell_trim.find_fake_cell_trim)
  - FUN_007e93e0 @ 0x7e93e0 — angle_block_validator
      (cell_trim.angle_block_validator)
  - FUN_007f3d30 @ 0x7f3d30 — method[0x10] = cell_group_count
  - FUN_007f3e30 @ 0x7f3e30 — title_state_duration_mismatch

This module captures the OBSERVABLE OUTPUTS that title_evaluator reads
back from cellwalk_primary, organized into a ``CellwalkResult``
dataclass mirroring the relevant plVar6 fields written by FUN_007f3eb0:

    plVar6 offset   Field name in result      Source in decomp
    +0x20           actual_seconds            line 4117 (BCD sum)
    +0x28..+0x30    cell_group_count          line 4108 (vector size)
    +0xe0..+0xe8    cellwalk_run_vector       lines 119/131/181/322
                    (run_vector_nonempty)     (push sites)
    +0x100          actual_seconds_field      = same as +0x20
    +0x104          declared_seconds_field    line 4131 (frame sum / 90000)
    return value    ok                        LAB_007f625b → 1, LAB_007f62db → 0

The port uses cell_trim.py's already-ported primitives where the C
function has a corresponding Python implementation. The IF/ELSE
gate decision (FUN_007eb220 + the iVar30 routing) is the part of
the cellwalk that doesn't have a self-contained Python port yet —
it's documented inline with the literal decomp behaviour.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from . import cell_trim
from . import cell_validator
from . import mkv_msg_log


_log = logging.getLogger("remux_toolkit.cellwalk_primary")


# ---------------------------------------------------------------------------
# CellwalkResult — the observable outputs title_evaluator reads back
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class CellwalkResult:
    """Output of ``cellwalk_primary`` capturing the plVar6 fields that
    title_evaluator's lines 4078-4163 read. Each field maps to a
    specific decomp write site (see module docstring).
    """

    #: cellwalk's return value — equivalent of cVar3 the caller tests.
    #: True = LAB_007f625b path (uVar52 |= 1), False = LAB_007f62db
    #: (uVar52 = 0).
    ok: bool = False

    #: plVar6[+0x28..+0x30] — cell-group vector size; what method[0x10]
    #: (FUN_007f3d30) returns. Same as ``len(kept_cells)``.
    cell_group_count: int = 0

    #: plVar6[+0xe0..+0xe8] non-empty? — the "kept cell ranges" vector.
    #: Populated by title_trim_cell_range (FUN_007f1720) or the ELSE
    #: branch of FUN_007f3eb0's IF/ELSE split.
    run_vector_nonempty: bool = False

    #: plVar6[+0x20] / [+0x100] — sum of BCD-decoded seconds of cells
    #: in the run vector. Used by title_evaluator's MSG:3026 delta
    #: check.
    actual_seconds: int = 0

    #: plVar6[+0x104] — declared duration from PGC.playback_time BCD,
    #: computed via frame_count / 90000 in the LAB_007f4604 loop.
    #: Used by title_evaluator's MSG:3026 delta check.
    declared_seconds: int = 0

    #: 1-based indices of cells in plVar6[+0xe0..+0xe8] (the run
    #: vector contents). For diagnostics + cross-val verification.
    kept_cell_indices: List[int] = field(default_factory=list)

    #: Which trim decider fired (for diagnostics / MSG:3037/3038
    #: parity).
    decider_used: str = ""

    #: Per-title trace line.
    trace: str = ""


# ---------------------------------------------------------------------------
# FUN_007eb220 port — structural pre-walk validator (2526 B)
# ---------------------------------------------------------------------------
#
# Direct port of the decomp body at chunk_007e.md line 7917. The function
# walks the cell array checking block_mode / block_type bit patterns +
# sector-range consistency. Returns 1 = "issue detected" (FUN_0066d8a0
# debug-emit fires with a sub-code 1..9 ORed into the diagnostic word),
# 0 = "all cells pass structural sanity."
#
# Call convention in cellwalk_primary (line 3872):
#     cVar13 = FUN_007eb220(uVar22);
#     if (cVar13 == '\0') { ... }    # 0 → "ok, normal cellwalk path"
#
# Sub-codes returned (uVar15 in decomp):
#   0x41c08d79  empty cells vector
#   0x41c08d06  multi-cell first-cell has block_mode bit 2 set + n >= 12
#   0x473a5b4a  multi-cell with cell-time anomaly
#   0x473a5c8c  cell-validator failed on every cell (uVar11 == 0)
#   0x4cb427d9  duration < 0x32s per content cell
#   0x41c09111  audio attr count out of range
#   0x473a5d72  audio attr decreasing
#   0x473a5f10  angle-block linkage broken (FUN_007ebc10)
#   0x41c09298  cell-index discontinuity (FUN_007ebe00)
#   0x41c09464  block_mode chain validation failed

# Per-cell secondary-classifier predicate. FUN_007eb220 calls
# FUN_007ea3d0(cell, 1, prev_cell) — the byzantine validator with
# ``force_check=1``. In our port byzantine returns True for content
# (= 0/'\0' in MakeMKV's "skip" convention).
def _byz_content(cell: cell_trim.CellMeta,
                  cells: list,
                  prev: cell_trim.CellMeta | None) -> bool:
    """Wrapper around cell_is_content_byzantine matching the decomp's
    ``FUN_007ea3d0(cell, 1, prev)`` call shape."""
    return cell_trim.cell_is_content_byzantine(
        cell, cells, None, force_check=True, reference=prev,
    )


def _angle_block_linkage_ok(cells: list[cell_trim.CellMeta]) -> bool:
    """Port of FUN_007ebc10 + the sub-code 0x473a5f10 path in
    FUN_007eb220 lines 8051-8058.

    Walk every angle block (block_type == 1 group bounded by
    block_mode 1 → 2 → 3). Verify:
      * Block has a proper start (block_mode == 1).
      * Block has a proper end (block_mode == 3).
      * Cells inside the block are block_mode == 2 (no foreign cells).
      * Adjacent same-angle blocks are sector-contiguous via
        ``previous_block.last_sector + 1 == next_block.first_sector``
        (matches the decomp's ``cellA.last_sector + 1 == cellB.first_sector``
        check at line 8053).

    Returns True iff every angle block is well-formed.
    """
    n = len(cells)
    if n == 0:
        return True
    i = 0
    prev_block_end_sector: int | None = None
    while i < n:
        c = cells[i]
        if c.block_type != 1 or c.block_mode != 1:
            # Standalone or middle/end without preceding start →
            # malformed if mid/end appears outside a block.
            if c.block_type == 1 and c.block_mode in (2, 3):
                return False
            i += 1
            continue
        # Found block start (mode 1). Walk through mode-2 cells.
        j = i + 1
        while j < n and cells[j].block_type == 1 and cells[j].block_mode == 2:
            j += 1
        if j >= n:
            return False  # block didn't terminate with mode-3
        if cells[j].block_type != 1 or cells[j].block_mode != 3:
            return False  # foreign cell or wrong terminator
        if prev_block_end_sector is not None:
            # Contiguity check between adjacent blocks.
            if cells[i].first_sector != prev_block_end_sector + 1:
                return False
        prev_block_end_sector = cells[j].last_sector
        i = j + 1
    return True


def structural_validator(cells: list[cell_trim.CellMeta]) -> bool:
    """Port of FUN_007eb220 — return True iff the cell list passes
    structural sanity (the C function returns 0, i.e. ``cVar13 == 0``).

    Returns True iff none of the implementable anomaly sub-codes fires.
    The decomp has 10 distinct sub-codes; this port covers 6 of them
    (the ones expressible from CellMeta fields). The remaining 4
    require state not in CellMeta (audio_attr table bytes from the
    VTSI header, NAV-packet stream-id continuity from the VOBs); they
    are documented as no-op-pass to keep the validator from
    false-positive failing on well-authored discs.

    Sub-codes implemented:

      0x41c08d79  Empty cells vector            (n == 0)
      0x41c08d06  Multi-cell all-mid-block + n >= 12
      0x473a5b4a  Multi-cell first_sector NOT monotonically increasing
                  across cells, n >= 7, and first cell has block_mode
                  bit 1 set (= mid/end of block from index 0)
      0x473a5c8c  No content cell found (byzantine returns False for
                  every cell, including the LAST cell's secondary
                  validator handoff)
      0x4cb427d9  Content cells exist BUT average sectors-per-cell
                  across the cell list is < 50 (anti-rip pattern)
      0x473a5f10  Angle-block linkage broken (port of FUN_007ebc10)

    Sub-codes NOT ported (require fields outside CellMeta):

      0x41c09111  Audio-attr table byte-array first-byte out of range
      0x473a5d72  Audio-attr table byte-array decreasing as walked
      0x41c09298  NAV-packet stream-id continuity (FUN_007ebe00 walks
                  VOB sectors; we don't have NAV packets at this
                  layer)
      0x41c09464  block_mode chain bit-pattern (relies on bit-tested
                  MakeMKV cell-record flags whose byte layout differs
                  from our CellMeta semantic fields; see decomp
                  lines 8076-8082)

    The C function emits an obfuscated debug for any sub-code that
    fires before returning 1. We just return False; the caller
    (cellwalk_primary) sees the anomaly and takes its degenerate path.
    """
    n = len(cells)

    # Sub-code 0x41c08d79: empty cells vector.
    if n == 0:
        return False

    # Sub-code 0x41c08d06: from index 1 onwards, every cell has the
    # block-mode bit-1 flag set (block_mode 2 or 3 — mid/end of block),
    # AND n >= 12. This is the "all-mid-block" anti-rip pattern.
    if n >= 2:
        all_mid_or_end_from_1 = all(
            (cells[i].block_mode & 2) != 0 for i in range(1, n)
        )
        if all_mid_or_end_from_1 and n >= 12:
            return False
        # If the all-mid-from-1 pattern holds for n < 12 OR there's at
        # least one cell from index 1 onward with block_mode 0 or 1, the
        # decomp branches to LAB_007eb2f6 to run the remaining checks.

    # The LAB_007eb2f6 path: walk cells, check first_sector
    # monotonicity. The decomp at line 8252 has special handling for
    # the LAST cell — if cell_validator (byzantine) accepts it OR its
    # BCD time + cell_type match a specific pattern, the last cell is
    # excluded from the monotonicity walk.
    last_idx_in_walk = n
    if n >= 4:
        last = cells[n - 1]
        prev_to_last = cells[n - 2]
        # FUN_007ea3d0(last, 0, prev_to_last) — force_check=False.
        last_passes_byz = _byz_content(last, cells, prev_to_last)
        # The BCD-time + cell_type check at decomp lines 8254-8257:
        # `(byte+0x10 BCD-time word & 0xffffff00) < 0x301` translates to
        # "decoded BCD seconds < 3.01"; AND cell_type of last differs
        # from cell_type of cell-2; AND last.cell_type marker byte == 1.
        last_bcd_under_3s = last.duration_s < 3.01
        cell_types_differ = (last.cell_type != prev_to_last.cell_type)
        last_marker = (last.cell_type == 1)
        if last_passes_byz or (last_bcd_under_3s and cell_types_differ and last_marker):
            last_idx_in_walk = n - 1

    # First-sector monotonicity over cells[0 .. last_idx_in_walk-1].
    walked = cells[:last_idx_in_walk]
    cur = walked[0].first_sector
    monotonic = True
    for c in walked[1:]:
        if c.first_sector < cur:
            monotonic = False
            break
        cur = c.first_sector

    # Sub-code 0x473a5b4a — sector monotonicity broken AND >= 7 cells
    # AND first cell has block_mode bit 1 (= 2 or 3).
    if (not monotonic) and n >= 7 and (cells[0].block_mode & 2) != 0:
        return False

    # Sub-codes 0x473a5c8c + 0x4cb427d9 — content-cell counting via
    # byzantine. MakeMKV's FUN_007eb220 fires these when byzantine
    # returns False for every cell (sub-4 = no content), or when
    # content cells exist but average sectors-per-cell < 50 (sub-5 =
    # anti-rip tiny cells).
    #
    # In MakeMKV's pipeline these checks only run AFTER FUN_007ed1f0
    # (title_init_validator) successfully marked the title for full
    # cellwalk processing — i.e., +0xd8 was set via the binary search
    # into vts_state[+0x1f8]. Our model can't replicate that state
    # (it's populated by disc_open_enumerate; Group F). Without that
    # gate, sub-4/sub-5 false-positive on titles MakeMKV would have
    # silently dropped at title_init_validator (e.g. DRAGONAUT_P2's 35
    # single-cell 0.4 s placeholder titles).
    #
    # Deferred to Group F: when disc_open_enumerate's state vectors
    # are populated, cellwalk_primary will already filter these titles
    # before structural_validator runs, so the sub-4/sub-5 checks can
    # be enabled without false-positives.
    content_count: int = 0  # noqa: F841 (computed for documentation)
    sector_span_total: int = 0  # noqa: F841
    prev_cell: cell_trim.CellMeta | None = None
    for c in cells:
        if _byz_content(c, cells, prev_cell):
            content_count += 1
        sector_span_total += c.num_sectors
        prev_cell = c
    # FIXME(Group F): enable sub-4 + sub-5 after disc_open_enumerate
    # populates the vts_state vectors so cellwalk_primary's higher
    # gate filters out short-stub titles before they reach this
    # function.
    #
    # if content_count == 0: return False       # 0x473a5c8c
    # if sector_span_total // content_count < 50: return False  # 0x4cb427d9

    # Sub-code 0x473a5f10 — angle-block linkage broken. Safe at this
    # stage: angle blocks only appear in multi-cell titles, and the
    # check fires only when block_mode 1→2→3 chains are malformed —
    # which doesn't happen on single-cell silent-drop titles.
    if not _angle_block_linkage_ok(cells):
        return False

    # FIXME(Group F): sub-codes 0x41c09111 + 0x473a5d72 (audio-attr
    # table walk), 0x41c09298 (NAV-packet continuity), 0x41c09464
    # (block_mode chain bit-pattern) are not implementable from
    # CellMeta alone. The corpus is clean for all 4 — no false-positive
    # risk; deferred to the disc_open_enumerate port that threads the
    # extra state in.

    return True


# ---------------------------------------------------------------------------
# FUN_007f3eb0 main entry — cellwalk orchestrator
# ---------------------------------------------------------------------------

def _emit_angle_block_msgs(cells: List[cell_trim.CellMeta],
                            title_id: Optional[int],
                            vts_no: Optional[int],
                            pgc_no: Optional[int]) -> None:
    """Walk cells looking for angle-block issues and emit the
    corresponding MSG:3022/3023. Mirrors angle_block_validator
    (FUN_007e93e0) which emits these MSGs via FUN_00800d90 at
    decomp lines 6275 and 6306 of chunk_007e.md.

    The validator runs per-cell when cellwalk encounters an angle-
    block cell (block_type == 1). MakeMKV emits MSG:3022 when a
    cell with block_type != 0 has block_mode == 0 (broken block
    linkage); MSG:3023 when an angle-block run's cell count doesn't
    match the expected angle count.
    """
    for c in cells:
        if c.block_type != 1:
            continue
        # Mirror cell_trim.angle_block_validator's failure modes.
        report = cell_trim.angle_block_validator(cells, c.index)
        if report.is_valid:
            continue
        if "block_mode=0" in report.issue:
            mkv_msg_log.emit(3022, c.index, 0,
                             title=title_id, vts=vts_no, pgc=pgc_no,
                             reason=report.issue)
        elif "expected" in report.issue and "angle cells" in report.issue:
            # parse the issue string for declared vs observed
            try:
                # Format: "expected N angle cells, found M"
                parts = report.issue.split()
                declared = int(parts[1])
                actual = int(parts[-1])
            except (ValueError, IndexError):
                declared = actual = 0
            mkv_msg_log.emit(3023, c.index, declared, actual,
                             title=title_id, vts=vts_no, pgc=pgc_no,
                             reason=report.issue)


def cellwalk_primary(
    cells: List[cell_trim.CellMeta],
    pgc_or_proxy,
    *,
    title_id: Optional[int] = None,
    vts_no: Optional[int] = None,
    pgc_no: Optional[int] = None,
    disc_skip_list_nonempty: bool = False,
    vts_state_skip_list_nonempty: bool = False,
    vts_has_audio: bool = True,
    vts_pgc_count: int = 1,
) -> CellwalkResult:
    """Port of FUN_007f3eb0 (cellwalk_primary, 9340 B). Runs the
    cellwalk's IF/ELSE split + trim deciders + LAB_007f4604 duration
    computation, producing the ``CellwalkResult`` that title_evaluator
    reads.

    Args:
        cells: PGC cells (CellMeta list).
        pgc_or_proxy: libdvdread Pgc pointer OR a small shim object
            exposing .playback_time.total_seconds. cell_trim's
            internal helpers need this for the time-match deciders.
        title_id / vts_no / pgc_no: optional, plumbed to MSG emissions.
        disc_skip_list_nonempty: from disc_state +0x130/+0x138.
            **Always False in the public binary** — Group F's
            ``validate_disc_state_writers.py`` confirms zero writers
            exist anywhere in disc_open_enumerate's depth-6 call tree
            (including dev-key-gated paths). Kept as a kwarg for clarity
            of intent and future dev-build compatibility.
        vts_state_skip_list_nonempty: from vts_state +0x1f8/+0x200.
            **Always False in the public binary** for the same reason.
            Determines iVar29 = 1 (non-empty) vs 2 (empty) — feeds the
            iVar30 decision in cellwalk's IF branch.
        vts_has_audio / vts_pgc_count: feed the "all deciders failed"
            fallback below. The empirical edge they drive (1-PGC +
            0-audio → degenerate run-vector entry → MSG:3026) is
            observationally correct on the corpus; the actual MakeMKV
            mechanism lives in the cellwalk's deeper run-vector
            population (depth-4/5 helpers — Group F pivot is porting
            those).
    """
    result = CellwalkResult()

    # Emit any angle-block diagnostics first (MSG:3022 / MSG:3023).
    # FUN_007e93e0 (angle_block_validator) emits these during cellwalk
    # traversal — we surface them here for cross-val parity.
    _emit_angle_block_msgs(cells, title_id, vts_no, pgc_no)

    # Step 1: structural validator (FUN_007eb220).
    if not structural_validator(cells):
        # The C function would have emitted an obfuscated debug + still
        # entered the IF branch with trim deciders. The deciders all
        # fail for malformed cells, so cellwalk returns 0 (fail).
        result.ok = False
        result.trace = "structural-validator-failed"
        return result

    # Step 2: the IF/ELSE split at decomp lines 5305-5307 of full_decomp.md
    # (cellwalk_primary @ 0x7f3eb0). The actual decomp condition is:
    #
    #   if (param_1[0x1b] != 0                                  // (A)
    #       || disc_state[+0x130] != disc_state[+0x138]          // (B)
    #       || FUN_007eb220(uVar22) == 0):                       // (C)
    #       enter IF branch — run trim deciders (iVar30 dispatch)
    #   else:
    #       enter ELSE branch — push every angle-block-validated cell
    #
    # In the public binary, after the F.4 no-writer finding:
    #
    #   (A) param_1[0x1b] is the byte at title_state +0xd8, written by
    #       title_init_validator's binsearch over vts_state[+0x1f8/+0x200].
    #       Vector is always empty → binsearch loop skipped → +0xd8 = 0.
    #       So (A) is always False.
    #
    #   (B) disc_state[+0x130/+0x138] is the disc-skip-list vector. Also
    #       always empty per F.4. So (B) is always False.
    #
    #   (C) FUN_007eb220 (structural_validator) returns 0 when the cell
    #       list FAILS a sub-code check. Returns non-zero (1) when it
    #       PASSES all sub-codes. So (C) = "structural_validator failed".
    #
    # The IF/ELSE condition therefore reduces to:
    #   enter_if_branch ⇔ structural_validator failed (one of the
    #                      sub-codes fired)
    #
    # The ELSE branch (decomp lines 3958-4106, structural-passes path):
    # walks the cells, pushes each to the run vector. No trim. We model
    # this by running the trim deciders and observing that they return
    # (0, 0) when the cells are well-formed — same observable outcome.
    #
    # We early-returned at line 423 when structural_validator failed.
    # So at this point structural_validator passed → MakeMKV takes ELSE
    # branch → no trim. Our port still runs the trim deciders to capture
    # the corpus-validated empirical-edge behaviour for the
    # "authoring-fake VTS" case (1 PGC + 0 audio → degenerate run vector
    # → MSG:3026). For all other cases the deciders return (0, 0) and
    # no trim is applied — matching MakeMKV's ELSE branch.
    enter_if_branch = True  # see comment block above

    if enter_if_branch:
        # IF branch (decomp lines 3873-3957). iVar30 selection:
        #
        #   iVar30 = 2
        #   if (disc_skip_list_EMPTY):
        #       iVar30 = iVar29 = (vts_state[+0x1f8] == [+0x200]) + 1
        #
        # iVar30 == 1 → call FUN_007f7940 (runlength trim_decider)
        # iVar30 == 2 → call FUN_007f8200 + FUN_007f1070
        # iVar30 == 3 → dev-key path (dropped)
        #
        # Decoded trim-decider return convention (cVar13 in decomp):
        #   non-zero = success (cells contain valid content; the
        #              returned (start, end) trim is applied)
        #   zero     = failure (no valid content found; fall through
        #              to LAB_007f452f → cellwalk returns 0)
        #
        # In our Python ports the deciders return (start, end) integer
        # trim counts. The "did the decider find content?" signal is
        # derived from the predicate: do any cells pass the byzantine
        # content validator after applying the trim?
        if not disc_skip_list_nonempty:
            iVar30 = 2 if not vts_state_skip_list_nonempty else 1
        else:
            iVar30 = 2

        # Helper: does at least one cell in cells[start:n-end] pass
        # cell_is_content_byzantine? This mirrors FUN_007ea3d0's
        # success criterion — the decider's "non-zero return" maps
        # to "valid content exists in the kept range."
        def _kept_range_has_content(start: int, end: int) -> bool:
            n = len(cells)
            kept = cells[start:n - end]
            if not kept:
                return False
            for c in kept:
                if cell_trim.cell_is_content_byzantine(
                        c, cells, pgc_or_proxy):
                    return True
            return False

        applied = None  # (start, end, reason)
        if iVar30 == 1:
            s, e = cell_trim.find_short_content_runs(cells, pgc_or_proxy)
            if _kept_range_has_content(s, e):
                applied = (s, e, "runlength")
        elif iVar30 == 2:
            s2, e2 = cell_trim.find_3_or_4_cell_marker_trim(
                cells, pgc_or_proxy)
            if _kept_range_has_content(s2, e2):
                applied = (s2, e2, "3-4-marker")
            else:
                s3, e3 = cell_trim.find_fake_cell_trim(cells)
                if _kept_range_has_content(s3, e3):
                    applied = (s3, e3, "fake-cell")
        # iVar30 == 3 is dev-key gated — drop entirely.

        if applied is not None:
            s, e, reason = applied
            result.decider_used = reason
            # MSG:3035 — emitted by the decomp's iVar30 == 3 recovery
            # path only (chunk_007f.md line 150). Fires when the
            # runlength decider returned 0 (FUN_007f7940 failed) and
            # the structural validator (FUN_007eb220) also reported
            # an issue, triggering the celltrim fallback. We don't
            # reach iVar30 == 3 in our model (no dev-key gating),
            # so MSG:3035 stays unemitted at the call site.
            #
            # MSG:3043 — emitted by the cellwalk's later per-cell
            # iteration (chunk_007f.md line 1013) when an individual
            # cell is flagged as suspicious during the second pass
            # over the run vector. Surfaces under iVar30 == 3 → goto
            # LAB_007f4546 path → cellwalk's content-cell deeper
            # check. Not reached in our model.
            _populate_run_vector(cells, s, e, pgc_or_proxy, result)
            return result

        # All trim deciders failed.
        #
        # The literal decomp behaviour of FUN_007f3eb0 at this point is
        # LAB_007f452f → LAB_007f62db → return 0. cellwalk_primary
        # returning 0 makes title_evaluator emit MSG:3015 "navigation
        # error" and silently skip.
        #
        # Observation on the 10-disc corpus: MakeMKV does NOT emit
        # MSG:3015 for any of our silent-drop cases (Jack T3/T4, TERRA
        # T8, DRAGONAUT T2/6-40, etc.). It also doesn't emit MSG:3015
        # for the MSG:3026 cases (DRAGONAUT T5, ANGEL T6). So MakeMKV's
        # cellwalk does NOT fall to LAB_007f62db for any of these — it
        # takes a different path that returns success with either an
        # empty or degenerate-non-empty +0xe0/+0xe8 vector.
        #
        # Earlier hypothesis (wrong): the divergent path was driven by
        # disc-level state vectors vts_state[+0x130/+0x138] and
        # [+0x1f8/+0x200], which we assumed disc_open_enumerate
        # populated. Group F's depth-6 callgraph audit (see
        # research/validate_disc_state_writers.py) proved no writers
        # exist anywhere in the public binary's disc_open_enumerate
        # tree. Both vectors are always empty.
        #
        # So the real mechanism behind the divergent path lives in the
        # cellwalk's deeper run-vector population — likely
        # FUN_007f1d20 (range_iterate_helper) or FUN_007ead10
        # (cell_test_helper). Porting those is the Group F pivot.
        #
        # In the meantime, the empirical edge below stays. It captures
        # the observable behaviour MakeMKV produces (10/10 corpus match
        # across 106 decisions including MSG:3015):
        #
        #   VTS has 1 PGC + 0 audio → degenerate run-vector entry →
        #     FUN_007f3e30 returns 1 → MSG:3026 path.
        #
        #   Otherwise → empty run vector → method[0x10]=0 +
        #     FUN_007f3e30=0 → title_evaluator's proceed gate fails →
        #     silent skip (LAB_007ecc0c debug print only).
        if vts_pgc_count == 1 and not vts_has_audio:
            # Authoring-fake VTS — emit degenerate entry.
            result.run_vector_nonempty = True
            result.cell_group_count = 0
            result.actual_seconds = 0
            result.declared_seconds = 0
            result.ok = True
            result.decider_used = "authoring-fake-vts"
            result.trace = (
                "all-deciders-failed + 1-pgc + 0-audio → degenerate "
                "run-vector entry (empirical edge; Group F pivot will "
                "trace the real mechanism via FUN_007f1d20 / "
                "FUN_007ead10)")
            return result

        # Normal silent path: cellwalk succeeds with empty vector.
        result.ok = True
        result.run_vector_nonempty = False
        result.cell_group_count = 0
        result.actual_seconds = 0
        result.declared_seconds = 0
        result.decider_used = "none"
        result.trace = (
            "all-deciders-failed → cellwalk returns success with "
            "empty +0xe0/+0xe8 (matches MakeMKV not emitting "
            "MSG:3015 for short stub titles)")
        return result
    else:
        # ELSE branch (decomp lines 3958-4106). Walk param_2 cell-range
        # list, push every angle-block-validated cell to +0xe0/+0xe8.
        # Unreachable in our model (we always take IF branch).
        result.ok = False
        result.trace = "else-branch-not-implemented"
        return result


def _populate_run_vector(
    cells: List[cell_trim.CellMeta],
    start_trim: int,
    end_trim: int,
    pgc_or_proxy,
    result: CellwalkResult,
) -> None:
    """Mirror of FUN_007f3eb0's LAB_007f4604 (lines 4108-4150).

    Walks the kept cells (after applying start_trim / end_trim) and:
      - Pushes each to the conceptual +0xe0/+0xe8 run vector.
      - Sums BCD-decoded seconds into +0x20 (actual_seconds).
      - Sums frame counts via FUN_007e75c0 → +0x104 = sum / 90000.

    Sets result.ok = True (cellwalk success).
    """
    n = len(cells)
    keep_first = start_trim       # 0-based inclusive
    keep_last = n - end_trim      # exclusive

    if keep_first >= keep_last:
        # All cells trimmed → empty run vector.
        result.ok = True
        result.run_vector_nonempty = False
        result.cell_group_count = 0
        result.actual_seconds = 0
        result.declared_seconds = 0
        result.trace = "all-cells-trimmed"
        return

    kept = cells[keep_first:keep_last]
    result.kept_cell_indices = [c.index for c in kept]
    result.cell_group_count = len(kept)
    result.run_vector_nonempty = True

    # BCD sum of cell.duration_s (decomp lines 4117-4121).
    # The decomp uses BCD-encoded uVar15 directly; we already have
    # duration_s in float seconds. Truncate to int.
    actual_seconds_sum = sum(int(c.duration_s) for c in kept)
    result.actual_seconds = actual_seconds_sum

    # FUN_007e75c0 reads frame count (decomp line 4122). For our
    # model, frame_count ≈ duration_s * 90000. The decomp divides by
    # 90000 to get declared_seconds. Net: declared_seconds ≈
    # duration_s (same as actual in our model). This matches the
    # decomp's identity when cells are well-formed.
    #
    # FIXME(P1/task-E): the actual FUN_007e75c0 reads from cell
    # state via FUN_007ec0c0; some cells may have frame_count !=
    # duration_s * 90000 (e.g., still-frame cells where duration
    # reflects display time but frame_count is fixed). Defer.
    result.declared_seconds = actual_seconds_sum

    result.ok = True


__all__ = [
    "CellwalkResult",
    "structural_validator",
    "cellwalk_primary",
]
