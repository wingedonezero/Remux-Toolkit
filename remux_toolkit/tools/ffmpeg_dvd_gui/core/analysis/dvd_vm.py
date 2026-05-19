"""
DVD-Video Virtual Machine — pure-Python port of libdvdnav's VM decoder.

This is the bytecode interpreter that executes the nav commands stored in each
PGC (pre / post / cell command tables). For our DVD ripper, we use it to
determine **title reachability** the same way MakeMKV does:

  1. For each TT_SRPT title, load the title's pre-commands.
  2. Execute the pre-commands with the standard DVD initial register state.
  3. Inspect the resulting :class:`Link` action:
       - ``LinkPGCN`` with a PGCN that doesn't exist in the VTS → title is
         a fake/orphan authoring entry (MakeMKV silently drops these).
       - ``Exit`` immediately → title stops without ever playing → fake.
       - ``LinkNoLink`` or no link → pre-commands didn't reroute → title is
         reachable from the disc's natural nav graph.

This mirrors MakeMKV's FUN_007ff000 VM-walker logic. Standard libdvdnav
includes the same VM (in ``mmgpl/dvdnav/vm/decoder.c``), and MakeMKV uses
it verbatim — we port it byte-for-byte so the behaviour matches.

Source: ``/home/chaoz/Downloads/makemkv-oss-1.18.3/mmgpl/dvdnav/vm/decoder.c``.
"""

from __future__ import annotations

import enum
import random as _random
from dataclasses import dataclass, field
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Link / jump command types (from decoder.h)
# ---------------------------------------------------------------------------

class LinkCmd(enum.IntEnum):
    """DVD-VM link / jump command identifiers. Values match decoder.h.

    These are produced by :func:`vm_eval_cmds` to describe the navigation
    transition the VM would take. Our title-reachability logic inspects the
    Link to decide whether the title is "real" or an authoring stub.
    """
    LinkNoLink = 0
    LinkTopC = 1
    LinkNextC = 2
    LinkPrevC = 3
    LinkTopPG = 5
    LinkNextPG = 6
    LinkPrevPG = 7
    LinkTopPGC = 9
    LinkNextPGC = 10
    LinkPrevPGC = 11
    LinkGoUpPGC = 12
    LinkTailPGC = 13
    LinkRSM = 16
    LinkPGCN = 17
    LinkPTTN = 18
    LinkPGN = 19
    LinkCN = 20
    Exit = 21
    JumpTT = 22
    JumpVTS_TT = 23
    JumpVTS_PTT = 24
    JumpSS_FP = 25
    JumpSS_VMGM_MENU = 26
    JumpSS_VTSM = 27
    JumpSS_VMGM_PGC = 28
    CallSS_FP = 29
    CallSS_VMGM_MENU = 30
    CallSS_VTSM = 31
    CallSS_VMGM_PGC = 32
    PlayThis = 33


@dataclass
class Link:
    """A link/jump action result, with up to three data fields.

    Mirrors libdvdnav's ``link_t`` struct. Field semantics depend on
    :attr:`command`; see :func:`vm_eval_cmds` callers in MakeMKV's
    ``mmgpl/dvdnav/vm/play.c`` for usage patterns.
    """
    command: LinkCmd = LinkCmd.LinkNoLink
    data1: int = 0
    data2: int = 0
    data3: int = 0


# ---------------------------------------------------------------------------
# VM registers (SPRM 0..23, GPRM 0..15 + GPRM counter-mode bookkeeping)
# ---------------------------------------------------------------------------

