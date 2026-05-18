"""
DVD cell content validators — Python port of MakeMKV's cell-validation
machinery for trim decisions.

Sources (all in research/full_decomp.md or research/ghidra_output/):
    cell_similarity_compare   FUN_007eb0b0   DVD VM opcode classifier
    cell_validator_primary    FUN_007ea3d0   gateway with threshold checks
    cell_validator_secondary  FUN_007ea5e0   50-iter PRNG-sampling fallback

A cell's content-ness is judged by the side-effect-fulness of its
``cell_cmd`` (the 8-byte DVD VM command in the PGC command_tbl). NOP-like
commands → not content (a candidate for trimming); commands that move
playback or compare/set GPRMs → content.

This module is data-driven and pure-Python; it consumes the libdvdread
PGC + command_tbl + cell_playback structures directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List, Optional


# ---------------------------------------------------------------------------
# DVD VM command opcode classification
# ---------------------------------------------------------------------------
#
# A VM command is 8 bytes. Bytes 0-1 form the "opcode word" (byte 0 = MSB).
# MakeMKV's classifier uses two masks to bucket related opcodes:
#
#     opcode_hi = word & 0xFF0F   — keep byte 0 + low nibble of byte 1
#     opcode_lo = word & 0xF0FF   — keep high nibble of byte 0 + byte 1
#
# The 0x71 bitmap selects offsets {0, 4, 5, 6} from a 0x2001 base, i.e.
# the "structural" subset of the 0x2000 opcode class:
#     0x2001, 0x2005, 0x2006, 0x2007
# Plus 0x3008 (independent SetSPRM-class).

_OPCODE_MASK_HI = 0xFF0F
_OPCODE_MASK_LO = 0xF0FF
_OPCODE_2001_BITMAP = 0x71  # bits 0, 4, 5, 6 → 0x2001/2005/2006/2007


def _cell_command_indicates_content(cmd_bytes: bytes) -> bool:
    """Return True iff the 8-byte DVD VM command performs a non-trivial
    operation (i.e. its host cell should be treated as content-bearing
    rather than a NOP cell that can be trimmed).

    Direct port of ``FUN_007eb0b0`` (``cell_similarity_compare``). The
    decomp comparison-byte names map as:
        byte0/1 = ``uVar5`` (the opcode word, byte0 << 8 | byte1)
        byte3   = ``cVar2`` (compare operand 1, signed)
        byte5   = ``cVar3`` (compare operand 2, signed)
        byte7   = link-flag sub-byte
    """
    if len(cmd_bytes) < 8:
        return False

    b0 = cmd_bytes[0]
    b1 = cmd_bytes[1]
    b3 = cmd_bytes[3]    # signed comparison value
    b4 = cmd_bytes[4]
    b5 = cmd_bytes[5]    # signed comparison value
    b7 = cmd_bytes[7]    # link-target sub-byte

    word = (b0 << 8) | b1
    opcode_hi = word & _OPCODE_MASK_HI
    opcode_lo = word & _OPCODE_MASK_LO

    # NOP fast-paths: certain BranchIf / Compare opcodes with link-flag 0
    # don't actually advance playback.
    if opcode_hi == 0x2001 and b7 == 0:
        return False
    if opcode_lo in (0x6001, 0x7001) and b7 == 0:
        return False

    # Restrict to the "structural" opcode set; anything else is treated
    # as non-content immediately (decomp does ``return false`` here).
    offset = opcode_hi - 0x2001
    in_2001_family = (0 <= offset <= 6) and ((_OPCODE_2001_BITMAP >> offset) & 1)
    in_3008 = (opcode_hi == 0x3008)
    if not (in_2001_family or in_3008):
        # Fall through to the post-classification range check with
        # bVar10=False — but the range check may still return True if
        # opcode_lo is in the 0x6001/0x7001 family. Mirror exactly.
        return _opcode_lo_in_content_range(opcode_lo, default=False)

    # Sub-opcode classification (bVar6 = (byte1 >> 4) & 0x7). Branch on
    # byte 1 high-bit (sign of byte1 as char).
    sub_op = (b1 >> 4) & 0x7
    byte1_msb_set = (b1 & 0x80) != 0

    if not byte1_msb_set:
        # Decomp path: -1 < (char)bVar1
        if sub_op == 0:
            cvar9 = 1
        elif sub_op == 1:
            cvar9 = 0
        elif sub_op in (3, 5, 7):
            cvar9 = (1 if (b5 == b3) else 0) * 2   # 0 or 2
        else:  # 2, 4, 6
            cvar9 = 1 if (b5 == b3) else 0
        bvar10 = (cvar9 != 2)
    else:
        # Decomp else-branch: byte 1 high-bit set.
        if sub_op == 0:
            # goto switchD_007eb19a_caseD_0: bVar10 = (cVar9 != 2)
            # with cVar9 = '\x01' default → bVar10 = True.
            bvar10 = True
        elif sub_op == 1:
            # bVar10 = CONCAT11(byte4, byte5) == 0; then cVar9 = bVar10*2;
            # then bVar10 = (cVar9 != 2). So: final bVar10 =
            # (CONCAT11(byte4, byte5) != 0).
            bvar10 = ((b4 << 8) | b5) != 0
        else:
            # Decomp default: goto switchD_007eb19a_caseD_1; cVar9 = 0;
            # bVar10 = (cVar9 != 2) = True.
            bvar10 = True

    return _opcode_lo_in_content_range(opcode_lo, default=bvar10)


#: opcode_lo values that count as "content range" in the decomp's tail.
#: Derived from the C condition `(uVar5 - 0x6004) > 3 && uVar5 != 0x6001`
#: in unsigned 32-bit arithmetic, which wraps when uVar5 < 0x6004 — the
#: condition only stays False (i.e. fall through to ``return true``) when
#: uVar5 ∈ {0x6001, 0x6004-0x6007}; the 0x7001/7004-7007 branch is the
#: mirror image for uVar5 >= 0x7001.
_CONTENT_OPCODE_LO_SET = frozenset({
    0x6001, 0x6004, 0x6005, 0x6006, 0x6007,
    0x7001, 0x7004, 0x7005, 0x7006, 0x7007,
})


def _opcode_lo_in_content_range(opcode_lo: int, *, default: bool) -> bool:
    """Final opcode_lo range filter (decomp's tail). The C code uses
    unsigned wrap-around arithmetic; in Python we just check set
    membership against the equivalent enumerated values. Returns
    ``default`` outside the set, ``True`` inside it.
    """
    if opcode_lo in _CONTENT_OPCODE_LO_SET:
        return True
    return default


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def cell_command_at(pgc, cell_cmd_nr: int) -> Optional[bytes]:
    """Read the 8-byte VM command at ``cell_cmd_nr`` (1-based) from a
    libdvdread Pgc structure. Returns None if ``cell_cmd_nr`` is 0 or
    out of range. The bytes are returned in original DVD-Video order
    (byte 0 = MSB of the opcode word)."""
    if cell_cmd_nr == 0:
        return None
    if not pgc.command_tbl:
        return None
    tbl = pgc.command_tbl.contents
    if cell_cmd_nr > tbl.nr_of_cell:
        return None
    if not tbl.cell_cmds:
        return None
    # libdvdread already split the command_tbl into pre/post/cell arrays.
    cmd = tbl.cell_cmds[cell_cmd_nr - 1]
    return bytes(cmd.bytes)


def cell_has_content_command(pgc, cell_cmd_nr: int) -> bool:
    """High-level wrapper: is the cell at ``cell_cmd_nr`` content-bearing
    according to its VM command? Returns False for ``cell_cmd_nr == 0``
    (which means "no command attached"; the cell may still be content
    but this function only judges by the command).
    """
    cmd = cell_command_at(pgc, cell_cmd_nr)
    if cmd is None:
        return False
    return _cell_command_indicates_content(cmd)


# ---------------------------------------------------------------------------
# Duration thresholds (port of cell_validator_primary's 0x101/0x501 logic)
# ---------------------------------------------------------------------------
#
# MakeMKV's cell_validator_primary reads the BCD playback_time as a u32
# (big-endian on disc → high byte = hour, low byte = frame_u) and compares
# the duration-with-rate-code-masked-off against two thresholds:
#
#   (time & 0xFFFFFF00) < 0x101   ≡  duration < 1 second
#   (time & 0xFFFFFF00) < 0x501   ≡  duration < 5 seconds
#
# These are the breakpoints between three content-classification regimes:
#   < 1 sec  → "trim candidate" (probably an ident frame or blank cell)
#   1-5 sec  → "ambiguous" (check the 0x0c angle-block flag to decide)
#   ≥ 5 sec  → "main content path" (cell is real material)
#
# The decomp uses these via raw BCD bytes for fast comparisons; we expose
# them as float seconds since our CellMeta already decodes BCD.

#: Duration in seconds below which a cell is presumed non-content (was
#: < 0x101 in MakeMKV's BCD-byte representation).
CELL_DURATION_TRIVIAL_S: float = 1.0

#: Duration in seconds below which a cell needs the 0x0c angle-block
#: flag to be treated as content. Above this we always evaluate via
#: the full validator path. Was < 0x501 in MakeMKV.
CELL_DURATION_AMBIGUOUS_S: float = 5.0


__all__ = [
    "cell_command_at",
    "cell_has_content_command",
    "CELL_DURATION_TRIVIAL_S",
    "CELL_DURATION_AMBIGUOUS_S",
]
