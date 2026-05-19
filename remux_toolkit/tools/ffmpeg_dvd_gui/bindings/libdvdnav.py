"""
ctypes binding for libdvdnav.so.4.

We use libdvdnav purely for **VM-driven title discovery** — the same mechanism
MakeMKV uses to decide which TT_SRPT entries are reachable from the DVD's
navigation graph (and therefore "real" titles) versus authoring stubs that
silently get dropped. See research/ghidra_output/dvd_full/coverage/ for the
decomp-derived rationale.

Tested against libdvdnav 6.1.1 (libdvdnav.so.4.3.0). Mirrors
``/usr/include/dvdnav/dvdnav.h``.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from ctypes import (
    CDLL, POINTER, Structure,
    c_char_p, c_int32, c_int64, c_uint8, c_uint16, c_uint32, c_uint64, c_void_p,
)
from contextlib import contextmanager
from typing import Iterator, Optional


_LIB_NAME = ctypes.util.find_library("dvdnav") or "libdvdnav.so.4"
_lib = CDLL(_LIB_NAME)


# ---------------------------------------------------------------------------
# Opaque handles + constants
# ---------------------------------------------------------------------------

class _DvdnavT(Structure):
    pass


DvdnavP = POINTER(_DvdnavT)

# Return codes
DVDNAV_STATUS_ERR = 0
DVDNAV_STATUS_OK = 1

# Block events from dvdnav_next_block (we only need a few)
DVDNAV_BLOCK_OK = 0
DVDNAV_NOP = 1
DVDNAV_STILL_FRAME = 2
DVDNAV_SPU_STREAM_CHANGE = 3
DVDNAV_AUDIO_STREAM_CHANGE = 4
DVDNAV_VTS_CHANGE = 5
DVDNAV_CELL_CHANGE = 6
DVDNAV_NAV_PACKET = 7
DVDNAV_STOP = 8
DVDNAV_HIGHLIGHT = 9
DVDNAV_SPU_CLUT_CHANGE = 10
DVDNAV_HOP_CHANNEL = 12
DVDNAV_WAIT = 13


# ---------------------------------------------------------------------------
# Function prototypes
# ---------------------------------------------------------------------------

# init / cleanup
_lib.dvdnav_open.argtypes = [POINTER(DvdnavP), c_char_p]
_lib.dvdnav_open.restype = c_int32

_lib.dvdnav_close.argtypes = [DvdnavP]
_lib.dvdnav_close.restype = c_int32

_lib.dvdnav_reset.argtypes = [DvdnavP]
_lib.dvdnav_reset.restype = c_int32

_lib.dvdnav_err_to_string.argtypes = [DvdnavP]
_lib.dvdnav_err_to_string.restype = c_char_p

_lib.dvdnav_version.argtypes = []
_lib.dvdnav_version.restype = c_char_p

# config / characteristics
_lib.dvdnav_set_readahead_flag.argtypes = [DvdnavP, c_int32]
_lib.dvdnav_set_readahead_flag.restype = c_int32

_lib.dvdnav_set_PGC_positioning_flag.argtypes = [DvdnavP, c_int32]
_lib.dvdnav_set_PGC_positioning_flag.restype = c_int32

_lib.dvdnav_set_region_mask.argtypes = [DvdnavP, c_int32]
_lib.dvdnav_set_region_mask.restype = c_int32

# titles / chapters / VTS-level queries
_lib.dvdnav_get_number_of_titles.argtypes = [DvdnavP, POINTER(c_int32)]
_lib.dvdnav_get_number_of_titles.restype = c_int32

_lib.dvdnav_get_number_of_parts.argtypes = [DvdnavP, c_int32, POINTER(c_int32)]
_lib.dvdnav_get_number_of_parts.restype = c_int32

# dvdnav_describe_title_chapters returns the times array via malloc; caller frees.
_lib.dvdnav_describe_title_chapters.argtypes = [
    DvdnavP, c_int32, POINTER(POINTER(c_uint64)), POINTER(c_uint64),
]
_lib.dvdnav_describe_title_chapters.restype = c_uint32  # returns chapter count

# Playback control
_lib.dvdnav_title_play.argtypes = [DvdnavP, c_int32]
_lib.dvdnav_title_play.restype = c_int32

_lib.dvdnav_part_play.argtypes = [DvdnavP, c_int32, c_int32]
_lib.dvdnav_part_play.restype = c_int32

_lib.dvdnav_program_play.argtypes = [DvdnavP, c_int32, c_int32, c_int32]
_lib.dvdnav_program_play.restype = c_int32

_lib.dvdnav_stop.argtypes = [DvdnavP]
_lib.dvdnav_stop.restype = c_int32

# Current state inspection (the part we mostly use to determine reachability)
_lib.dvdnav_current_title_info.argtypes = [DvdnavP, POINTER(c_int32), POINTER(c_int32)]
_lib.dvdnav_current_title_info.restype = c_int32

_lib.dvdnav_current_title_program.argtypes = [
    DvdnavP, POINTER(c_int32), POINTER(c_int32), POINTER(c_int32),
]
_lib.dvdnav_current_title_program.restype = c_int32

_lib.dvdnav_get_position.argtypes = [DvdnavP, POINTER(c_uint32), POINTER(c_uint32)]
_lib.dvdnav_get_position.restype = c_int32

# Stream-attr queries (used to confirm a title's stream layout)
_lib.dvdnav_get_audio_logical_stream.argtypes = [DvdnavP, c_uint8]
_lib.dvdnav_get_audio_logical_stream.restype = c_int32

_lib.dvdnav_get_spu_logical_stream.argtypes = [DvdnavP, c_uint8]
_lib.dvdnav_get_spu_logical_stream.restype = c_int32

# VM step — drives the navigation state machine, returns DVDNAV_* event codes.
# This is the only way to drive the actual nav-graph traversal (the high-level
# title_play / program_play APIs are direct-jumps that don't reflect reachability).
DVD_VIDEO_LB_LEN = 2048

_lib.dvdnav_get_next_block.argtypes = [
    DvdnavP, ctypes.POINTER(c_uint8), POINTER(c_int32), POINTER(c_int32),
]
_lib.dvdnav_get_next_block.restype = c_int32

# DVDNAV_STILL_FRAME / DVDNAV_WAIT events block forever unless skipped
_lib.dvdnav_still_skip.argtypes = [DvdnavP]
_lib.dvdnav_still_skip.restype = c_int32

_lib.dvdnav_wait_skip.argtypes = [DvdnavP]
_lib.dvdnav_wait_skip.restype = c_int32

# Allows skipping past an interactive menu (button presses we don't have)
_lib.dvdnav_menu_call.argtypes = [DvdnavP, c_int32]
_lib.dvdnav_menu_call.restype = c_int32

# Sector-search: jump-by-time within current PGC (helps escape long menus)
_lib.dvdnav_sector_search.argtypes = [DvdnavP, c_int64, c_int32]
_lib.dvdnav_sector_search.restype = c_int32

# free for the chapter-times pointer (libdvdnav-allocated buffer)
_libc = CDLL(ctypes.util.find_library("c") or "libc.so.6")
_libc.free.argtypes = [c_void_p]
_libc.free.restype = None


# ---------------------------------------------------------------------------
# Pythonic API
# ---------------------------------------------------------------------------

class DvdnavError(RuntimeError):
    """Raised when libdvdnav returns an error status."""


def version() -> str:
    """Return the libdvdnav version string."""
    raw = _lib.dvdnav_version()
    return raw.decode("utf-8", "replace") if raw else ""


def _err_str(handle: DvdnavP) -> str:
    """Read libdvdnav's last-error message."""
    raw = _lib.dvdnav_err_to_string(handle)
    return raw.decode("utf-8", "replace") if raw else ""


