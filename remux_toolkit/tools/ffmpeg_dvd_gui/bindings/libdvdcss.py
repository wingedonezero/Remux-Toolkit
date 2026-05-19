"""
Minimal ctypes binding for libdvdcss.so.2 — used only to detect whether a
disc is CSS-scrambled. We don't do raw CSS reads ourselves; libdvdread (which
links against libdvdcss internally) handles all on-the-fly decryption.

API surface intentionally tiny:
    is_disc_scrambled(path) -> Optional[bool]

Returns True / False, or None if libdvdcss isn't installed / the open failed.
"""
from __future__ import annotations

import ctypes
import ctypes.util
from ctypes import CDLL, POINTER, Structure, c_char_p, c_int, c_void_p
from pathlib import Path
from typing import Optional


class _DvdcssT(Structure):
    pass  # opaque


_DvdcssP = POINTER(_DvdcssT)


def _load() -> Optional[CDLL]:
    name = ctypes.util.find_library("dvdcss") or "libdvdcss.so.2"
    try:
        lib = CDLL(name)
    except OSError:
        return None
    lib.dvdcss_open.argtypes = [c_char_p]
    lib.dvdcss_open.restype = _DvdcssP
    lib.dvdcss_close.argtypes = [_DvdcssP]
    lib.dvdcss_close.restype = c_int
    lib.dvdcss_is_scrambled.argtypes = [_DvdcssP]
    lib.dvdcss_is_scrambled.restype = c_int
    return lib


_lib = _load()


def is_disc_scrambled(path: str | Path) -> Optional[bool]:
    """Return True if the given disc / VIDEO_TS folder / ISO is CSS-scrambled,
    False if it isn't, or None if libdvdcss can't tell (library missing or
    open failed)."""
    if _lib is None:
        return None
    handle = _lib.dvdcss_open(str(path).encode("utf-8"))
    if not handle:
        return None
    try:
        return bool(_lib.dvdcss_is_scrambled(handle))
    finally:
        _lib.dvdcss_close(handle)


def is_available() -> bool:
    return _lib is not None


__all__ = ["is_disc_scrambled", "is_available"]