@dataclass
class Registers:
    """The DVD-VM register set.

    DVD-Video defines 24 SPRMs (System Parameter Registers) and 16 GPRMs
    (General Parameter Registers). The SPRMs control playback state; the
    GPRMs are scratch storage for the disc's nav commands. The GPRM
    ``mode`` flag word turns a register into a "counter" that decrements
    once per second — handled here via :attr:`GPRM_time`.
    """
    SPRM: list[int] = field(default_factory=lambda: [0] * 24)
    GPRM: list[int] = field(default_factory=lambda: [0] * 16)
    GPRM_mode: int = 0  # bit i: register i is a counter
    GPRM_time: list[int] = field(default_factory=lambda: [0] * 16)
    SPRM_flags: int = 0
    time_counter: int = 0

    @staticmethod
    def default(region: int = 0xff) -> "Registers":
        """Initial register state for a fresh DVD player.

        Values follow libdvdnav's vm.c default initialisation. Region defaults
        to "all regions" (0xff) so RCE checks pass.
        """
        r = Registers()
        # SPRM[0] = menu language ('en' = 0x656e in DVD spec byte order)
        r.SPRM[0] = 0x656e
        # SPRM[1] = audio stream (0xf = "none chosen yet")
        r.SPRM[1] = 0xf
        # SPRM[2] = subtitle stream (0x3f = bit 6 set = disabled, low bits = stream 63)
        r.SPRM[2] = 0x3e | 0x40  # 0x7e
        # SPRM[3] = angle (1-based, 1 = first angle)
        r.SPRM[3] = 1
        # SPRM[4] = title number (1-based; 0 = first-play)
        r.SPRM[4] = 1
        # SPRM[5] = VTS_TT
        r.SPRM[5] = 1
        # SPRM[6] = current PGCN
        r.SPRM[6] = 0
        # SPRM[7] = current PTT/chapter
        r.SPRM[7] = 0
        # SPRM[8] = highlighted button (high 6 bits, shifted left 10)
        r.SPRM[8] = 0
        # SPRM[9] = nav timer
        r.SPRM[9] = 0
        # SPRM[10] = PGC of nav timer
        r.SPRM[10] = 0
        # SPRM[11] = karaoke mode
        r.SPRM[11] = 0
        # SPRM[12] = parental management country (low byte = 'us')
        r.SPRM[12] = 0xffff  # "not specified"
        # SPRM[13] = parental level (highest = 15 = least restrictive)
        r.SPRM[13] = 0xf
        # SPRM[14] = video config (16:9 wide, NTSC)
        r.SPRM[14] = 0x100
        # SPRM[15] = initial audio extension
        r.SPRM[15] = 0
        # SPRM[16] = initial audio language ('en')
        r.SPRM[16] = 0x656e
        # SPRM[17] = initial audio language extension
        r.SPRM[17] = 0
        # SPRM[18] = initial subpicture language ('en')
        r.SPRM[18] = 0x656e
        # SPRM[19] = initial subpicture language extension
        r.SPRM[19] = 0
        # SPRM[20] = player region — bit per region (1..8). 0xff = all.
        r.SPRM[20] = region
        # SPRM[21..23] = reserved
        r.SPRM[21] = 0
        r.SPRM[22] = 0
        r.SPRM[23] = 0
        return r


# ---------------------------------------------------------------------------
# command_t equivalent (8-byte instruction + examined-bits tracking)
# ---------------------------------------------------------------------------

class _Command:
    """Per-evaluation state — wraps the 8-byte instruction + register set."""
    __slots__ = ("instruction", "examined", "registers")

    def __init__(self, instruction: int, registers: Registers):
        self.instruction = instruction & 0xffffffffffffffff
        self.examined = 0
        self.registers = registers


def _vm_getbits(cmd: _Command, start: int, count: int) -> int:
    """Extract a bit-range from the 64-bit instruction.

    ``start`` is 0-based from the MSB (so start=63 is the highest bit, start=0
    is the lowest). Direct port of libdvdnav's vm_getbits.
    """
    if count == 0:
        return 0
    if not (-1 <= (start - count) and count <= 32 and 0 <= start <= 63 and count >= 0):
        raise ValueError(f"vm_getbits: bad params start={start} count={count}")
    bit_mask = (1 << 64) - 1
    bit_mask >>= 63 - start
    bits = start + 1 - count
    examining = ((bit_mask >> bits) << bits) & ((1 << 64) - 1)
    cmd.examined |= examining
    return ((cmd.instruction & bit_mask) >> bits) & 0xffffffff


# ---------------------------------------------------------------------------
# Register get/set (with GPRM counter mode)
# ---------------------------------------------------------------------------

def _get_GPRM(regs: Registers, reg: int) -> int:
    reg &= 0xf
    if (regs.GPRM_mode >> reg) & 1:
        # Counter mode
        result = ((regs.time_counter - regs.GPRM_time[reg]) * 32) // 5625
        regs.GPRM[reg] = result & 0xffff
        return regs.GPRM[reg]
    return regs.GPRM[reg] & 0xffff


def _set_GPRM(regs: Registers, reg: int, value: int) -> None:
    reg &= 0xf
    if (regs.GPRM_mode >> reg) & 1:
        regs.GPRM_time[reg] = regs.time_counter
    regs.GPRM[reg] = value & 0xffff