@contextmanager
def open_disc(path: str) -> Iterator[DvdnavP]:
    """Open the DVD at ``path`` and yield a dvdnav handle.

    The handle is closed when the context exits (even on exception).

    Raises:
        DvdnavError: if dvdnav_open returns DVDNAV_STATUS_ERR.
    """
    handle = DvdnavP()
    rc = _lib.dvdnav_open(ctypes.byref(handle), path.encode("utf-8"))
    if rc != DVDNAV_STATUS_OK or not handle:
        raise DvdnavError(f"dvdnav_open failed for {path!r}")
    # Sane defaults for VM-discovery: read-ahead off, PGC-positioning on
    _lib.dvdnav_set_readahead_flag(handle, 0)
    _lib.dvdnav_set_PGC_positioning_flag(handle, 1)
    _lib.dvdnav_set_region_mask(handle, 0xff)  # accept all regions
    try:
        yield handle
    finally:
        _lib.dvdnav_close(handle)


def get_number_of_titles(handle: DvdnavP) -> int:
    """Count of TT_SRPT entries discoverable on the disc."""
    out = c_int32(0)
    rc = _lib.dvdnav_get_number_of_titles(handle, ctypes.byref(out))
    if rc != DVDNAV_STATUS_OK:
        raise DvdnavError(f"dvdnav_get_number_of_titles failed: {_err_str(handle)}")
    return int(out.value)


