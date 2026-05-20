"""
VM-driven title discovery — Group E partial port of MakeMKV's
FUN_007fdc50 chain (see research/AUDIT.md §1.4).

MakeMKV silently drops titles whose **DVD-VM pre-commands jump away before
the title's cells ever play**. A title where ``LinkPGCN`` / ``CallSS_VMGM_PGC``
fires in the pre-command sequence — and the called PGC never returns control
to the original title — is an authoring stub, not a real title. This module
replicates that filter using a pure-Python port of libdvdnav's DVD VM
(see :mod:`dvd_vm`).

Group E status: the PCI button-graph walker (FUN_00800aa0 + FUN_00800cc0
+ FUN_007fdc10, ~720 B) is ported here as ``button_graph_closure`` and
``button_graph_walk``. The full FUN_007fdc50 forward-walk + FUN_007ff000
VM-step engine (~10 KB combined, tightly coupled with disc_open_enumerate
state) is deferred to a follow-up that lands alongside Group F.

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

import ctypes
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


# ---------------------------------------------------------------------------
# Port of FUN_007fdc10 — strict-weak ordering comparator over anchor keys
# ---------------------------------------------------------------------------

def _anchor_key_lt(a: tuple[int, int, int, int],
                    b: tuple[int, int, int, int]) -> bool:
    """Port of FUN_007fdc10 (36 B).

    Strict-weak-less over an 8-byte anchor key encoded as the 4-tuple
    ``(pgcn_or_lba: u32, ptt_or_cell: u16, mode: u8, cell_n_minus_1: u8)``.
    Comparison order matches the decomp's lexicographic walk:
    ``(mode, ptt_or_cell, pgcn_or_lba, cell_n_minus_1)`` — note that
    the decomp's "byte+6" tuple member sorts first; that maps to our
    ``mode`` (the third tuple field) per the anchor key layout in the
    walker. See research/AUDIT.md §1.4 Group E + subagent decomp report.
    """
    if a[2] != b[2]:        # mode (byte at +6)
        return a[2] < b[2]
    if a[1] != b[1]:        # ptt_or_cell (short at +4)
        return a[1] < b[1]
    if a[0] != b[0]:        # pgcn_or_lba (uint at +0)
        return a[0] < b[0]
    return a[3] < b[3]      # cell_n_minus_1 (byte at +7)


# ---------------------------------------------------------------------------
# Port of FUN_00800cc0 — recursive button-graph transitive closure
# ---------------------------------------------------------------------------

#: Max valid button count in a PCI HLI table (per DVD-Video spec: 36).
_MAX_BUTTONS = 36


def button_graph_closure(button_links: dict[int, tuple[int, int, int, int]],
                          start_button: int) -> set[int]:
    """Port of FUN_00800cc0 (151 B).

    Walks the HLI button neighbour graph and returns every button
    reachable from ``start_button`` via up/down/left/right links.

    Args:
        button_links: Dict mapping 1-based button index → (up, down,
            left, right) tuple of 1-based neighbour button indices.
            Use ``bindings.libdvdnav.pci_button_links`` to populate.
        start_button: 1-based button index to start walking from.

    The decomp's recursion:

        void closure(uint64_t *bitmap, btni_t *hli, byte cur_button):
            while cur_button != 0:
                if cur_button > 0x24 or cur_button > nbtn: return
                bit = cur_button - 1
                if (*bitmap >> bit) & 1: return     # visited
                *bitmap |= 1 << bit
                base = hli[bit].btni_t + 0x32
                closure(bitmap, hli, base.up)
                closure(bitmap, hli, base.down)
                closure(bitmap, hli, base.left)
                cur_button = base.right             # tail call

    The Python port uses an explicit work-list to avoid hitting
    Python's recursion limit on densely-connected button graphs.
    """
    if start_button < 1 or start_button > _MAX_BUTTONS:
        return set()
    visited: set[int] = set()
    work: list[int] = [start_button]
    while work:
        cur = work.pop()
        if cur < 1 or cur > _MAX_BUTTONS:
            continue
        if cur in visited:
            continue
        if cur not in button_links:
            continue
        visited.add(cur)
        up, down, left, right = button_links[cur]
        # Match the decomp's traversal order: up, down, left, then right
        # tail-called (LIFO via append/pop reverses, but the visited set
        # makes order semantically equivalent).
        if up:
            work.append(up)
        if down:
            work.append(down)
        if left:
            work.append(left)
        if right:
            work.append(right)
    return visited


# ---------------------------------------------------------------------------
# Port of FUN_00800aa0 — per-PCI button-walker
# ---------------------------------------------------------------------------

def button_graph_walk(pci_addr: int) -> list[bytes]:
    """Port of FUN_00800aa0 (531 B).

    For one PCI's HLI button table, walk every button reachable from
    the default selected button (forcedly-selected, or 1 if none), then
    return the 8-byte VM commands embedded in each reachable button's
    btni_t.cmd field. Caller can feed each command through
    ``dvd_vm.vm_eval_cmds`` to discover its link target.

    Args:
        pci_addr: opaque pointer to a pci_t (from
            ``bindings.libdvdnav.get_current_nav_pci``).

    Returns:
        List of 8-byte ``vm_cmd_t`` blobs, one per reachable button.
        Empty if the PCI has no buttons.

    The decomp's algorithm:

        nbtn = HLI.btn_ns          # number of buttons
        if nbtn == 0: return
        default = HLI.fosl_btnn or 1, clamped to nbtn
        reach = closure(start=default)
        for btn in reverse(reach):
            anchor = pack(cell metadata + btn)
            if anchor not in dedup_set:
                VM step to button btn (vt[0x28](dom, 5, btn))
                if anchor STILL not in dedup_set:
                    log; push anchor

    Our port skips the dedup_set + VM-step coupling (those are
    FUN_007fec70 + FUN_007fedf0, which need the full walker state).
    Instead we return the button cmd bytes so callers can integrate
    into their own reachability model.
    """
    from ...bindings import libdvdnav as ldn
    import ctypes

    if pci_addr == 0:
        return []
    nbtn = ldn.pci_button_count(pci_addr)
    if nbtn == 0:
        return []

    # Default start button = forcedly-selected, or 1 if FOSL is 0 / out
    # of range. The decomp clamps via "min(default, nbtn)".
    default = ldn.pci_force_selected_button(pci_addr)
    if default < 1 or default > nbtn:
        default = 1

    # Build the button-link dict by querying the binding for each
    # button.
    button_links: dict[int, tuple[int, int, int, int]] = {}
    for i in range(1, nbtn + 1):
        button_links[i] = ldn.pci_button_links(pci_addr, i)

    reach = button_graph_closure(button_links, default)
    if not reach:
        return []

    # Extract the 8-byte vm_cmd_t from each reachable button's btni_t.
    # btni_t layout (18 bytes total): 4 bytes geometry, 4 bytes
    # geometry, 4 bytes UDLR links, 8 bytes vm_cmd_t at offset 0x0a.
    # The cmd is at PCI_BTNIT_OFFSET + (btn-1) * PCI_BTNI_SIZE + 10.
    cmds: list[bytes] = []
    for btn in sorted(reach):
        cmd_offset = (
            ldn.PCI_BTNIT_OFFSET
            + (btn - 1) * ldn.PCI_BTNI_SIZE
            + 10  # offset of vm_cmd_t within btni_t
        )
        cmd_bytes_ptr = ctypes.cast(
            pci_addr + cmd_offset, ctypes.POINTER(ctypes.c_ubyte * 8)
        )
        cmd_bytes = bytes(cmd_bytes_ptr[0])
        cmds.append(cmd_bytes)
    return cmds


# ---------------------------------------------------------------------------
# Disc-wide button-graph reachability via libdvdnav playback
# ---------------------------------------------------------------------------

def _walk_button_graph(disc_path: str,
                       ttsrpt_map: dict[int, tuple[int, int]],
                       ) -> set[tuple[int, int]]:
    """Drive a libdvdnav session through every menu and collect PCI
    button commands. Return the set of (vts, pgcn) targets reachable
    via any menu button on the disc.

    This is the integration point for the FUN_00800aa0 / 00800cc0 port.
    Unlike libdvdread (IFO-only), libdvdnav surfaces PCI button data
    as it plays through NAV packets — we walk every menu PGC briefly
    and extract its button command set.

    Strategy:

      1. Open the disc with libdvdnav.
      2. For each top-level menu invocation
         (DVD_MENU_Title / Root / Subpicture / Audio / Angle / Chapter),
         step through up to ``_BUTTON_GRAPH_BLOCK_LIMIT`` blocks, collecting
         every PCI we see.
      3. For each PCI, call ``button_graph_walk`` to extract reachable
         button commands.
      4. Run each command through ``dvd_vm.vm_eval_cmds`` to determine
         its link target. Add resolved (vts, pgcn) targets to ``reach``.

    The walk is bounded — we stop at STILL_FRAME / WAIT / STOP events,
    don't infinite-loop on menu graphs, and skip menus that fail to
    open.
    """
    from ...bindings import libdvdnav as ldn

    reach: set[tuple[int, int]] = set()
    _BLOCK_LIMIT_PER_STAGE = 512
    # DVD-Video menu IDs (libdvdnav's DVDMenuID_t).
    _MENU_IDS = (
        2,  # Title
        3,  # Root
        4,  # Subpicture
        5,  # Audio
        6,  # Angle
        7,  # Part (Chapter)
    )

    def _drain_pcis(nav, block_limit: int) -> set[int]:
        """Step through blocks, return the set of PCI pointers encountered.
        Stops on STOP, exits early when no new PCI for 64 consecutive blocks."""
        buf = (ctypes.c_uint8 * ldn.DVD_VIDEO_LB_LEN)()
        pcis: set[int] = set()
        idle_streak = 0
        for _ in range(block_limit):
            try:
                evt, _length = ldn.get_next_block(nav, buf)
            except Exception:
                break
            if evt == ldn.DVDNAV_STOP:
                break
            if evt == ldn.DVDNAV_STILL_FRAME:
                try:
                    ldn.still_skip(nav)
                except Exception:
                    break
            elif evt == ldn.DVDNAV_WAIT:
                try:
                    ldn.wait_skip(nav)
                except Exception:
                    break
            elif evt == ldn.DVDNAV_NAV_PACKET:
                pci = ldn.get_current_nav_pci(nav)
                if pci and pci not in pcis:
                    pcis.add(pci)
                    idle_streak = 0
                else:
                    idle_streak += 1
            else:
                idle_streak += 1
            if idle_streak > 64:
                break
        return pcis

    def _resolve_button_cmds(pcis: set[int],
                             current_vts: int) -> None:
        for pci in pcis:
            for cmd in button_graph_walk(pci):
                try:
                    link, _regs = dvd_vm.vm_eval_cmds([cmd])
                except Exception:
                    continue
                resolved = _resolve_link_target(
                    link, current_vts=current_vts, ttsrpt_map=ttsrpt_map,
                )
                if resolved is not None:
                    reach.add((resolved.vts, resolved.pgcn))

    try:
        with ldn.open_disc(disc_path) as nav:
            # Stage 1: Drive First-Play (which auto-runs on open). This
            # gets the VM into the FP_PGC → (typically) VMGM root menu.
            fp_pcis = _drain_pcis(nav, _BLOCK_LIMIT_PER_STAGE)
            _resolve_button_cmds(fp_pcis, current_vts=0)
            # Stage 2: Visit each top-level menu via dvdnav_menu_call.
            for menu in _MENU_IDS:
                try:
                    ok = ldn.menu_call(nav, menu)
                except Exception:
                    ok = False
                if not ok:
                    continue
                menu_pcis = _drain_pcis(nav, _BLOCK_LIMIT_PER_STAGE)
                _resolve_button_cmds(menu_pcis, current_vts=0)
            # Stage 3: Visit each title's VTSM by walking into each VTS
            # via title_play, then menu_call. This catches per-VTS menu
            # buttons whose targets aren't reachable from the root.
            try:
                n_titles = ldn.get_number_of_titles(nav)
            except Exception:
                n_titles = 0
            for tt in range(1, min(n_titles, 99) + 1):
                try:
                    if not ldn.title_play(nav, tt):
                        continue
                except Exception:
                    continue
                # Brief title play to land in VTS, then call VTSM root menu.
                _drain_pcis(nav, 32)  # warmup
                for menu in (3,):  # Root menu within current VTS
                    try:
                        ok = ldn.menu_call(nav, menu)
                    except Exception:
                        ok = False
                    if not ok:
                        continue
                    vts_pcis = _drain_pcis(nav, _BLOCK_LIMIT_PER_STAGE)
                    # Best-effort: assume current VTS from the title number.
                    # ttsrpt_map maps tt → (vts, pgcn).
                    current_vts = ttsrpt_map.get(tt, (0, 0))[0]
                    _resolve_button_cmds(vts_pcis, current_vts=current_vts)
    except Exception as e:
        _log.debug("button-graph walk could not open %s: %s", disc_path, e)
    return reach


def discover_reachable_titles(disc_path: str | Path,
                              titles: list[tuple[int, int]],
                              *,
                              use_global_nav_graph: bool = False,
                              use_button_graph: bool = False,
                              ) -> set[tuple[int, int]]:
    """Run VM-driven title discovery for every (vts, pgcn) in ``titles``.

    Args:
        disc_path: Path to disc (folder, ISO, or device).
        titles: List of (vts_no, pgcn) pairs derived from TT_SRPT.
        use_global_nav_graph: also union with the global nav-graph closure
            (catches titles reachable via menu commands even though their
            own pre-commands jump away). Default False.
        use_button_graph: also union with the PCI button-graph walk
            (catches titles reachable via menu BUTTON commands rather
            than menu PGC commands). Default False — requires opening a
            libdvdnav session and playing through every menu PGC to
            collect PCI button command sets. Port of FUN_00800aa0 +
            FUN_00800cc0 (the missing D4 function per AUDIT.md §1.4).

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
    # Button-graph reachability requires its own dvdnav session
    # (libdvdread alone doesn't expose PCIs without a play context).
    if use_button_graph:
        try:
            button_reach = _walk_button_graph(str(disc_path), ttsrpt_map)
            for (vts, pgcn) in titles:
                if (vts, pgcn) in button_reach:
                    reachable.add((vts, pgcn))
        except Exception as e:
            _log.warning("button-graph walk failed: %s", e)
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