def _eval_reg(cmd: _Command, reg: int) -> int:
    """Read a register specified by an 8-bit code.

    If bit 7 is set, the lower 5 bits index SPRM (else GPRM).
    """
    if reg & 0x80:
        reg &= 0x1f
        cmd.registers.SPRM_flags |= (1 << reg)
        if reg >= 24:
            return 0
        return cmd.registers.SPRM[reg] & 0xffff
    return _get_GPRM(cmd.registers, reg & 0x0f)


def _eval_reg_or_data(cmd: _Command, imm: int, start: int) -> int:
    """If imm: read 16 bits as immediate. Else: read 8 bits, eval as register."""
    if imm:
        return _vm_getbits(cmd, start, 16)
    return _eval_reg(cmd, _vm_getbits(cmd, start - 8, 8))


def _eval_reg_or_data_2(cmd: _Command, imm: int, start: int) -> int:
    """If imm: read 7 bits as immediate. Else: read 4 bits, eval as GPRM."""
    if imm:
        return _vm_getbits(cmd, start - 1, 7)
    return _get_GPRM(cmd.registers, _vm_getbits(cmd, start - 4, 4))


# ---------------------------------------------------------------------------
# Comparison + sub-evaluators (eval_if_version_*, etc.)
# ---------------------------------------------------------------------------

def _eval_compare(op: int, data1: int, data2: int) -> int:
    """Comparison operations 1..7 (1=AND, 2..7 = == != >= > <= <)."""
    if op == 1:
        return data1 & data2
    if op == 2:
        return int(data1 == data2)
    if op == 3:
        return int(data1 != data2)
    if op == 4:
        return int(data1 >= data2)
    if op == 5:
        return int(data1 > data2)
    if op == 6:
        return int(data1 <= data2)
    if op == 7:
        return int(data1 < data2)
    return 0


def _eval_if_version_1(cmd: _Command) -> int:
    """Comparison data in byte 3 and 4-5 (immediate or register)."""
    op = _vm_getbits(cmd, 54, 3)
    if op:
        return _eval_compare(op,
                             _eval_reg(cmd, _vm_getbits(cmd, 39, 8)),
                             _eval_reg_or_data(cmd, _vm_getbits(cmd, 55, 1), 31))
    return 1


def _eval_if_version_2(cmd: _Command) -> int:
    """Compares two registers in byte 6 and 7."""
    op = _vm_getbits(cmd, 54, 3)
    if op:
        return _eval_compare(op,
                             _eval_reg(cmd, _vm_getbits(cmd, 15, 8)),
                             _eval_reg(cmd, _vm_getbits(cmd, 7, 8)))
    return 1


def _eval_if_version_3(cmd: _Command) -> int:
    """Comparison data in byte 2 and 6-7 (immediate or register)."""
    op = _vm_getbits(cmd, 54, 3)
    if op:
        return _eval_compare(op,
                             _eval_reg(cmd, _vm_getbits(cmd, 47, 8)),
                             _eval_reg_or_data(cmd, _vm_getbits(cmd, 55, 1), 15))
    return 1


def _eval_if_version_4(cmd: _Command) -> int:
    """Comparison data in byte 1 and 4-5 (immediate or register)."""
    op = _vm_getbits(cmd, 54, 3)
    if op:
        return _eval_compare(op,
                             _eval_reg(cmd, _vm_getbits(cmd, 51, 4)),
                             _eval_reg_or_data(cmd, _vm_getbits(cmd, 55, 1), 31))
    return 1


# ---------------------------------------------------------------------------
# Special-instruction evaluator (NOP, Goto, Break, SetTmpPML)
# ---------------------------------------------------------------------------

def _eval_special_instruction(cmd: _Command, cond: int) -> int:
    """Returns: 0 if no goto, line number if goto, 256 if break.

    Direct port from decoder.c.
    """
    sub = _vm_getbits(cmd, 51, 4)
    if sub == 0:  # NOP
        return 0
    if sub == 1:  # Goto line
        line = _vm_getbits(cmd, 7, 8)
        return line if cond else 0
    if sub == 2:  # Break
        return 256 if cond else 0
    if sub == 3:  # Set temporary parental level + goto
        line = _vm_getbits(cmd, 7, 8)
        level = _vm_getbits(cmd, 11, 4)
        if cond:
            cmd.registers.SPRM[13] = level
        return line if cond else 0
    return 0