def get_number_of_parts(handle: DvdnavP, title_num: int) -> int:
    """Number of PTTs (chapters) declared for the given TT_SRPT title (1-based).

    Returns 0 on dvdnav error (libdvdnav reports DVDNAV_STATUS_ERR for titles
    it considers unreachable; we use this as a reachability indicator).
    """
    out = c_int32(0)
    rc = _lib.dvdnav_get_number_of_parts(handle, title_num, ctypes.byref(out))
    if rc != DVDNAV_STATUS_OK:
        return 0
    return int(out.value)


def describe_title_chapters(handle: DvdnavP, title_num: int) -> Optional[tuple[list[int], int]]:
    """Return (per-chapter end times in 90 kHz ticks, total duration in 90 kHz ticks).

    Returns None if libdvdnav reports the title is undescribable — which
    correlates with MakeMKV silently dropping the title.
    """
    times_ptr = POINTER(c_uint64)()
    duration = c_uint64(0)
    n = _lib.dvdnav_describe_title_chapters(
        handle, title_num, ctypes.byref(times_ptr), ctypes.byref(duration),
    )
    if n == 0 or not times_ptr:
        return None
    try:
        times = [int(times_ptr[i]) for i in range(n)]
    finally:
        _libc.free(times_ptr)
    return (times, int(duration.value))


def title_play(handle: DvdnavP, title_num: int) -> bool:
    """Attempt to start playing the given TT_SRPT title.

    Returns True iff dvdnav navigated successfully — meaning the title is
    reachable via the disc's navigation graph (the same property MakeMKV's
    VM-discovery checks via its `+0x1f8` registration).
    """
    rc = _lib.dvdnav_title_play(handle, title_num)
    return rc == DVDNAV_STATUS_OK


def program_play(handle: DvdnavP, title_num: int, pgcn: int, pgn: int = 1) -> bool:
    """Attempt to play a specific (PGCN, PGN) within a title."""
    rc = _lib.dvdnav_program_play(handle, title_num, pgcn, pgn)
    return rc == DVDNAV_STATUS_OK


def current_title_program(handle: DvdnavP) -> Optional[tuple[int, int, int]]:
    """Return (title, pgcn, pgn) of the current playback state, or None on error."""
    title = c_int32(0)
    pgcn = c_int32(0)
    pgn = c_int32(0)
    rc = _lib.dvdnav_current_title_program(
        handle, ctypes.byref(title), ctypes.byref(pgcn), ctypes.byref(pgn),
    )
    if rc != DVDNAV_STATUS_OK:
        return None
    return (int(title.value), int(pgcn.value), int(pgn.value))


def reset(handle: DvdnavP) -> None:
    """Reset the DVD VM and cache buffers to a fresh state."""
    _lib.dvdnav_reset(handle)


def stop(handle: DvdnavP) -> None:
    """Stop playback (returns to FP_PGC / first-play state)."""
    _lib.dvdnav_stop(handle)


def is_title_reachable(handle: DvdnavP, title_num: int) -> bool:
    """High-level reachability check for VM-title discovery.

    Reachable iff dvdnav can describe the title's chapter structure AND
    title_play() succeeds. Mirrors MakeMKV's "register only what the VM can
    navigate to" filter.

    After this call the dvdnav VM is reset, leaving no playback state behind.
    """
    try:
        described = describe_title_chapters(handle, title_num) is not None
        if not described:
            return False
        played = title_play(handle, title_num)
        return played
    finally:
        try:
            _lib.dvdnav_reset(handle)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Nav-graph step API (VM driver)
# ---------------------------------------------------------------------------

def get_next_block(handle: DvdnavP, buf: bytes | None = None):
    """Step the DVD VM once, returning ``(event_code, payload_len)``.

    ``buf`` is a writable ``ctypes`` array (or None — we allocate one).
    The buffer holds the VOB sector returned when event==DVDNAV_BLOCK_OK.

    Returns:
        Tuple ``(event_code, length)`` where event_code is one of the DVDNAV_*
        constants. On error, returns ``(-1, 0)``.
    """
    if buf is None:
        buf = (c_uint8 * DVD_VIDEO_LB_LEN)()
    event = c_int32(0)
    length = c_int32(0)
    rc = _lib.dvdnav_get_next_block(handle, buf, ctypes.byref(event), ctypes.byref(length))
    if rc != DVDNAV_STATUS_OK:
        return (-1, 0)
    return (int(event.value), int(length.value))


