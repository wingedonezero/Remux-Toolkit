"""
VM-driven title discovery — port of MakeMKV's FUN_007ff000.

MakeMKV silently drops titles whose **DVD-VM pre-commands jump away before
the title's cells ever play**. A title where ``LinkPGCN`` / ``CallSS_VMGM_PGC``
fires in the pre-command sequence — and the called PGC never returns control
to the original title — is an authoring stub, not a real title. This module
replicates that filter using a pure-Python port of libdvdnav's DVD VM
(see :mod:`dvd_vm`).

## Algorithm

For each TT_SRPT title:

1. Look up the title's VTS + PGCN via libdvdread's TT_SRPT.
2. Execute the title's PGC pre-commands via :func:`dvd_vm.vm_eval_cmds`.
3. Recursively trace any resulting Link/Call/Jump:
   - ``LinkNoLink`` → pre-cmds didn't jump → title's cells will play → **reachable**.
   - ``CallSS_VMGM_PGC N`` → execute VMGM PGC N's pre/post commands too,
     with the current register state. If that PGC eventually links back to
     somewhere that lets our original PGC play → **reachable**. Otherwise →
     **unreachable** (silent drop).
   - ``LinkPGCN N`` → permanent jump within current VTS → unreachable (the
     original title's cells never play).
   - ``Exit`` / ``JumpSS_FP`` → halt → unreachable.
4. Cycle detection: each (vts, pgcn) trace can be visited at most once.
5. Depth cap: 16 levels of recursion (generous; real discs nest at most 3-4).

The output is a ``set[(vts, pgcn)]`` of titles whose cells WILL play under the
VM walk. Titles outside this set are the silent-drop set — our analyzer
suppresses MSG:3026 for them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ...bindings import libdvdread as lr
from . import dvd_vm
from .dvd_vm import Link, LinkCmd, Registers


_log = logging.getLogger("remux.vm_title_discovery")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Maximum recursive trace depth — generous safety bound for cycle-free walks.
#: Real discs never nest VMGM-call chains more than 3-4 deep.
MAX_TRACE_DEPTH = 16

#: Title is "reachable" if its PGC's cells will play. These Link outcomes
#: from pre-command execution mean the cells WON'T play (the VM jumps away).
PERMANENT_JUMPS = frozenset({
    LinkCmd.Exit,
    LinkCmd.JumpTT,
    LinkCmd.JumpVTS_TT,
    LinkCmd.JumpVTS_PTT,
    LinkCmd.JumpSS_FP,
    LinkCmd.JumpSS_VMGM_MENU,
    LinkCmd.JumpSS_VTSM,
    LinkCmd.JumpSS_VMGM_PGC,
    LinkCmd.LinkPGCN,    # within-VTS jump — original PGC's cells skipped
    LinkCmd.LinkPGN,
    LinkCmd.LinkCN,
    LinkCmd.LinkPTTN,
    # LinkRSM = "resume from saved state" — can return to caller; treat as call
})

#: Call operations that can return to the caller (mark reachable IF the
#: callee's post-cmds don't permanently jump away).
CALL_OPS = frozenset({
    LinkCmd.CallSS_FP,
    LinkCmd.CallSS_VMGM_MENU,
    LinkCmd.CallSS_VTSM,
    LinkCmd.CallSS_VMGM_PGC,
})


# ---------------------------------------------------------------------------
# Helpers to fetch PGC bytecode via libdvdread
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PgcRef:
    """A reference to a specific PGC on the disc.

    ``vts == 0`` means the VMGM (root menu) PGC namespace; otherwise it
    indexes into the corresponding VTS's ``vts_pgcit``.
    """
    vts: int
    pgcn: int

    def __repr__(self) -> str:
        ns = "VMGM" if self.vts == 0 else f"VTS{self.vts}"
        return f"{ns}/PGC{self.pgcn}"


@dataclass
class PgcCommands:
    """The three command tables of a PGC."""
    pre: list[bytes] = field(default_factory=list)
    post: list[bytes] = field(default_factory=list)
    cell: list[bytes] = field(default_factory=list)
    valid: bool = True  # False if PGC doesn't exist


def _load_pgc_commands(reader, ref: PgcRef) -> PgcCommands:
    """Load the pre/post/cell commands for ``ref`` via libdvdread."""
    try:
        with lr.open_ifo(reader, ref.vts) as ifo:
            if ref.vts == 0:
                # VMGM menus — pgci_ut[0].lu[0].pgcit
                if not ifo[0].pgci_ut:
                    return PgcCommands(valid=False)
                pgci_ut = ifo[0].pgci_ut[0]
                if pgci_ut.nr_of_lus == 0:
                    return PgcCommands(valid=False)
                lu = pgci_ut.lu[0]
                if not lu.pgcit:
                    return PgcCommands(valid=False)
                pgcit = lu.pgcit[0]
            else:
                # VTS title PGCs — vts_pgcit
                if not ifo[0].vts_pgcit:
                    return PgcCommands(valid=False)
                pgcit = ifo[0].vts_pgcit[0]

            if ref.pgcn < 1 or ref.pgcn > pgcit.nr_of_pgci_srp:
                return PgcCommands(valid=False)
            srp = pgcit.pgci_srp[ref.pgcn - 1]
            pgc = srp.pgc
            if not pgc:
                return PgcCommands(valid=False)
            ct = pgc[0].command_tbl
            if not ct:
                return PgcCommands(valid=True)  # PGC exists but has no commands
            ct = ct[0]
            pre = [bytes(ct.pre_cmds[i].bytes) for i in range(ct.nr_of_pre)] if ct.pre_cmds else []
            post = [bytes(ct.post_cmds[i].bytes) for i in range(ct.nr_of_post)] if ct.post_cmds else []
            cell = [bytes(ct.cell_cmds[i].bytes) for i in range(ct.nr_of_cell)] if ct.cell_cmds else []
            return PgcCommands(pre=pre, post=post, cell=cell)
    except Exception as e:
        _log.debug(f"_load_pgc_commands({ref}) failed: {e}")
        return PgcCommands(valid=False)


def _build_ttsrpt_map(reader) -> dict[int, tuple[int, int]]:
    """Build the global-TT-number → (vts, pgcn) map from TT_SRPT.

    The TT_SRPT entry tells us (vts, vts_ttn). To get the PGCN for a (vts,
    vts_ttn) we use the VTS's vts_ptt_srpt (title-program-mapping). The PGCN
    of a title's first chapter is the title's main PGCN.
    """
    out: dict[int, tuple[int, int]] = {}
    try:
        with lr.open_ifo(reader, 0) as vmgi_ifo:
            ttsrpt = vmgi_ifo[0].tt_srpt
            if not ttsrpt:
                return out
            n = ttsrpt[0].nr_of_srpts
            for tt in range(1, n + 1):
                entry = ttsrpt[0].title[tt - 1]
                vts = entry.title_set_nr
                vts_ttn = entry.vts_ttn
                # Resolve (vts, vts_ttn) → pgcn via the VTS's ptt_srpt
                try:
                    with lr.open_ifo(reader, vts) as vts_ifo:
                        ptt_srpt = vts_ifo[0].vts_ptt_srpt
                        if (not ptt_srpt or vts_ttn < 1
                                or vts_ttn > ptt_srpt[0].nr_of_srpts):
                            continue
                        ttu = ptt_srpt[0].title[vts_ttn - 1]
                        if ttu.nr_of_ptts < 1:
                            continue
                        # First chapter's PGC = title's main PGC
                        pgcn = ttu.ptt[0].pgcn
                        out[tt] = (vts, pgcn)
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _resolve_link_target(link: Link, current_vts: int,
                         ttsrpt_map: dict[int, tuple[int, int]]) -> Optional[PgcRef]:
    """Map a Link result to the PGC it transfers to.

    Returns None for terminal links (Exit, JumpSS_FP, etc.) or links we
    can't statically resolve (LinkRSM — resume target depends on caller).
    """
    if link.command == LinkCmd.LinkPGCN:
        return PgcRef(vts=current_vts, pgcn=link.data1)
    if link.command in (LinkCmd.CallSS_VMGM_PGC, LinkCmd.JumpSS_VMGM_PGC):
        return PgcRef(vts=0, pgcn=link.data1)
    if link.command == LinkCmd.JumpTT:
        # Global TT_SRPT title → look up (vts, pgcn)
        mapped = ttsrpt_map.get(link.data1)
        if mapped is None:
            return None
        return PgcRef(vts=mapped[0], pgcn=mapped[1])
    if link.command == LinkCmd.JumpVTS_TT:
        # Within current VTS — need vts_ptt_srpt; we approximate by treating
        # data1 as a (probably-correct) PGCN since each VTS-TTN typically
        # maps to its own PGC. (Imperfect for multi-PGC titles.)
        return PgcRef(vts=current_vts, pgcn=link.data1)
    if link.command == LinkCmd.JumpVTS_PTT:
        # Within current VTS, specific (TTN, PTT). For most titles PTT=1 -> pgcn=ttn.
        return PgcRef(vts=current_vts, pgcn=link.data1)
    # Other links not yet handled
    return None


def _trace_reachability(reader, ref: PgcRef, target: PgcRef,
                         registers: Registers,
                         visited: set[tuple[int, int]],
                         ttsrpt_map: dict[int, tuple[int, int]],
                         depth: int = 0) -> bool:
    """Return True if ``ref``'s execution reaches ``target``'s cells.

    Recursively traces nav commands. Cycle-broken via ``visited``. Bounded by
    :data:`MAX_TRACE_DEPTH`.
    """
    if depth > MAX_TRACE_DEPTH:
        return False
    key = (ref.vts, ref.pgcn)
    if key in visited:
        return False
    visited.add(key)

    cmds = _load_pgc_commands(reader, ref)
    if not cmds.valid:
        return False

    # Execute pre-commands. If they don't jump, the PGC's cells play.
    if cmds.pre:
        link, registers = dvd_vm.vm_eval_cmds(cmds.pre, registers)
    else:
        link = Link(LinkCmd.LinkNoLink)

    if link.command == LinkCmd.LinkNoLink:
        # No jump in pre-commands — this PGC's cells WILL play.
        # If this PGC IS the target, we're done.
        if (ref.vts, ref.pgcn) == (target.vts, target.pgcn):
            return True
        # Otherwise the cells play and then post-commands run.
        # Trace through post-cmds too, with the same logic.
        if cmds.post:
            link2, registers = dvd_vm.vm_eval_cmds(cmds.post, registers)
            if link2.command == LinkCmd.LinkNoLink:
                return False  # post fell through, no link back to target
            return _follow_link(reader, link2, registers, ref, target,
                                visited, ttsrpt_map, depth + 1)
        return False  # No way to reach target from here

    # Pre-commands fired a link/call/jump. Follow it.
    return _follow_link(reader, link, registers, ref, target,
                        visited, ttsrpt_map, depth + 1)


def _follow_link(reader, link: Link, registers: Registers,
                  caller: PgcRef, target: PgcRef,
                  visited: set[tuple[int, int]],
                  ttsrpt_map: dict[int, tuple[int, int]],
                  depth: int) -> bool:
    """Follow a link result; return True if it reaches ``target``."""
    if link.command in PERMANENT_JUMPS:
        # Permanent jump away. If the target is the link destination, reachable;
        # otherwise unreachable.
        dest = _resolve_link_target(link, caller.vts, ttsrpt_map)
        if dest == target:
            return True
        if dest is not None:
            return _trace_reachability(reader, dest, target, registers,
                                        visited, ttsrpt_map, depth)
        return False

    if link.command in CALL_OPS:
        # Call: callee runs, then control returns. If the callee itself
        # reaches target → reachable.
        dest = _resolve_link_target(link, caller.vts, ttsrpt_map)
        if dest is None:
            return False
        return _trace_reachability(reader, dest, target, registers,
                                    visited, ttsrpt_map, depth)

    if link.command == LinkCmd.LinkRSM:
        return False

    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_title_reachable(reader, vts: int, pgcn: int,
                        ttsrpt_map: Optional[dict[int, tuple[int, int]]] = None,
                        ) -> bool:
    """Return True iff title ``(vts, pgcn)``'s cells play under the VM walk.

    A title is "reachable" iff its PGC pre-commands don't permanently jump
    away from the PGC before its cells play, OR they jump away and the
    transitive nav-VM walk eventually returns to play this PGC's cells.
    Mirrors MakeMKV's silent-drop filter (titles failing this check are
    silently dropped — no MSG:3026, no MSG:3028, just a debug log).

    ``ttsrpt_map`` can be passed in if you've already built it (avoids
    redundant IFO opens when probing many titles on the same disc).
    """
    target = PgcRef(vts=vts, pgcn=pgcn)
    regs = Registers.default()
    regs.SPRM[5] = vts
    regs.SPRM[6] = pgcn
    if ttsrpt_map is None:
        ttsrpt_map = _build_ttsrpt_map(reader)
    visited: set[tuple[int, int]] = set()
    return _trace_reachability(reader, target, target, regs,
                                visited, ttsrpt_map, depth=0)


def _walk_global_nav_graph(reader,
                            ttsrpt_map: dict[int, tuple[int, int]],
                            ) -> set[tuple[int, int]]:
    """Compute the transitive closure of PGCs reachable via nav commands.

    Walks every PGC in every VTS + the VMGM, following any LinkPGCN / JumpTT /
    Jump/Call targets in their pre/post/cell commands. Output is a set of
    ``(vts, pgcn)`` that ANY nav command can reach from anywhere on the disc.

    This catches titles whose own pre-commands jump away but which are
    reachable via *some other PGC's* nav command — typically titles
    referenced from menu PGCs.
    """
    reached: set[tuple[int, int]] = set()
    # Seed with TT_SRPT entries (always-considered destinations)
    for (vts, pgcn) in ttsrpt_map.values():
        reached.add((vts, pgcn))
    # Walk all PGCs in VMGM + all VTSes
    try:
        with lr.open_ifo(reader, 0) as vmgi_ifo:
            tt_srpt = vmgi_ifo[0].tt_srpt
            n_vts = 0
            if tt_srpt:
                for tt in range(tt_srpt[0].nr_of_srpts):
                    n_vts = max(n_vts, tt_srpt[0].title[tt].title_set_nr)
    except Exception:
        n_vts = 0

    def scan_pgc(ref: PgcRef) -> None:
        cmds = _load_pgc_commands(reader, ref)
        if not cmds.valid:
            return
        for cmd_list in (cmds.pre, cmds.post, cmds.cell):
            for raw in cmd_list:
                link, _ = dvd_vm.vm_eval_cmds([raw])
                if link.command == LinkCmd.LinkPGCN:
                    reached.add((ref.vts, link.data1))
                elif link.command in (LinkCmd.CallSS_VMGM_PGC, LinkCmd.JumpSS_VMGM_PGC):
                    reached.add((0, link.data1))
                elif link.command == LinkCmd.JumpTT:
                    mapped = ttsrpt_map.get(link.data1)
                    if mapped is not None:
                        reached.add(mapped)
                elif link.command in (LinkCmd.JumpVTS_TT, LinkCmd.JumpVTS_PTT):
                    reached.add((ref.vts, link.data1))

    # VMGM PGCs
    try:
        with lr.open_ifo(reader, 0) as vmgi_ifo:
            if vmgi_ifo[0].pgci_ut:
                pgci_ut = vmgi_ifo[0].pgci_ut[0]
                if pgci_ut.nr_of_lus > 0:
                    lu = pgci_ut.lu[0]
                    if lu.pgcit:
                        for p in range(1, lu.pgcit[0].nr_of_pgci_srp + 1):
                            scan_pgc(PgcRef(vts=0, pgcn=p))
    except Exception:
        pass
    # All VTS PGCs
    for vts in range(1, n_vts + 1):
        try:
            with lr.open_ifo(reader, vts) as ifo:
                if not ifo[0].vts_pgcit:
                    continue
                for p in range(1, ifo[0].vts_pgcit[0].nr_of_pgci_srp + 1):
                    scan_pgc(PgcRef(vts=vts, pgcn=p))
        except Exception:
            continue
    return reached


def discover_reachable_titles(disc_path: str | Path,
                              titles: list[tuple[int, int]],
                              *,
                              use_global_nav_graph: bool = False,
                              ) -> set[tuple[int, int]]:
    """Run VM-driven title discovery for every (vts, pgcn) in ``titles``.

    Args:
        disc_path: Path to disc (folder, ISO, or device).
        titles: List of (vts_no, pgcn) pairs derived from TT_SRPT.
        use_global_nav_graph: If True (default), also union with the
            global nav-graph closure — catches titles reachable via menu
            commands even though their own pre-commands jump away.

    Returns:
        Set of (vts, pgcn) pairs that survive the VM-walk filter.
        Titles outside this set are MakeMKV's silent-drop set.
    """
    reachable: set[tuple[int, int]] = set()
    with lr.open_disc(str(disc_path)) as reader:
        ttsrpt_map = _build_ttsrpt_map(reader)
        # Per-title local trace
        for (vts, pgcn) in titles:
            if is_title_reachable(reader, vts, pgcn, ttsrpt_map):
                reachable.add((vts, pgcn))
        # Union with global nav-graph closure (catches "linked from menu" titles)
        if use_global_nav_graph:
            global_reach = _walk_global_nav_graph(reader, ttsrpt_map)
            for (vts, pgcn) in titles:
                if (vts, pgcn) in global_reach:
                    reachable.add((vts, pgcn))
    return reachable


# ---------------------------------------------------------------------------
# Diagnostic helpers (unchanged)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PgcSignals:
    """Per-PGC fields useful for diagnostics."""
    vts: int
    pgcn: int
    nr_of_cells: int
    duration_seconds: float
    nr_of_pre_cmds: int
    nr_of_post_cmds: int
    nr_of_cell_cmds: int
    next_pgc_nr: int
    prev_pgc_nr: int
    goup_pgc_nr: int
    still_time: int


def pgc_signals(disc_path: str | Path, vts: int, pgcn: int) -> Optional[PgcSignals]:
    """Diagnostic: dump per-PGC command counts + nav links via libdvdread."""
    try:
        with lr.open_disc(str(disc_path)) as reader:
            with lr.open_ifo(reader, vts) as ifo:
                vts_pgcit = ifo[0].vts_pgcit
                if not vts_pgcit:
                    return None
                if pgcn < 1 or pgcn > vts_pgcit[0].nr_of_pgci_srp:
                    return None
                pgc_ptr = vts_pgcit[0].pgci_srp[pgcn - 1].pgc
                if not pgc_ptr:
                    return None
                p = pgc_ptr[0]
                pre = post = cell = 0
                if p.command_tbl:
                    ct = p.command_tbl[0]
                    pre, post, cell = ct.nr_of_pre, ct.nr_of_post, ct.nr_of_cell
                return PgcSignals(
                    vts=vts, pgcn=pgcn,
                    nr_of_cells=p.nr_of_cells,
                    duration_seconds=p.playback_time.total_seconds,
                    nr_of_pre_cmds=pre, nr_of_post_cmds=post, nr_of_cell_cmds=cell,
                    next_pgc_nr=p.next_pgc_nr, prev_pgc_nr=p.prev_pgc_nr,
                    goup_pgc_nr=p.goup_pgc_nr, still_time=p.still_time,
                )
    except Exception:
        return None


__all__ = [
    "MAX_TRACE_DEPTH",
    "PgcRef",
    "PgcCommands",
    "PgcSignals",
    "discover_reachable_titles",
    "is_title_reachable",
    "pgc_signals",
]