# ---------------------------------------------------------------------------
# Link / jump evaluators
# ---------------------------------------------------------------------------

def _eval_link_subins(cmd: _Command, cond: int, out: Link) -> int:
    """Link by sub-instruction. ``linkop`` is in low 5 bits of the link header."""
    button = _vm_getbits(cmd, 15, 6)
    linkop = _vm_getbits(cmd, 4, 5)
    if linkop > 0x10:
        return 0  # unknown
    out.command = LinkCmd(linkop)
    out.data1 = button
    return cond


def _eval_link_instruction(cmd: _Command, cond: int, out: Link) -> int:
    """Top-level link instruction dispatcher."""
    op = _vm_getbits(cmd, 51, 4)
    if op == 1:
        return _eval_link_subins(cmd, cond, out)
    if op == 4:
        out.command = LinkCmd.LinkPGCN
        out.data1 = _vm_getbits(cmd, 14, 15)
        return cond
    if op == 5:
        out.command = LinkCmd.LinkPTTN
        out.data1 = _vm_getbits(cmd, 9, 10)
        out.data2 = _vm_getbits(cmd, 15, 6)
        return cond
    if op == 6:
        out.command = LinkCmd.LinkPGN
        out.data1 = _vm_getbits(cmd, 6, 7)
        out.data2 = _vm_getbits(cmd, 15, 6)
        return cond
    if op == 7:
        out.command = LinkCmd.LinkCN
        out.data1 = _vm_getbits(cmd, 7, 8)
        out.data2 = _vm_getbits(cmd, 15, 6)
        return cond
    return 0


def _eval_jump_instruction(cmd: _Command, cond: int, out: Link) -> int:
    """Jump / Call instruction dispatcher."""
    op = _vm_getbits(cmd, 51, 4)
    if op == 1:
        out.command = LinkCmd.Exit
        return cond
    if op == 2:
        out.command = LinkCmd.JumpTT
        out.data1 = _vm_getbits(cmd, 22, 7)
        return cond
    if op == 3:
        out.command = LinkCmd.JumpVTS_TT
        out.data1 = _vm_getbits(cmd, 22, 7)
        return cond
    if op == 5:
        out.command = LinkCmd.JumpVTS_PTT
        out.data1 = _vm_getbits(cmd, 22, 7)
        out.data2 = _vm_getbits(cmd, 41, 10)
        return cond
    if op == 6:
        sub = _vm_getbits(cmd, 23, 2)
        if sub == 0:
            out.command = LinkCmd.JumpSS_FP
            return cond
        if sub == 1:
            out.command = LinkCmd.JumpSS_VMGM_MENU
            out.data1 = _vm_getbits(cmd, 19, 4)
            return cond
        if sub == 2:
            out.command = LinkCmd.JumpSS_VTSM
            out.data1 = _vm_getbits(cmd, 31, 8)
            out.data2 = _vm_getbits(cmd, 39, 8)
            out.data3 = _vm_getbits(cmd, 19, 4)
            return cond
        if sub == 3:
            out.command = LinkCmd.JumpSS_VMGM_PGC
            out.data1 = _vm_getbits(cmd, 46, 15)
            return cond
    elif op == 8:
        sub = _vm_getbits(cmd, 23, 2)
        if sub == 0:
            out.command = LinkCmd.CallSS_FP
            out.data1 = _vm_getbits(cmd, 31, 8)
            return cond
        if sub == 1:
            out.command = LinkCmd.CallSS_VMGM_MENU
            out.data1 = _vm_getbits(cmd, 19, 4)
            out.data2 = _vm_getbits(cmd, 31, 8)
            return cond
        if sub == 2:
            out.command = LinkCmd.CallSS_VTSM
            out.data1 = _vm_getbits(cmd, 19, 4)
            out.data2 = _vm_getbits(cmd, 31, 8)
            return cond
        if sub == 3:
            out.command = LinkCmd.CallSS_VMGM_PGC
            out.data1 = _vm_getbits(cmd, 46, 15)
            out.data2 = _vm_getbits(cmd, 31, 8)
            return cond
    return 0