def still_skip(handle: DvdnavP) -> None:
    """Skip a DVDNAV_STILL_FRAME event so the VM can proceed."""
    _lib.dvdnav_still_skip(handle)


def wait_skip(handle: DvdnavP) -> None:
    """Skip a DVDNAV_WAIT event so the VM can proceed."""
    _lib.dvdnav_wait_skip(handle)


def menu_call(handle: DvdnavP, menu_id: int) -> bool:
    """Invoke a menu (DVD_MENU_Title=2, DVD_MENU_Root=3, etc.)."""
    rc = _lib.dvdnav_menu_call(handle, menu_id)
    return rc == DVDNAV_STATUS_OK


# ---------------------------------------------------------------------------
# PCI access — Program Control Information (per-VOBU runtime button data)
# ---------------------------------------------------------------------------
#
# libdvdnav exposes a pointer to the current VOBU's pci_t. We treat the
# returned pointer as opaque (an ``int`` address) and parse only the fields
# we need (button count + button-link bytes). Mirrors the layout in
# ``/usr/include/dvdread/nav_types.h``.

_lib.dvdnav_get_current_nav_pci.argtypes = [DvdnavP]
_lib.dvdnav_get_current_nav_pci.restype = c_void_p

# Button-select via PCI (dvdnav_button_select_and_activate is MakeMKV's
# dvdnav_button cmd target). We use the lower-level _select + _activate split.
_lib.dvdnav_button_select.argtypes = [DvdnavP, c_void_p, c_int32]
_lib.dvdnav_button_select.restype = c_int32

_lib.dvdnav_button_activate.argtypes = [DvdnavP, c_void_p]
_lib.dvdnav_button_activate.restype = c_int32

_lib.dvdnav_button_select_and_activate.argtypes = [DvdnavP, c_void_p, c_int32]
_lib.dvdnav_button_select_and_activate.restype = c_int32


# PCI offset layout (from /usr/include/dvdread/nav_types.h)
#
#   pci_t {
#     pci_gi_t     pci_gi;           // 0x00..0x3b (offset 0)
#     nsml_agli_t  nsml_agli;        // 0x3c..0x5f
#     hli_t        hli {              // 0x60
#       hl_gi_t     hl_gi;             // 0x60..0x7d
#       btn_colit_t btn_colit;         // 0x7e..0x95
#       btni_t      btnit[36];         // 0x96..0x32f
#     };
#     uint8_t     zero1[189];        // 0x32a..0x3ff (approx)
#   }
#
# Key field offsets (verified against the public struct definition):
#   hl_gi.btn_ns         @ 0x60 + 0x16 = 0x76      u8 (low 6 bits = button count)
#   hl_gi.nsl_btn_ns     @ 0x60 + 0x17 = 0x77      u8
#   hl_gi.fosl_btnn      @ 0x60 + 0x19 = 0x79      u8 (low 6 bits = "forcedly selected" button)
#   hl_gi.foac_btnn      @ 0x60 + 0x1a = 0x7a      u8 (low 6 bits = "forcedly activated" button)
#   btnit[i]             @ 0x96 + i * 18           18-byte btni_t entries
#
# Within each btni_t (bitfield layout from nav_types.h with the "ABCG DEFH IJ"
# rotation), the up/down/left/right link bytes pack as 6-bit values:
#   bytes +0x08..+0x09: zero3(2) + up(6) + zero4(2) + down(6)
#   bytes +0x0a..+0x0b: zero5(2) + left(6) + zero6(2) + right(6)
PCI_HL_GI_BTN_NS    = 0x76
PCI_HL_GI_FOSL_BTNN = 0x79
PCI_HL_GI_FOAC_BTNN = 0x7a
PCI_BTNIT_OFFSET    = 0x96
PCI_BTNI_SIZE       = 18

# Within each btni:
BTNI_LINK_UP_OFFSET    = 0x08
BTNI_LINK_DOWN_OFFSET  = 0x09
BTNI_LINK_LEFT_OFFSET  = 0x0a
BTNI_LINK_RIGHT_OFFSET = 0x0b


