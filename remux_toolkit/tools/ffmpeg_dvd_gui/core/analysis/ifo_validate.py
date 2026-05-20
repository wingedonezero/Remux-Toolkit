"""
IFO / BUP validation + source probing.

``libdvdread.ifoOpen()`` already falls back from VTS_xx_0.IFO to VTS_xx_0.BUP
if the main file fails to open — that's the easy case (matches FFmpeg's
``dvdvideodec.c``). What it can't tell us:

    * Whether the BUP was actually used (the API returns one handle).
    * Whether the main IFO opened successfully but holds **corrupt** values
      that subsequent code will silently misinterpret. This is the failure
      mode that MakeMKV / DVDFab handle but plain libdvdread + FFmpeg don't.

This module gives a higher layer two extra signals:

    * ``probe_ifo_pair(dvd, title)``  — opens the IFO and BUP as raw files
      via ``DVDOpenFile()``, compares their first sector(s). If only one is
      readable, that's the implicit fallback. If both are readable but
      *differ*, libdvdread will have used the main IFO; the BUP can be
      consulted as a tie-breaker.
    * ``validate_ifo_handle(ifo, is_vmg)``  — sanity-checks a loaded
      ``ifo_handle_t`` for impossible values (zero PGCs, out-of-range
      counts, absurd durations). Returns the list of issues, empty on a
      well-formed IFO.

These signals are surfaced in the inspector JSON so cross-validation can
correlate them with MakeMKV's MSG:3002 / 3042 emissions, and the rip
pipeline can refuse to operate on titles with severe issues.

The complement: ``css_state(dvd_path)`` returns whether the disc is
CSS-scrambled (via libdvdcss's ``dvdcss_is_scrambled``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ...bindings import libdvdread as dr
from ...bindings import libdvdcss as dc
from . import ifo_parser as ifop


_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class IfoSourceProbe:
    """Raw IFO / BUP file probe for one title (VMG = 0, VTS = 1..N)."""
    title: int
    main_present: bool
    bup_present: bool
    main_size: int = -1
    bup_size: int = -1
    #: True when both files are present AND their first compared sector(s)
    #: match byte-for-byte. False when both present but differ.
    #: None when one (or both) is missing.
    content_matches: Optional[bool] = None
    #: Which file libdvdread will end up using on ``ifoOpen``.
    #: ``"main"`` when the main is readable; ``"bup"`` only when main is
    #: missing; ``"missing"`` when neither is readable.
    effective_source: str = "missing"


@dataclass(slots=True)
class IfoIssue:
    """A single problem found on a loaded IFO."""
    severity: str           # ``"warn"`` or ``"error"``
    category: str           # ``"missing_main"`` | ``"missing_bup"`` |
                            # ``"diverged"`` | ``"sanity"``
    message: str


@dataclass(slots=True)
class IfoReport:
    """Per-title summary combining probe + validation."""
    title: int              # 0 for VMG, 1..N for VTS
    is_vmg: bool
    probe: IfoSourceProbe
    issues: List[IfoIssue] = field(default_factory=list)
    #: Audio + sub counts as parsed from the main IFO header bytes (NOT
    #: libdvdread's view — useful when libdvdread agrees with main but
    #: BUP says something different).
    main_audio_count: Optional[int] = None
    main_subp_count: Optional[int] = None
    #: Same counts as parsed from the BUP header bytes. Populated whenever
    #: the BUP file is present; None otherwise.
    bup_audio_count: Optional[int] = None
    bup_subp_count: Optional[int] = None
    #: True when main and BUP report different audio/sub counts. A strong
    #: signal that one source is corrupt — the rip pipeline should consult
    #: ``preferred_attr_source`` for which to trust.
    counts_diverge: bool = False
    #: ``"main"`` when libdvdread's view (== main IFO) looks sane;
    #: ``"bup"`` when main looks corrupt (e.g. 0 streams) but BUP has
    #: plausible values; ``None`` when there's nothing to choose.
    preferred_attr_source: Optional[str] = None

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)


# ---------------------------------------------------------------------------
# Source probe
# ---------------------------------------------------------------------------

def probe_ifo_pair(dvd, title: int, *, compare_blocks: int = 1) -> IfoSourceProbe:
    """Open the IFO and BUP as raw files, return availability + content match.

    ``compare_blocks`` controls how many 2048-byte sectors are compared
    when both files are present. The IFO header sits in the first sector;
    a full byte-for-byte file comparison would catch more corruption but
    is expensive and rarely needed (corruption usually hits the header
    first or makes the file unreadable).
    """
    main_size = dr.probe_ifo_size(dvd, title, backup=False)
    bup_size = dr.probe_ifo_size(dvd, title, backup=True)

    main_present = main_size > 0
    bup_present = bup_size > 0

    if main_present:
        effective = "main"
    elif bup_present:
        effective = "bup"
    else:
        effective = "missing"

    content_matches: Optional[bool] = None
    if main_present and bup_present:
        try:
            main_head = dr.probe_ifo_blocks(dvd, title, backup=False,
                                            n_blocks=compare_blocks)
            bup_head = dr.probe_ifo_blocks(dvd, title, backup=True,
                                           n_blocks=compare_blocks)
        except dr.DvdReadError as e:
            _logger.warning(
                "probe_ifo_pair(title=%d): mid-read error: %s", title, e)
            main_head = None
            bup_head = None
        if main_head is not None and bup_head is not None:
            n = min(len(main_head), len(bup_head))
            content_matches = (n > 0 and main_head[:n] == bup_head[:n])

    return IfoSourceProbe(
        title=title,
        main_present=main_present,
        bup_present=bup_present,
        main_size=main_size,
        bup_size=bup_size,
        content_matches=content_matches,
        effective_source=effective,
    )


# ---------------------------------------------------------------------------
# Sanity validation
# ---------------------------------------------------------------------------

#: Max plausible value for various counters. Real DVDs sit far below these.
_MAX_TITLE_SETS = 99            # spec maximum
_MAX_PGCS = 256
_MAX_CELLS_PER_PGC = 1024
_MAX_AUDIO_STREAMS = 8          # spec maximum
_MAX_SUBP_STREAMS = 32          # spec maximum
_MAX_DURATION_SECONDS = 100 * 3600   # 100h — anything beyond is corruption


def validate_ifo_handle(ifo, *, is_vmg: bool) -> List[IfoIssue]:
    """Walk a loaded ``ifo_handle_t`` and flag impossible values.

    Empty return list means the IFO passes basic sanity.

    Caller still owns / closes ``ifo``. We do not mutate it.
    """
    issues: List[IfoIssue] = []
    h = ifo.contents

    if is_vmg:
        if not h.vmgi_mat:
            issues.append(IfoIssue("error", "sanity",
                "VMG has no vmgi_mat"))
            return issues
        vmgi = h.vmgi_mat.contents
        num_vts = int(vmgi.vmg_nr_of_title_sets)
        if num_vts < 1 or num_vts > _MAX_TITLE_SETS:
            issues.append(IfoIssue("error", "sanity",
                f"VMG vmg_nr_of_title_sets={num_vts} (expected 1..{_MAX_TITLE_SETS})"))
        if not h.tt_srpt:
            issues.append(IfoIssue("error", "sanity",
                "VMG missing tt_srpt"))
        else:
            tt = h.tt_srpt.contents
            n_titles = int(tt.nr_of_srpts)
            if n_titles < 1:
                issues.append(IfoIssue("error", "sanity",
                    "VMG declares zero titles"))
            elif n_titles > 99 * _MAX_PGCS:
                issues.append(IfoIssue("error", "sanity",
                    f"VMG title count={n_titles} implausible"))
        return issues

    # VTS path
    if not h.vtsi_mat:
        issues.append(IfoIssue("error", "sanity",
            "VTS has no vtsi_mat"))
        return issues
    m = h.vtsi_mat.contents

    n_audio = int(m.nr_of_vts_audio_streams)
    if n_audio > _MAX_AUDIO_STREAMS:
        issues.append(IfoIssue("warn", "sanity",
            f"VTS audio stream count={n_audio} > spec max {_MAX_AUDIO_STREAMS}"))
    n_subp = int(m.nr_of_vts_subp_streams)
    if n_subp > _MAX_SUBP_STREAMS:
        issues.append(IfoIssue("warn", "sanity",
            f"VTS subp stream count={n_subp} > spec max {_MAX_SUBP_STREAMS}"))

    # video attrs basic checks (fields are bitfields, narrow ranges by C type)
    va = m.vts_video_attr
    if int(va.mpeg_version) not in (0, 1):
        issues.append(IfoIssue("warn", "sanity",
            f"VTS video mpeg_version={int(va.mpeg_version)} unexpected"))
    if int(va.video_format) not in (0, 1):
        issues.append(IfoIssue("warn", "sanity",
            f"VTS video video_format={int(va.video_format)} unexpected"))

    if not h.vts_pgcit:
        issues.append(IfoIssue("error", "sanity",
            "VTS has no PGCI table"))
        return issues

    pgcit = h.vts_pgcit.contents
    n_pgcs = int(pgcit.nr_of_pgci_srp)
    if n_pgcs < 1:
        issues.append(IfoIssue("error", "sanity",
            "VTS declares zero PGCs"))
    if n_pgcs > _MAX_PGCS:
        issues.append(IfoIssue("warn", "sanity",
            f"VTS PGC count={n_pgcs} > soft limit {_MAX_PGCS}"))

    for i in range(min(n_pgcs, _MAX_PGCS)):
        srp = pgcit.pgci_srp[i]
        if not srp.pgc:
            issues.append(IfoIssue("warn", "sanity",
                f"VTS PGC {i+1} pointer is NULL"))
            continue
        pgc = srp.pgc.contents
        n_cells = int(pgc.nr_of_cells)
        if n_cells > _MAX_CELLS_PER_PGC:
            issues.append(IfoIssue("warn", "sanity",
                f"VTS PGC {i+1} cell count={n_cells} > {_MAX_CELLS_PER_PGC}"))
        dur = float(pgc.playback_time.total_seconds)
        if dur > _MAX_DURATION_SECONDS:
            issues.append(IfoIssue("warn", "sanity",
                f"VTS PGC {i+1} duration={dur:.1f}s exceeds {_MAX_DURATION_SECONDS}s"))
        # A PGC with zero cells but non-zero duration (or vice versa) is suspicious.
        if (n_cells == 0) != (dur == 0.0):
            issues.append(IfoIssue("warn", "sanity",
                f"VTS PGC {i+1} cells={n_cells} but duration={dur:.3f}s"))

    return issues


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def _has_menu_vob(dvd, vts_no: int) -> bool:
    """Return True iff VTS_xx_0.VOB (the per-VTS menu VOB) opens via
    libdvdread. Used as the gate for the BUP byte-position check —
    mirrors MakeMKV's decomp at FUN_007e46b0 lines 4798-4803, where the
    empty-VOB-file path falls through to LAB_007e4c62 and skips the
    check entirely."""
    try:
        with dr.open_vob(dvd, vts_no, menu=True):
            return True
    except dr.DvdReadError:
        return False


def _check_ifo_self_declared_size(dvd, title: int, *, is_vmg: bool,
                                  probe: IfoSourceProbe,
                                  issues: List[IfoIssue]) -> None:
    """Verify the IFO/BUP self-declared layout matches reality.

    Three checks, in order of strength:

      1. **File size consistency** — IFO header's ``vmgi_last_sector`` /
         ``vtsi_last_sector`` field declares the IFO's own size in
         sectors. Actual file size must match. BUP size must equal main
         IFO size (it's a backup copy).

      2. **BUP byte-position check** (per-VTS only, gated on menu VOB
         presence) — mirror of FUN_007e46b0 lines 4920-4936. Reads
         vts_last_sector and vtsi_last_sector from the main IFO; the
         expected BUP byte offset within the VTS group is
         ``(vts_last_sector - vtsi_last_sector) << 11``. On UDF/ISO
         sources, compares against ``(bup_lba - ifo_lba) << 11``. On
         folder sources, the comparison cannot succeed (no on-disc
         layout) and the check fires whenever a menu VOB exists —
         matching MakeMKV's emit-or-skip rule (it emits MSG:3002 per
         VTS for every VTS_xx_0.VOB present on the disc).
    """
    if not probe.main_present:
        return
    try:
        main_head = dr.probe_ifo_blocks(dvd, title, backup=False, n_blocks=1)
    except dr.DvdReadError:
        return
    if main_head is None:
        return
    try:
        mat = ifop.parse_ifo_or_bup(main_head, is_vmg=is_vmg)
    except ifop.IfoParseError as e:
        issues.append(IfoIssue("warn", "sanity",
            f"Couldn't parse main IFO header for title {title}: {e}"))
        return

    if is_vmg:
        declared_ifo_sectors = mat.vmgi_last_sector + 1
    else:
        declared_ifo_sectors = mat.vtsi_last_sector + 1

    expected_size = declared_ifo_sectors * ifop.LB_LEN
    if probe.main_size != expected_size:
        issues.append(IfoIssue(
            "warn", "bup_offset_mismatch",
            (f"Title {title}: IFO declares "
             f"{declared_ifo_sectors} sectors "
             f"({expected_size} bytes) but file is {probe.main_size} bytes")))

    # BUP should be a byte-faithful copy of the main IFO — so its size
    # must match too.
    if probe.bup_present and probe.bup_size != probe.main_size:
        issues.append(IfoIssue(
            "warn", "bup_offset_mismatch",
            (f"Title {title}: BUP file size {probe.bup_size} differs from "
             f"main IFO size {probe.main_size}")))

    # BUP byte-position check — mirror of FUN_007e46b0 lines 4920-4936.
    # MakeMKV only emits MSG:3002 for VTSes (never VMG). On the corpus
    # the captured emit pattern is:
    #
    #   ISO sources (UDF available): emit per-VTS whenever the UDF-based
    #   BUP-position differs from the IFO-declared offset. Verified:
    #     * DRAGONAUT_P2 / HARLOCK ISOs: 0 emits (LBA arithmetic matches)
    #     * DRAGONAUT_JP ISO: 4 emits (all 4 VTSes have +16 / +64 sector
    #       BUP shifts; menu VOB only on VTS 1 but MakeMKV still emits
    #       for all 4 — so the menu-VOB gate is folder-only)
    #
    #   Folder sources (no UDF): emit per-VTS that has a menu VOB
    #   (VTS_xx_0.VOB). The on-disc comparison cannot succeed without
    #   UDF, but MakeMKV's check on folder rips fires whenever a menu
    #   VOB is opened (the decomp's plVar17 degenerate-state check at
    #   lines 4798-4803). Verified:
    #     * ANGEL_S1D1: 2 emits (VTS 1+3 — both have menu VOB)
    #     * FOREVER_KNIGHT: 8 emits (all 8 VTSes have menu VOBs)
    #     * Великий Мерлин: 17 emits (all VTSes have menu VOBs)
    #     * Condor Hero: 1 emit (single VTS, has menu VOB)
    #     * TERRA_NOVA: 1 emit (only VTS 3 has menu VOB)
    if is_vmg or not probe.main_present:
        return

    last_sec = mat.vts_last_sector
    ifo_last = mat.vtsi_last_sector
    expected_offset_sectors = last_sec - ifo_last
    expected_offset_bytes = expected_offset_sectors << 11  # × 2048

    ifo_name = f"/VIDEO_TS/VTS_{title:02d}_0.IFO"
    bup_name = f"/VIDEO_TS/VTS_{title:02d}_0.BUP"
    try:
        ifo_udf = dr.udf_find_file(dvd, ifo_name)
        bup_udf = dr.udf_find_file(dvd, bup_name)
    except Exception:
        ifo_udf = bup_udf = None

    if ifo_udf is not None and bup_udf is not None:
        # UDF / ISO path: compute the actual byte offset and compare.
        # Menu VOB presence is NOT a gate here — MakeMKV emits per-VTS
        # whenever the UDF-derived BUP offset differs from IFO-declared.
        ifo_lba, _ = ifo_udf
        bup_lba, _ = bup_udf
        actual_offset_sectors = bup_lba - ifo_lba
        actual_offset_bytes = actual_offset_sectors << 11
        if expected_offset_bytes != actual_offset_bytes:
            delta = actual_offset_bytes - expected_offset_bytes
            issues.append(IfoIssue(
                "warn", "bup_offset_mismatch",
                (f"Title {title}: BUP byte offset {actual_offset_bytes} "
                 f"(LBA {bup_lba}) differs from IFO-derived expected "
                 f"offset {expected_offset_bytes} "
                 f"(= {expected_offset_sectors} sectors); delta={delta:+d}")))
    else:
        # Folder path: no UDF layout to honor. MakeMKV's check fires
        # iff the VTS has a menu VOB (gated by the decomp's plVar17
        # non-degenerate-state check). Emit MSG:3002 to mirror.
        if _has_menu_vob(dvd, title):
            issues.append(IfoIssue(
                "warn", "bup_offset_mismatch",
                (f"Title {title}: folder source — IFO declares BUP byte "
                 f"offset {expected_offset_bytes} "
                 f"(= {expected_offset_sectors} sectors) but folder "
                 f"layout cannot verify on-disc position")))


def inspect_ifo_source(dvd, title: int, *, is_vmg: bool) -> IfoReport:
    """Probe the raw IFO/BUP for ``title`` and validate the loaded handle.

    ``title=0`` = VMG, ``title>=1`` = VTS. The returned IfoReport carries
    every signal the inspector needs to surface."""
    probe = probe_ifo_pair(dvd, title)
    issues: List[IfoIssue] = []

    if not probe.main_present and not probe.bup_present:
        issues.append(IfoIssue("error", "missing_main",
            f"Both IFO and BUP missing for title {title}"))
        return IfoReport(title=title, is_vmg=is_vmg,
                         probe=probe, issues=issues)
    if not probe.main_present:
        issues.append(IfoIssue("warn", "missing_main",
            f"Main IFO missing for title {title}; libdvdread will use BUP"))
    elif not probe.bup_present:
        issues.append(IfoIssue("warn", "missing_bup",
            f"BUP missing for title {title}; recovery options reduced"))
    elif probe.content_matches is False:
        issues.append(IfoIssue("warn", "diverged",
            f"IFO and BUP differ for title {title} — possible corruption"))

    _check_ifo_self_declared_size(dvd, title, is_vmg=is_vmg,
                                  probe=probe, issues=issues)

    # Emit MakeMKV-equivalent MSG codes for the issues we detected.
    # Dedup: MakeMKV emits at most one MSG:3002 per VTS regardless of how
    # many sub-checks fire (the decomp emit-site at FUN_007e46b0 line 4935
    # is a single FUN_00800d90(0xbba, ...) call gated by a single
    # comparison; multiple issues in our IfoReport map to the same
    # observable emit count).
    #
    # MSG:3002 = "Calculated %s offset for VTS #%d does not match one
    #             in IFO header" — fires for bup_offset_mismatch AND
    #             content-diverged issues. VTS-only; MakeMKV never
    #             emits MSG:3002 for VMG (captured logs show no
    #             "BUP","0" entry).
    # MSG:3003 = "Using BUP for VTS X" — when main is missing.
    # MSG:3042 = "IFO/BUP repair: %s — needs VOB scan" — only when
    #             FUN_007e5680's audio-attr binsearch fails. Not emitted
    #             from this path; deferred to Group H.
    from . import mkv_msg_log
    emitted_3002 = False
    emitted_3003 = False
    for issue in issues:
        if (issue.category in ("bup_offset_mismatch", "diverged")
                and not emitted_3002 and not is_vmg):
            mkv_msg_log.emit(3002, "BUP", title,
                              vts=title, severity=issue.severity,
                              reason=issue.message)
            emitted_3002 = True
        elif (issue.category == "missing_main"
                and probe.bup_present and not emitted_3003):
            mkv_msg_log.emit(3003, title,
                              vts=title, reason=issue.message)
            emitted_3003 = True

    try:
        with dr.open_ifo(dvd, title) as ifo:
            issues.extend(validate_ifo_handle(ifo, is_vmg=is_vmg))
    except dr.DvdReadError as e:
        issues.append(IfoIssue("error", "sanity",
            f"ifoOpen({title}) failed: {e}"))

    # For VTSes, also compare main vs BUP audio/sub counts.
    # ``Великий Мерлин`` VTS_01 has main_audio=0, bup_audio=8 — silent
    # data-loss case unless the rip path knows to prefer BUP.
    main_a = main_s = bup_a = bup_s = None
    counts_div = False
    preferred = None
    if not is_vmg and probe.main_present:
        try:
            main_head = dr.probe_ifo_blocks(dvd, title, backup=False, n_blocks=1)
            if main_head is not None:
                main_mat = ifop.parse_vts_mat(main_head)
                main_a = main_mat.nr_of_audio_streams
                main_s = main_mat.nr_of_subp_streams
        except (dr.DvdReadError, ifop.IfoParseError):
            pass
        if probe.bup_present:
            try:
                bup_head = dr.probe_ifo_blocks(dvd, title, backup=True, n_blocks=1)
                if bup_head is not None:
                    bup_mat = ifop.parse_vts_mat(bup_head)
                    bup_a = bup_mat.nr_of_audio_streams
                    bup_s = bup_mat.nr_of_subp_streams
            except (dr.DvdReadError, ifop.IfoParseError):
                pass
        if main_a is not None and bup_a is not None:
            counts_div = (main_a != bup_a) or (main_s != bup_s)
            if counts_div:
                # Heuristic: trust the source with non-zero values when one
                # is zero. If both are non-zero but differ, prefer the
                # larger (more permissive — fewer missed streams).
                if main_a == 0 and main_s == 0 and (bup_a > 0 or bup_s > 0):
                    preferred = "bup"
                elif bup_a == 0 and bup_s == 0 and (main_a > 0 or main_s > 0):
                    preferred = "main"
                else:
                    preferred = "main"  # both plausible, default to libdvdread
                issues.append(IfoIssue(
                    "warn", "counts_diverge",
                    (f"Title {title}: main IFO declares "
                     f"audio={main_a}/sub={main_s} but BUP declares "
                     f"audio={bup_a}/sub={bup_s} — preferring '{preferred}'")))

    return IfoReport(
        title=title, is_vmg=is_vmg, probe=probe, issues=issues,
        main_audio_count=main_a, main_subp_count=main_s,
        bup_audio_count=bup_a, bup_subp_count=bup_s,
        counts_diverge=counts_div, preferred_attr_source=preferred,
    )


# ---------------------------------------------------------------------------
# JSON serialisation helpers (kept stable for tests + downstream consumers)
# ---------------------------------------------------------------------------

def probe_to_dict(p: IfoSourceProbe) -> dict:
    return {
        "title": p.title,
        "main_present": p.main_present,
        "bup_present": p.bup_present,
        "main_size": p.main_size,
        "bup_size": p.bup_size,
        "content_matches": p.content_matches,
        "effective_source": p.effective_source,
    }


def issue_to_dict(i: IfoIssue) -> dict:
    return {
        "severity": i.severity,
        "category": i.category,
        "message": i.message,
    }


def report_to_dict(r: IfoReport) -> dict:
    return {
        "title": r.title,
        "is_vmg": r.is_vmg,
        "probe": probe_to_dict(r.probe),
        "issues": [issue_to_dict(i) for i in r.issues],
        "ok": r.ok,
        "main_audio_count": r.main_audio_count,
        "main_subp_count": r.main_subp_count,
        "bup_audio_count": r.bup_audio_count,
        "bup_subp_count": r.bup_subp_count,
        "counts_diverge": r.counts_diverge,
        "preferred_attr_source": r.preferred_attr_source,
    }


# ---------------------------------------------------------------------------
# CSS state
# ---------------------------------------------------------------------------

def detect_css(disc_path: str | Path) -> dict:
    """Detect whether ``disc_path`` is CSS-scrambled via libdvdcss.

    Returns ``{"scrambled": bool | None, "detected_by": "libdvdcss" | "unavailable"}``.
    ``scrambled=None`` when libdvdcss isn't installed or the disc couldn't be
    opened. The native rip path still works in either case — libdvdread auto-
    invokes libdvdcss internally; this helper exists purely so the UI / JSON
    output can show users why a key cache entry appeared (or didn't)."""
    if not dc.is_available():
        return {"scrambled": None, "detected_by": "unavailable"}
    scrambled = dc.is_disc_scrambled(disc_path)
    return {
        "scrambled": scrambled,
        "detected_by": "libdvdcss" if scrambled is not None else "unavailable",
    }


__all__ = [
    "IfoSourceProbe",
    "IfoIssue",
    "IfoReport",
    "probe_ifo_pair",
    "validate_ifo_handle",
    "inspect_ifo_source",
    "probe_to_dict",
    "issue_to_dict",
    "report_to_dict",
    "detect_css",
]