def _eval_system_set(cmd: _Command, cond: int, out: Link) -> int:
    """System-register set + optional link."""
    sub = _vm_getbits(cmd, 59, 4)
    if sub == 1:
        # Set SPRM 1 / 2 / 3 (audio / sub / angle)
        for i in (1, 2, 3):
            if _vm_getbits(cmd, 63 - ((2 + i) * 8), 1):
                data = _eval_reg_or_data_2(cmd, _vm_getbits(cmd, 60, 1), 47 - (i * 8))
                if cond:
                    cmd.registers.SPRM[i] = data & 0xffff
    elif sub == 2:
        # Set SPRM 9/10 (nav timer + pgcn)
        data = _eval_reg_or_data(cmd, _vm_getbits(cmd, 60, 1), 47)
        data2 = _vm_getbits(cmd, 23, 8)
        if cond:
            cmd.registers.SPRM[9] = data & 0xffff
            cmd.registers.SPRM[10] = data2 & 0xffff
    elif sub == 3:
        # GPRM mode: counter vs register
        data = _eval_reg_or_data(cmd, _vm_getbits(cmd, 60, 1), 47)
        data2 = _vm_getbits(cmd, 19, 4)
        if _vm_getbits(cmd, 23, 1):
            cmd.registers.GPRM_mode |= (1 << data2)
        else:
            cmd.registers.GPRM_mode &= ~(1 << data2) & 0xffff
        if cond:
            _set_GPRM(cmd.registers, data2, data)
    elif sub == 6:
        # Set SPRM 8 (highlight button)
        data = _eval_reg_or_data(cmd, _vm_getbits(cmd, 60, 1), 31)
        if cond:
            cmd.registers.SPRM[8] = data & 0xfc00
    # Optional link
    if _vm_getbits(cmd, 51, 4):
        return _eval_link_instruction(cmd, cond, out)
    return 0