def get_current_nav_pci(handle: DvdnavP) -> int:
    """Return the address of the current pci_t (or 0 if no PCI available).

    The returned int is an opaque pointer — pass it to :func:`pci_*` helpers.
    """
    ptr = _lib.dvdnav_get_current_nav_pci(handle)
    return int(ptr) if ptr else 0


def _pci_read_u8(pci_addr: int, offset: int) -> int:
    """Read a uint8 from the pci_t at ``offset``."""
    if pci_addr == 0:
        return 0
    return ctypes.cast(pci_addr + offset, ctypes.POINTER(c_uint8))[0]


def pci_button_count(pci_addr: int) -> int:
    """Return the number of valid buttons in the current PCI (0..36)."""
    if pci_addr == 0:
        return 0
    # btn_ns: low 6 bits of byte at PCI_HL_GI_BTN_NS
    return _pci_read_u8(pci_addr, PCI_HL_GI_BTN_NS) & 0x3f


def pci_force_selected_button(pci_addr: int) -> int:
    """Return the "forcedly selected" button (low 6 bits), or 0 if none."""
    return _pci_read_u8(pci_addr, PCI_HL_GI_FOSL_BTNN) & 0x3f


def pci_button_links(pci_addr: int, button_num: int) -> tuple[int, int, int, int]:
    """Return the (up, down, left, right) link targets for ``button_num``.

    Each link is a 1-based button index (or 0 = no link). ``button_num`` is
    1-based and must be 1..pci_button_count().

    The link bytes are at fixed positions within each 18-byte btni_t entry.
    """
    if pci_addr == 0 or button_num < 1 or button_num > 36:
        return (0, 0, 0, 0)
    base = pci_addr + PCI_BTNIT_OFFSET + (button_num - 1) * PCI_BTNI_SIZE
    # The link bytes are 6-bit values packed with 2 zero bits in the high
    # nibble. Mask to extract.
    up = ctypes.cast(base + BTNI_LINK_UP_OFFSET, ctypes.POINTER(c_uint8))[0] & 0x3f
    down = ctypes.cast(base + BTNI_LINK_DOWN_OFFSET, ctypes.POINTER(c_uint8))[0] & 0x3f
    left = ctypes.cast(base + BTNI_LINK_LEFT_OFFSET, ctypes.POINTER(c_uint8))[0] & 0x3f
    right = ctypes.cast(base + BTNI_LINK_RIGHT_OFFSET, ctypes.POINTER(c_uint8))[0] & 0x3f
    return (up, down, left, right)


def button_select(handle: DvdnavP, pci_addr: int, button_num: int) -> bool:
    """Select a button on the current menu via dvdnav_button_select."""
    rc = _lib.dvdnav_button_select(handle, pci_addr, button_num)
    return rc == DVDNAV_STATUS_OK


def button_activate(handle: DvdnavP, pci_addr: int) -> bool:
    """Activate the currently selected button."""
    rc = _lib.dvdnav_button_activate(handle, pci_addr)
    return rc == DVDNAV_STATUS_OK


def button_select_and_activate(handle: DvdnavP, pci_addr: int, button_num: int) -> bool:
    """Select + activate a button in one call (MakeMKV's dvdnav_button cmd target)."""
    rc = _lib.dvdnav_button_select_and_activate(handle, pci_addr, button_num)
    return rc == DVDNAV_STATUS_OK


__all__ = [
    "DvdnavError",
    "DvdnavP",
    "DVDNAV_STATUS_ERR",
    "DVDNAV_STATUS_OK",
    "DVDNAV_BLOCK_OK",
    "DVDNAV_NOP",
    "DVDNAV_STILL_FRAME",
    "DVDNAV_VTS_CHANGE",
    "DVDNAV_CELL_CHANGE",
    "DVDNAV_NAV_PACKET",
    "DVDNAV_STOP",
    "DVDNAV_WAIT",
    "DVD_VIDEO_LB_LEN",
    "button_activate",
    "button_select",
    "button_select_and_activate",
    "current_title_program",
    "describe_title_chapters",
    "get_current_nav_pci",
    "get_next_block",
    "get_number_of_parts",
    "get_number_of_titles",
    "is_title_reachable",
    "menu_call",
    "open_disc",
    "pci_button_count",
    "pci_button_links",
    "pci_force_selected_button",
    "program_play",
    "reset",
    "still_skip",
    "stop",
    "title_play",
    "version",
    "wait_skip",
]
