"""
Cell-trim deciders — Python port of MakeMKV's trim algorithms (FUN_007f7940,
FUN_007f8200, FUN_007f1070) that strip nav/trailer/ident cells off the
start and end of a title.

Architecture:
1. ``cell_metadata_from_pgc`` adapts libdvdread's CellPlayback into a
   ``CellMeta`` dataclass that captures every field the decomp uses
   (cell_cmd_nr, cell_type, BCD duration, sector counts, block flags).
2. ``cell_is_content`` returns True when a cell carries playable data,
   False when it's a NOP/nav cell that can be trimmed. Uses
   ``cell_validator.cell_command_indicates_content`` for the VM-command
   side; combines with cell-level signals (duration, sector count).
3. ``find_short_content_runs`` ports FUN_007f7940's 5-second-runs
   algorithm: walk forward / backward from each end, accumulating
   duration; trim while the accumulator stays below the threshold.
4. ``find_3_or_4_cell_marker_trim`` ports FUN_007f8200's pattern: very
   short titles (3-4 cells) where the first or last cell has
   ``cell_type_marker == 1`` (typically a logo or studio ident).

Cross-validation against MakeMKV's MSG:3037/3038 emissions over the disc
corpus is a separate task (see research/mmcon_trace.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, List, Optional, Tuple

from .cell_validator import (
    cell_has_content_command,
    CELL_DURATION_TRIVIAL_S,
    CELL_DURATION_AMBIGUOUS_S,
)


# ---------------------------------------------------------------------------
# Cell-level metadata + adapter
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class CellMeta:
    """Per-cell analyzer view, derived from libdvdread CellPlayback.

    Fields mirror what MakeMKV's 40-byte cell record carries. The mapping
    from MakeMKV's struct offsets to libdvdread sources (derived from
    decomp-side validator usage + DVD-Video spec):

        MakeMKV     Type    Meaning                       libdvdread source
        +0x08       u8      cell index                    self.index
        +0x09       u8      flags (& 0x32)                self.block_mode/type
        +0x0c       bool    angle-block-fragment flag     cell_playback.interleaved
        +0x10       u32 BE  BCD playback_time             cell_playback.playback_time
        +0x14       u32     cell-group start index        self.range_start
        +0x1c       u32     cell-group end index          self.range_end
        +0x24       u16     cell_type                     cell_playback.cell_type
        +0x26       u8      type-1 marker                 cell_type == 1
        +0x27       u8 bit  runtime "marked" flag         self.is_fake

    Cell groupings (range_start, range_end): MakeMKV groups consecutive
    angle-block cells (block_mode 1→2→3) into a single record. Standalone
    cells (block_mode == 0) have range_start == range_end == index. We
    compute this in ``cell_metadata_from_pgc``.
    """
    index: int                  # 1-based cell number in PGC
    cell_cmd_nr: int
    cell_type: int
    block_type: int
    block_mode: int
    duration_s: float
    first_sector: int
    last_sector: int
    vob_id_nr: int = 0
    interleaved: bool = False    # CellPlayback.interleaved bit
    range_start: int = 0         # derived; defaults to self.index post-init
    range_end: int = 0           # derived; defaults to self.index post-init
    is_fake: bool = False

    def __post_init__(self) -> None:
        # If range not explicitly set, default to the 1:1 mapping
        # (standalone cell). Angle-block grouping is applied by the
        # adapter (cell_metadata_from_pgc) — it sets range_start/end on
        # block members to span the whole block.
        if self.range_start == 0:
            self.range_start = self.index
        if self.range_end == 0:
            self.range_end = self.index

    @property
    def num_sectors(self) -> int:
        return max(0, self.last_sector - self.first_sector + 1)

    @property
    def type1_marker(self) -> bool:
        """True when this cell's cell_type is 1 — the "first-cell-of-head"
        marker used by the 3-or-4-cell-trim decider (FUN_007f8200)."""
        return self.cell_type == 1


def _bcd_byte(b: int) -> int:
    """Decode one BCD byte (0xXY → X*10 + Y)."""
    return ((b >> 4) & 0xF) * 10 + (b & 0xF)


def _dvdtime_to_seconds(t) -> float:
    """libdvdread DvdTime is HMSF BCD with frame_u top 2 bits = framerate
    mode. Returns a floating-point seconds value (frames as a fraction).
    """
    h = _bcd_byte(t.hour)
    m = _bcd_byte(t.minute)
    s = _bcd_byte(t.second)
    # frame_u: lower 6 bits = BCD frame, upper 2 bits = rate code
    rate_code = (t.frame_u >> 6) & 0x3
    frame = _bcd_byte(t.frame_u & 0x3F)
    fps = 25.0 if rate_code == 1 else 29.97  # 1=PAL 25, 3=NTSC ~29.97
    return h * 3600 + m * 60 + s + (frame / fps if fps > 0 else 0)


def cell_metadata_from_pgc(pgc) -> List[CellMeta]:
    """Build the per-cell CellMeta list for a libdvdread PGC pointer."""
    cells: List[CellMeta] = []
    n = int(pgc.nr_of_cells)
    if not pgc.cell_playback or n == 0:
        return cells
    cp_pos = pgc.cell_position if pgc.cell_position else None
    for i in range(n):
        cp = pgc.cell_playback[i]
        vob = int(cp_pos[i].vob_id_nr) if cp_pos else 0
        cells.append(CellMeta(
            index=i + 1,
            cell_cmd_nr=int(cp.cell_cmd_nr),
            cell_type=int(cp.cell_type),
            block_type=int(cp.block_type),
            block_mode=int(cp.block_mode),
            duration_s=_dvdtime_to_seconds(cp.playback_time),
            first_sector=int(cp.first_sector),
            last_sector=int(cp.last_sector),
            vob_id_nr=vob,
            interleaved=bool(cp.interleaved),
        ))

    # Second pass: collapse angle-block runs into shared (range_start,
    # range_end). MakeMKV's 40-byte cell records group block members
    # together so that the +0x14/+0x1c range walked by the validators
    # covers the full block. Block layout:
    #   block_mode == 1  → first cell of block
    #   block_mode == 2  → in-block cell
    #   block_mode == 3  → last cell of block
    #   block_mode == 0  → standalone (its own group)
    i = 0
    while i < n:
        if cells[i].block_mode == 1:    # start of a block
            j = i
            # Find end of block (block_mode == 3, or end-of-list).
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
    return cells


# ---------------------------------------------------------------------------
# Content classifier (uses the cell-command port from cell_validator.py)
# ---------------------------------------------------------------------------

#: Minimum cell duration (seconds) to be considered "real content" when no
#: VM command is attached. Below this, a cell is treated as a candidate
#: for trimming. 0.5 s ≈ 15 NTSC frames — well below any actual program
#: cell but above ident/black-flash cells.
MIN_CONTENT_DURATION_S = 0.5


def cell_is_content(cell: CellMeta, pgc) -> bool:
    """High-level content classifier.

    True iff the cell carries playable data. False for nav/menu cells
    that the trim deciders should consider lopping off.

    Rules (in order):
      1. is_fake → False (the anti-rip detector caught it).
      2. Zero sectors → False (degenerate cell).
      3. cell_cmd_nr != 0 → ask the VM-command opcode classifier.
         True if the command does real work; False if it's a NOP.
      4. cell_cmd_nr == 0:
         - Duration >= MIN_CONTENT_DURATION_S → True.
         - Below threshold → False (ident/transition cell).
    """
    if cell.is_fake:
        return False
    if cell.num_sectors == 0:
        return False
    if cell.cell_cmd_nr != 0:
        return cell_has_content_command(pgc, cell.cell_cmd_nr)
    return cell.duration_s >= MIN_CONTENT_DURATION_S


def cell_is_content_byzantine(cell: CellMeta, all_cells: List[CellMeta], pgc,
                              *, force_check: bool = False,
                              reference: Optional[CellMeta] = None) -> bool:
    """Byzantine port of FUN_007ea3d0 (``cell_validator_primary``).

    Implements the 0x101 / 0x501 duration thresholds and the secondary-
    validator handoff. Returns True iff cell is content (the positive
    sense; MakeMKV's decomp returns 1 = "mark for skip" = our False).

    The three duration regimes:
        < 1 sec  → trivial (likely an ident frame). With ``force_check``
                   or with no ``reference``, treat as non-content.
                   With ``reference`` adjacent, hand off to
                   ``cell_validator_secondary`` to walk the cell-group
                   range — that path can rescue a short cell that's part
                   of a longer angle block.
        1-5 sec  → ambiguous. Defer to the cell_record +0x0c flag, which
                   we now know maps to CellPlayback.interleaved. When
                   set, the cell is an angle-block fragment that should
                   be evaluated as content (run cell_is_content);
                   otherwise non-content.
        ≥ 5 sec  → main content path. cell_is_content is authoritative.

    ``all_cells`` is the full per-PGC cell list (needed by the secondary
    validator's range walk).
    """
    if cell.is_fake:
        return False
    if cell.duration_s == 0.0:
        return False

    # Regime 1: < 1 sec.
    if cell.duration_s < CELL_DURATION_TRIVIAL_S and not force_check:
        if reference is not None and reference.index + 1 == cell.index:
            return cell_validator_secondary(cell, all_cells, pgc)
        return False

    # Regime 2: 1-5 sec.
    if cell.duration_s < CELL_DURATION_AMBIGUOUS_S:
        # The MakeMKV cell-record +0x0c flag is the angle-block-fragment
        # bit — libdvdread's CellPlayback.interleaved.
        if cell.interleaved:
            return cell_is_content(cell, pgc)
        return False

    # Regime 3: ≥ 5 sec.
    return cell_is_content(cell, pgc)


# ---------------------------------------------------------------------------
# cell_validator_secondary — deterministic walk of the cell-group range
# ---------------------------------------------------------------------------
#
# Port of FUN_007ea5e0. MakeMKV's binary samples 50 random cells from the
# cell-group range [+0x14, +0x1c] using a PRNG seeded from /dev/urandom.
# Two key observations:
#
# 1. The PRNG is a *performance* optimization, not a correctness
#    requirement. The function exits early when ANY tested cell passes
#    cell_test_helper — sampling vs. exhaustive walk differ only in
#    which cell triggers the early exit (and rarely, in whether some
#    minority of passing cells gets missed in sampling).
#
# 2. The working set is the cell-group range, not "all cells in PGC."
#    For 1:1 cells (no angle blocks), the range is a single cell. For
#    angle blocks, the range spans the block_mode 1→2→3 sequence.
#
# Our port: deterministic walk of [range_start, range_end]; return True
# if any cell in the range passes the basic content check. No PRNG.


def cell_validator_secondary(cell: CellMeta, all_cells: List[CellMeta],
                             pgc) -> bool:
    """Deterministic port of FUN_007ea5e0 — secondary content-check
    fallback for cell groups.

    Walks every cell in [cell.range_start, cell.range_end] and returns
    True if any of them passes the basic content check. The MakeMKV
    binary does this with 50 PRNG samples + an exhaustive walk after;
    we just do the exhaustive walk, which is *strictly more accurate*
    on a typical commercial DVD where range sizes are 1-10 cells.

    ``all_cells`` is the full per-PGC cell list (from
    ``cell_metadata_from_pgc``) — the function indexes into it to find
    members of the range.
    """
    if not all_cells:
        return False
    # Look up cells in [range_start, range_end] (1-based).
    for c in all_cells:
        if not (cell.range_start <= c.index <= cell.range_end):
            continue
        if cell_is_content(c, pgc):
            return True
    return False


# ---------------------------------------------------------------------------
# fake_cell_detector (FUN_007f1b40) — anti-rip dummy detection
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class FakeCellAnalysis:
    """Result of ``fake_cell_detector``."""
    #: The 1-based (start, end) cell index range that constitutes the
    #: bulk of the PGC duration. None when no dominant run was found.
    dominant_range: Optional[Tuple[int, int]] = None
    #: 1-based cell indices identified as anti-rip dummies (i.e. cells
    #: outside ``dominant_range`` when one dominates).
    fake_indices: FrozenSet[int] = field(default_factory=frozenset)
    #: Fraction of total PGC duration covered by ``dominant_range``
    #: (0..100). High = strong "fakes outside" signal.
    confidence_pct: float = 0.0


#: From FUN_007f1b40: if the cells OUTSIDE the dominant run are less
#: than 26% of total PGC duration, the dominant run is treated as the
#: "real" content and outside cells are flagged fake. (Decomp uses
#: ``percentage < 0x1a`` literal.)
FAKE_CELL_PCT_THRESHOLD = 26.0

#: Minimum "outside" fraction for the dominant-VOB anti-rip pattern to
#: count. Below this floor, the outlier is just legit DVD authoring
#: noise (e.g. TERRA_NOVA T2's 0.5s trailer cell at 0.01% of a 5180s
#: PGC, T7's 0.5s outro at 1.94%). MakeMKV's cellwalk_primary appears
#: to gate decider-3 on the PGC having a "meaningfully distributed"
#: multi-VOB structure; 5% empirically matches MakeMKV's behaviour on
#: the corpus. The companion check is the outlier-cell-count >= 3
#: requirement (anti-rip discs use many fake cells, legit DVDs use
#: 1-2 trailer stubs).
FAKE_CELL_PCT_FLOOR = 5.0


def fake_cell_detector(cells: List[CellMeta]) -> FakeCellAnalysis:
    """Port of FUN_007f1b40 — detect anti-rip dummy cells.

    Algorithm (matches MakeMKV's grouping by VOB-ID transitions):
        1. Walk the cells; group consecutive cells with the same
           ``vob_id_nr`` together.
        2. Find the GROUP with the maximum total duration ("dominant").
        3. Compute (total - dominant_group_duration) / total = outside
           fraction.
        4. If outside fraction < 26%, flag every cell whose ``vob_id_nr``
           differs from the dominant group as fake.

    When all cells share a single VOB ID (the common case on commercial
    DVDs), there's only one group and no fakes are flagged. Anti-rip
    discs that interleave cells from a decoy VOB get flagged.

    Returns ``FakeCellAnalysis``.
    """
    if not cells:
        return FakeCellAnalysis()
    total = sum(c.duration_s for c in cells)
    if total == 0.0:
        return FakeCellAnalysis()

    # Bucket cell durations by vob_id_nr.
    by_vob: dict[int, float] = {}
    for c in cells:
        by_vob[c.vob_id_nr] = by_vob.get(c.vob_id_nr, 0.0) + c.duration_s
    if len(by_vob) <= 1:
        # All cells share one VOB ID → no transitions → no fakes.
        return FakeCellAnalysis(confidence_pct=100.0)

    # Pick the VOB-ID group with max duration.
    dominant_vob, dominant_dur = max(by_vob.items(), key=lambda kv: kv[1])
    confidence = (dominant_dur / total) * 100.0
    outside_pct = ((total - dominant_dur) / total) * 100.0
    if outside_pct >= FAKE_CELL_PCT_THRESHOLD:
        return FakeCellAnalysis(confidence_pct=confidence)
    # Noise floor: a single short outlier cell at sub-1% of total duration
    # is almost always a legit fade-out (TERRA_NOVA T2: 0.5s fade after a
    # 5180s 3-episode PGC). Anti-rip patterns insert MANY dummy cells —
    # if the outlier vob has ≥3 cells the rule kicks in regardless of
    # duration percentage.
    outlier_cell_count = sum(1 for c in cells if c.vob_id_nr != dominant_vob)
    if outside_pct < FAKE_CELL_PCT_FLOOR and outlier_cell_count < 3:
        return FakeCellAnalysis(confidence_pct=confidence)

    # Compute the dominant cell range (first..last cell that's in the
    # dominant VOB ID).
    dom_first = min(c.index for c in cells if c.vob_id_nr == dominant_vob)
    dom_last = max(c.index for c in cells if c.vob_id_nr == dominant_vob)
    fake = frozenset(
        c.index for c in cells if c.vob_id_nr != dominant_vob
    )
    return FakeCellAnalysis(
        dominant_range=(dom_first, dom_last),
        fake_indices=fake,
        confidence_pct=confidence,
    )


# ---------------------------------------------------------------------------
# angle_block_validator (FUN_007e93e0) — multi-angle structural check
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class AngleBlockReport:
    """Output of ``angle_block_validator``."""
    is_valid: bool = True
    issue: str = ""


def angle_block_validator(cells: List[CellMeta], cell_index: int,
                          angle_param: int = 0) -> AngleBlockReport:
    """Port of FUN_007e93e0 — validate that multi-angle blocks are
    well-formed.

    A "block" of multi-angle cells is a contiguous run of cells where:
        - block_mode = 1 (first), 2 (in), 3 (last)
        - block_type = 1 (angle block)

    The validator checks that:
        - The first cell of a block has block_mode == 1 and the
          high bits of its flag byte match the angle-cell pattern.
        - The block contains the expected number of angle cells
          (encoded in ``angle_param`` low byte).
        - Each in-block cell has block_mode > 0 (no premature
          standalone cells).

    Returns a report with ``is_valid`` and a diagnostic message.

    On commercial DVDs with single-angle content, blocks don't exist
    and this always returns valid. Multi-angle discs (rare on the
    corpus) get the full validation.
    """
    if cell_index < 1 or cell_index > len(cells):
        return AngleBlockReport(is_valid=False, issue="cell_index out of range")
    cell = cells[cell_index - 1]
    if cell.block_mode == 0 and cell.block_type == 0:
        # Standalone cell — no block to validate.
        return AngleBlockReport()
    if cell.block_type != 1:
        # Not an angle block — nothing to validate.
        return AngleBlockReport()
    # We're at a multi-angle cell. Walk forward to find the block.
    expected_angles = angle_param & 0xFF
    seen = 0
    i = cell_index - 1
    while i < len(cells) and cells[i].block_type == 1:
        if cells[i].block_mode == 0:
            return AngleBlockReport(
                is_valid=False,
                issue=f"cell {cells[i].index} has block_type=1 but block_mode=0",
            )
        seen += 1
        if cells[i].block_mode == 3:   # last
            break
        i += 1
    if expected_angles and seen != expected_angles:
        return AngleBlockReport(
            is_valid=False,
            issue=f"expected {expected_angles} angle cells, found {seen}",
        )
    return AngleBlockReport()


# ---------------------------------------------------------------------------
# trim decider 3 (FUN_007f1070) — fake-cell-aware trim
# ---------------------------------------------------------------------------

#: A "dummy" cell range is small enough to be just MPEG-PS headers + a
#: handful of VOBU markers — no real video. 16 sectors = 32 KB,
#: corresponding to ~0.5s of MPEG-2 at low bitrates. DRAGONAUT_P2 T31
#: anti-rip cells are 7 sectors (sectors 0..6).
_DUMMY_RANGE_MAX_SECTORS = 16


#: Max duration in seconds that a "dummy" cell holds. Anti-rip dummy cells
#: are typically 0.4-0.5s — one or two GOP boundaries' worth of MPEG-2
#: header + padding. Real content cells run multiple seconds at minimum.
_DUMMY_MAX_DURATION_S = 0.6

def find_dummy_sector_cells(cells: List[CellMeta]) -> FrozenSet[int]:
    """Identify cells whose ``first_sector``/``last_sector`` ranges and
    duration are clear anti-rip dummies:

        1. Cells with ``first_sector == 0`` AND a small range
           (≤ ``_DUMMY_RANGE_MAX_SECTORS`` = 16 sectors = ≤32 KB).
           A legit content cell never starts at sector 0 of a
           VTS title-VOB.
        2. Cells whose (first_sector, last_sector) range is **small** AND
           shared by ≥3 OTHER cells in the same PGC. Legit DVD authoring
           assigns unique ranges per cell, but anti-rip discs replay the
           same tiny fragment N times.
        3. Cells with **short duration** (≤ ``_DUMMY_MAX_DURATION_S``)
           AND **small sector extent** (≤ ``_DUMMY_RANGE_MAX_SECTORS``).
           Catches cells 1, 2 of DRAGONAUT_P2 T31 (each 0.4s × 16
           sectors, but starting at sectors 7 and 23 — past sector-0
           but still authoring-noise).

    Returns the 1-based set of cell indices flagged as dummies.
    """
    if not cells:
        return frozenset()
    out: set = set()
    # Pattern 1: zero-sector range with a small extent.
    for c in cells:
        size = c.last_sector - c.first_sector
        if c.first_sector == 0 and size <= _DUMMY_RANGE_MAX_SECTORS:
            out.add(c.index)
    # Pattern 2: ≥3 cells sharing the exact same SMALL sector range.
    by_range: dict[Tuple[int, int], List[int]] = {}
    for c in cells:
        size = c.last_sector - c.first_sector
        if size > _DUMMY_RANGE_MAX_SECTORS:
            continue
        sig = (c.first_sector, c.last_sector)
        by_range.setdefault(sig, []).append(c.index)
    for sig, indices in by_range.items():
        if len(indices) >= 3:
            out.update(indices)
    # Pattern 3: short-duration small-range cells, but only when there
    # are ≥3 such cells in the PGC (= anti-rip pattern, not a single
    # legit trailer / fade cell). ANGEL_S1D1 T1's last cell is 0.501s
    # × 5 sectors and is a real fade — keeping it. DRAGONAUT_P2 T31 has
    # 92 such cells — clearly anti-rip.
    pattern3_candidates = [
        c.index for c in cells
        if (c.last_sector - c.first_sector) <= _DUMMY_RANGE_MAX_SECTORS
        and c.duration_s <= _DUMMY_MAX_DURATION_S
    ]
    if len(pattern3_candidates) >= 3:
        out.update(pattern3_candidates)
    # Pattern 4 (DRAGONAUT_P2 T32 cell 7 discontiguous trailer) was
    # removed in the Group B cleanup — it was an empirical extension
    # with no decomp anchor. See AUDIT.md §1.8 D9 + R5. The genuine
    # decomp source for this trim verdict is expected to surface in
    # Group F when the disc_open_enumerate port populates the
    # vts_state[+0x130/+0x138] / [+0x1f8/+0x200] vectors that drive
    # cellwalk_primary's deeper paths.
    return frozenset(out)


def find_fake_cell_trim(cells: List[CellMeta]) -> Tuple[int, int]:
    """Port of FUN_007f1070 — trim cells flagged as fake.

    Combines two detection signals:

        1. VOB-ID grouping (``fake_cell_detector``): cells outside the
           dominant-by-duration VOB ID group. Catches anti-rip discs that
           interleave decoy VOBs.
        2. Sector-range signature (``find_dummy_sector_cells``): cells
           whose physical sector range is invalid (sector 0) or
           duplicated. Catches the DRAGONAUT_P2 T31 pattern (92 fake
           cells all pointing at sectors 0..6).

    Trims only contiguous fake cells from each end; never trims a
    cell in the middle of the dominant run.

    Returns (start_trim_cells, end_trim_cells).
    """
    sector_fakes = find_dummy_sector_cells(cells)
    analysis = fake_cell_detector(cells)
    fake_set = set(analysis.fake_indices) | set(sector_fakes)
    if not fake_set:
        return 0, 0

    # Compute a "real-content" range: the contiguous run between the
    # first and last non-fake cell.
    real_cells = [c for c in cells if c.index not in fake_set]
    if not real_cells:
        # Whole title is fake — don't trim everything; let the analyzer
        # mark this as a fake title higher up.
        return 0, 0
    dom_start = real_cells[0].index
    dom_end = real_cells[-1].index

    start_trim = 0
    end_trim = 0
    for c in cells:
        if c.index < dom_start and c.index in fake_set:
            start_trim += 1
        else:
            break
    for c in reversed(cells):
        if c.index > dom_end and c.index in fake_set:
            end_trim += 1
        else:
            break
    return start_trim, end_trim


# ---------------------------------------------------------------------------
# Trim deciders
# ---------------------------------------------------------------------------

#: Threshold from FUN_007f7940 — runs of non-content cells whose combined
#: duration is below this are eligible for trim.
TRIM_5SEC_THRESHOLD_S = 5.0


@dataclass(slots=True)
class TrimDecision:
    """Result of running the trim deciders on a title.

    ``start_trim`` / ``end_trim`` are the number of cells (counted from
    the start / end of the PGC, respectively) to skip when ripping. Both
    are inclusive of consecutive non-content cells whose combined
    duration stays below the deciders' thresholds.

    ``reason`` is a short string explaining which decider fired (for
    debugging + matching against MakeMKV's MSG:3037 / MSG:3038
    emissions).
    """
    start_trim: int = 0
    end_trim: int = 0
    reason_start: str = ""
    reason_end: str = ""

    @property
    def any_trim(self) -> bool:
        return self.start_trim > 0 or self.end_trim > 0


def _is_content_for_trim(cell: CellMeta, all_cells: List[CellMeta], pgc) -> bool:
    """Match MakeMKV's content classifier used during trim: the
    "byzantine" cell_validator_primary port (not the simpler lenient
    classifier). MakeMKV's FUN_007f7940 internally invokes
    FUN_007ea3d0 (cell_validator_primary). Using the lenient classifier
    here makes us miss short trailer cells (DRAGONAUT_P2 T31's cell 100
    at 0.5005s would otherwise pass cell_is_content's 0.5 threshold).
    """
    return cell_is_content_byzantine(cell, all_cells, pgc)


def find_short_content_runs(cells: List[CellMeta], pgc,
                            *, threshold_s: float = TRIM_5SEC_THRESHOLD_S,
                            ) -> Tuple[int, int]:
    """Port of FUN_007f7940's 5-sec-run trim:

    Walking forward from the start: extend a trim window over cells that
    are NOT content; stop as soon as we either (a) hit a content cell or
    (b) accumulate >= threshold_s of duration.

    Mirror the same walk from the end.

    Returns (start_trim_cells, end_trim_cells).

    Uses ``cell_is_content`` (the lenient classifier). A prior pass
    tried the byzantine variant per AUDIT.md U6 but the trivial-
    regime path (< 1 s, no reference) trims legitimate end-cell
    fades across the corpus (ANGEL T1 cell 23 at 0.501 s, FOREVER_KNIGHT
    last-cell fades, etc.) which MakeMKV keeps. FUN_007f7940's real
    algorithm walks **ranges** of cells from cell-record +0xc and
    keeps a range iff (some byzantine-content cell seen) AND (range
    duration >= 5 s) — a range-aware filter that requires the
    disc-state vectors populated by disc_open_enumerate (Group F)
    to drive faithfully. U6 reclassified as Group F's responsibility.
    """
    n = len(cells)
    if n == 0:
        return 0, 0

    # Forward walk: count non-content cells until we hit a real one or
    # accumulate threshold_s of duration.
    start_trim = 0
    acc = 0.0
    for i, cell in enumerate(cells):
        if cell_is_content(cell, pgc):
            break
        acc += cell.duration_s
        if acc >= threshold_s:
            break
        start_trim = i + 1

    # Backward walk: same logic from the tail.
    end_trim = 0
    acc = 0.0
    for i in range(n - 1, -1, -1):
        cell = cells[i]
        if cell_is_content(cell, pgc):
            break
        acc += cell.duration_s
        if acc >= threshold_s:
            break
        end_trim = n - i

    # Never trim every cell — keep at least one. If forward and backward
    # walks each swallow the whole title (all-non-content), cap so
    # start_trim + end_trim ≤ n-1. Bias the kept cell toward the front
    # by capping the end-trim first.
    if start_trim + end_trim >= n:
        budget = n - 1
        start_trim = min(start_trim, budget)
        end_trim = max(0, budget - start_trim)

    return start_trim, end_trim


def find_3_or_4_cell_marker_trim(cells: List[CellMeta], pgc) -> Tuple[int, int]:
    """Port of FUN_007f8200's pattern: very short titles (3 or 4 cells)
    whose first or last cell has ``cell_type`` indicating a marker
    (studio logo, ratings card). These get trimmed off so the rip
    starts on the actual feature.

    MakeMKV checks ``cell_type_marker == 0x01`` at cell_record + 0x26;
    our libdvdread ``cell_type`` field carries the same 5-bit value
    (block flags low 5 bits).

    Returns (start_trim_cells, end_trim_cells).
    """
    n = len(cells)
    if n not in (3, 4):
        return 0, 0
    # The decomp checks that both first and last cells are content
    # (cell_validator_primary passes) before considering a trim — that
    # avoids trimming when the whole title is junk.
    first = cells[0]
    last = cells[-1]
    if not cell_is_content(first, pgc) or not cell_is_content(last, pgc):
        return 0, 0
    # cell_type == 1 indicates the "type-1 marker" cell.
    start_trim = 1 if first.cell_type == 1 and last.cell_type != 1 else 0
    end_trim   = 1 if last.cell_type == 1 and first.cell_type != 1 else 0
    return start_trim, end_trim


# Group B removal note: find_compilation_pgc_trim and
# find_pgc_time_match_trim were calibrated empirically against
# DRAGONAUT_P2 T31 + ANGEL_S1D1 T1. Neither had a decomp anchor in
# FUN_007f1070 or anywhere else in the cellwalk subtree. Removing
# them per AUDIT.md R5 / §1.8 D7 — the actual decomp source for the
# compilation-PGC trim verdict is expected to surface inside
# disc_open_enumerate (Group F) once the vts_state[+0x130/+0x138] /
# [+0x1f8/+0x200] vectors drive cellwalk_primary's deeper paths.


def decide_trim(cells: List[CellMeta], pgc, *,
                title_id: Optional[int] = None,
                vts_no: Optional[int] = None,
                pgc_no: Optional[int] = None) -> TrimDecision:
    """Top-level trim selector. Runs the three decomp-anchored deciders
    and combines their results.

    Priority (most-specific wins):
        1. 5-sec content run (FUN_007f7940 — most general)
        2. 3-or-4-cell marker (FUN_007f8200 — specific to short titles)
        3. Fake-cell trim (FUN_007f1070 + FUN_007f1b40 — only fires on
           anti-rip discs)

    We take the MAX trim count from each end (i.e. the most aggressive
    decider wins), reasoning that all three deciders are conservative
    about what to trim and any one of them firing is a strong signal.

    Group B note: the prior empirical extensions (compilation-PGC
    trim, PGC-time-match trim) were removed — they had no decomp
    anchor. The real decomp source for those verdicts is expected to
    surface in Group F via the disc_open_enumerate state vectors.
    """
    s1, e1 = find_short_content_runs(cells, pgc)
    s2, e2 = find_3_or_4_cell_marker_trim(cells, pgc)
    s3, e3 = find_fake_cell_trim(cells)
    out = TrimDecision()
    out.start_trim = max(s1, s2, s3)
    out.end_trim   = max(e1, e2, e3)
    # Reason picking: prefer the most-specific decider that fired.
    if out.start_trim == s3 and s3 > 0:
        out.reason_start = "fake-cell-outside-dominant-run"
    elif out.start_trim == s2 and s2 > 0:
        out.reason_start = "3-4-cell-marker"
    elif out.start_trim > 0:
        out.reason_start = "short-content-run<5s"
    if out.end_trim == e3 and e3 > 0:
        out.reason_end = "fake-cell-outside-dominant-run"
    elif out.end_trim == e2 and e2 > 0:
        out.reason_end = "3-4-cell-marker"
    elif out.end_trim > 0:
        out.reason_end = "short-content-run<5s"

    # Emit MakeMKV-equivalent MSG:3037/3038 for cross-validation.
    if title_id is not None and (out.start_trim > 0 or out.end_trim > 0):
        from . import mkv_msg_log
        n = len(cells)
        if out.start_trim > 0:
            mkv_msg_log.emit(3037, out.start_trim,
                             title=title_id, vts=vts_no, pgc=pgc_no,
                             reason=out.reason_start)
        if out.end_trim > 0:
            mkv_msg_log.emit(3038, n - out.end_trim + 1, n,
                             title=title_id, vts=vts_no, pgc=pgc_no,
                             reason=out.reason_end)
    return out


# ---------------------------------------------------------------------------
# Multi-angle handling
# ---------------------------------------------------------------------------
#
# Angle blocks: a sequence of cells with block_type == 1 representing the
# same content shot from different angles. The cells are stored
# sequentially in PGC order — block_mode 1 → 2 → 2 → ... → 3 — and play
# back interleaved on disc. At playback time the player picks ONE cell
# per block (the one matching the current angle setting); other cells in
# the block are skipped.
#
# A title's "angle count" is the length of its first angle block (every
# angle block in a multi-angle title has the same length per DVD spec).

def count_angles(cells: List[CellMeta]) -> int:
    """Return the number of angles in a PGC. Returns 1 for single-angle
    titles (the common case). Examines the FIRST angle block; the DVD
    spec requires all angle blocks in a PGC to have the same length."""
    i = 0
    while i < len(cells):
        if cells[i].block_type == 1 and cells[i].block_mode == 1:
            # Found the start of an angle block — count to block_mode == 3
            j = i
            while j < len(cells) and cells[j].block_mode != 3:
                j += 1
            if j < len(cells):
                return j - i + 1
            return j - i  # malformed (no terminator)
        i += 1
    return 1   # no angle block → single angle


def cells_for_angle(cells: List[CellMeta], angle: int = 1) -> set[int]:
    """Return the set of 1-based cell indices that should be played for
    the given angle (1-indexed). For non-multi-angle PGCs, returns every
    cell index (the angle argument is a no-op).

    For a multi-angle PGC: include all non-angle-block cells, plus the
    Nth cell of each angle block (where N = angle, 1-indexed). When the
    requested angle exceeds the block length, the last angle is used
    (matches MakeMKV's clamp-to-last behavior).
    """
    included: set[int] = set()
    n = len(cells)
    i = 0
    while i < n:
        c = cells[i]
        if c.block_type != 1 or c.block_mode == 0:
            # Standalone cell — always include
            included.add(c.index)
            i += 1
            continue
        # Walk to end of angle block (block_mode == 3 terminator).
        block_start = i
        block_end = i
        while block_end < n and cells[block_end].block_mode != 3:
            block_end += 1
        if block_end >= n:
            # Malformed block (no terminator); include nothing extra
            i = block_end
            continue
        block_len = block_end - block_start + 1
        # 1-indexed angle clamped to [1, block_len].
        pick = max(0, min(angle - 1, block_len - 1))
        included.add(cells[block_start + pick].index)
        i = block_end + 1
    return included


__all__ = [
    "CellMeta",
    "cell_metadata_from_pgc",
    "cell_is_content",
    "cell_is_content_byzantine",
    "cell_validator_secondary",
    "MIN_CONTENT_DURATION_S",
    "TRIM_5SEC_THRESHOLD_S",
    "FAKE_CELL_PCT_THRESHOLD",
    "TrimDecision",
    "FakeCellAnalysis",
    "AngleBlockReport",
    "fake_cell_detector",
    "find_dummy_sector_cells",
    "angle_block_validator",
    "find_fake_cell_trim",
    "find_short_content_runs",
    "find_3_or_4_cell_marker_trim",
    "decide_trim",
    "count_angles",
    "cells_for_angle",
]