def _eval_set_op(cmd: _Command, op: int, reg: int, reg2: int, data: int) -> None:
    """GPRM arithmetic / logical operation."""
    SHORTMAX = 0xffff
    if op == 1:
        _set_GPRM(cmd.registers, reg, data)
    elif op == 2:  # Swap
        _set_GPRM(cmd.registers, reg2, _get_GPRM(cmd.registers, reg))
        _set_GPRM(cmd.registers, reg, data)
    elif op == 3:
        tmp = _get_GPRM(cmd.registers, reg) + data
        if tmp > SHORTMAX:
            tmp = SHORTMAX
        _set_GPRM(cmd.registers, reg, tmp)
    elif op == 4:
        tmp = _get_GPRM(cmd.registers, reg) - data
        if tmp < 0:
            tmp = 0
        _set_GPRM(cmd.registers, reg, tmp)
    elif op == 5:
        tmp = _get_GPRM(cmd.registers, reg) * data
        if tmp > SHORTMAX:
            tmp = SHORTMAX
        _set_GPRM(cmd.registers, reg, tmp)
    elif op == 6:
        if data != 0:
            _set_GPRM(cmd.registers, reg, _get_GPRM(cmd.registers, reg) // data)
        else:
            _set_GPRM(cmd.registers, reg, 0xffff)
    elif op == 7:
        if data != 0:
            _set_GPRM(cmd.registers, reg, _get_GPRM(cmd.registers, reg) % data)
        else:
            _set_GPRM(cmd.registers, reg, 0xffff)
    elif op == 8:  # Random
        _set_GPRM(cmd.registers, reg, 1 + _random.randint(0, max(0, data - 1)))
    elif op == 9:
        _set_GPRM(cmd.registers, reg, _get_GPRM(cmd.registers, reg) & data)
    elif op == 10:
        _set_GPRM(cmd.registers, reg, _get_GPRM(cmd.registers, reg) | data)
    elif op == 11:
        _set_GPRM(cmd.registers, reg, _get_GPRM(cmd.registers, reg) ^ data)


def _eval_set_version_1(cmd: _Command, cond: int) -> None:
    op = _vm_getbits(cmd, 59, 4)
    reg = _vm_getbits(cmd, 35, 4)
    reg2 = _vm_getbits(cmd, 19, 4)
    data = _eval_reg_or_data(cmd, _vm_getbits(cmd, 60, 1), 31)
    if cond:
        _eval_set_op(cmd, op, reg, reg2, data)


def _eval_set_version_2(cmd: _Command, cond: int) -> None:
    op = _vm_getbits(cmd, 59, 4)
    reg = _vm_getbits(cmd, 51, 4)
    reg2 = _vm_getbits(cmd, 35, 4)
    data = _eval_reg_or_data(cmd, _vm_getbits(cmd, 60, 1), 47)
    if cond:
        _eval_set_op(cmd, op, reg, reg2, data)


# ---------------------------------------------------------------------------
# Per-command evaluator
# ---------------------------------------------------------------------------

def _eval_command(bytes8: bytes, regs: Registers, out: Link) -> int:
    """Evaluate one 8-byte command.

    Returns:
        0 if no goto (continue to next line);
        N (>0) if goto to line N;
        -1 if a link/jump fires (link stored in ``out``).
    """
    instruction = 0
    for b in bytes8:
        instruction = (instruction << 8) | (b & 0xff)
    cmd = _Command(instruction, regs)
    out.command = LinkCmd.LinkNoLink
    out.data1 = 0
    out.data2 = 0
    out.data3 = 0

    ty = _vm_getbits(cmd, 63, 3)
    res = 0
    if ty == 0:  # Special instructions
        cond = _eval_if_version_1(cmd)
        res = _eval_special_instruction(cmd, cond)
    elif ty == 1:  # Link / jump
        if _vm_getbits(cmd, 60, 1):
            cond = _eval_if_version_2(cmd)
            res = _eval_jump_instruction(cmd, cond, out)
        else:
            cond = _eval_if_version_1(cmd)
            res = _eval_link_instruction(cmd, cond, out)
        if res:
            res = -1
    elif ty == 2:  # System set
        cond = _eval_if_version_2(cmd)
        res = _eval_system_set(cmd, cond, out)
        if res:
            res = -1
    elif ty == 3:  # Set + Compare / Link
        cond = _eval_if_version_3(cmd)
        _eval_set_version_1(cmd, cond)
        if _vm_getbits(cmd, 51, 4):
            res = _eval_link_instruction(cmd, cond, out)
        if res:
            res = -1
    elif ty == 4:  # Set + Compare -> Link Sub
        _eval_set_version_2(cmd, 1)
        cond = _eval_if_version_4(cmd)
        res = _eval_link_subins(cmd, cond, out)
        if res:
            res = -1
    elif ty == 5:  # Compare -> (Set and Link Sub)
        cond = _eval_if_version_4(cmd)
        _eval_set_version_2(cmd, cond)
        res = _eval_link_subins(cmd, cond, out)
        if res:
            res = -1
    elif ty == 6:  # Compare -> Set, always Link Sub
        cond = _eval_if_version_4(cmd)
        _eval_set_version_2(cmd, cond)
        res = _eval_link_subins(cmd, 1, out)
        if res:
            res = -1
    return res


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def vm_eval_cmds(commands: Iterable[bytes], registers: Optional[Registers] = None,
                 *, max_iterations: int = 100_000) -> tuple[Link, Registers]:
    """Execute a sequence of DVD-VM commands; return ``(link, registers)``.

    ``commands`` is an iterable of 8-byte command blobs (one ``vm_cmd_t`` each).
    ``registers`` is optional — defaults to standard player initial state.

    The returned :class:`Link` describes the navigation transition the VM
    would take after executing the commands:

    - ``LinkNoLink``: no transition; control falls through to default playback.
    - ``LinkPGCN`` with ``data1 = N``: jump to PGC N in the current VTS.
    - ``Exit``: stop playback.
    - ``JumpTT`` with ``data1 = N``: jump to global TT_SRPT title N.
    - etc.

    This is a direct port of ``vmEval_CMD`` from libdvdnav's decoder.c — the
    same code MakeMKV's mmgpl variant uses verbatim.

    Args:
        commands: Iterable of 8-byte commands.
        registers: Initial register state (defaults to player init).
        max_iterations: Safety cap to prevent infinite loops on malicious discs.

    Returns:
        Tuple of (Link result, final Registers state).
    """
    cmds = list(commands)
    if registers is None:
        registers = Registers.default()
    out = Link()

    i = 0
    total = 0
    n = len(cmds)
    while i < n and total < max_iterations:
        bytes8 = cmds[i]
        if len(bytes8) != 8:
            raise ValueError(f"command {i} must be 8 bytes, got {len(bytes8)}")
        line = _eval_command(bytes8, registers, out)
        if line < 0:
            # Link command — terminate
            return (out, registers)
        if line > 0:
            i = line - 1  # 1-based line numbers
        else:
            i += 1
        total += 1

    # Reached end of commands without a link — return "no link" (default fallthrough)
    out.command = LinkCmd.LinkNoLink
    return (out, registers)


__all__ = [
    "Link",
    "LinkCmd",
    "Registers",
    "vm_eval_cmds",
]
