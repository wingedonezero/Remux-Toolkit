"""
ctypes binding for libdvdread.so.8.

Mirrors the structures from /usr/include/dvdread/{dvd_reader,ifo_types,ifo_read}.h
and exposes the minimum C entry points we need for disc inspection, IFO traversal
and raw VOB sector reads.

Bit-field layouts and struct packing match GCC on little-endian Linux, which is
how libdvdread is compiled. All packed structs use _pack_ = 1.

Tested against libdvdread 6.1.x (libdvdread.so.8). The public ABI has been stable
for many years.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from ctypes import (
    CDLL, POINTER, Structure, Union,
    c_char, c_char_p, c_int, c_int32, c_uint, c_uint8, c_uint16, c_uint32, c_uint64,
    c_uint64, c_size_t, c_ssize_t, c_void_p,
)
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


_LIB_NAME = ctypes.util.find_library("dvdread") or "libdvdread.so.8"
_lib = CDLL(_LIB_NAME)


# ---------------------------------------------------------------------------
# Opaque handles
# ---------------------------------------------------------------------------

class _DvdReaderT(Structure):
    pass  # opaque


class _DvdFileT(Structure):
    pass  # opaque


DvdReaderP = POINTER(_DvdReaderT)
DvdFileP = POINTER(_DvdFileT)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DVD_VIDEO_LB_LEN = 2048


class DvdReadDomain:
    INFO_FILE = 0          # VTS_XX_0.IFO
    INFO_BACKUP_FILE = 1   # VTS_XX_0.BUP
    MENU_VOBS = 2          # VTS_XX_0.VOB
    TITLE_VOBS = 3         # VTS_XX_[1-9].VOB


# ---------------------------------------------------------------------------
# Primitive IFO structures
# ---------------------------------------------------------------------------

class DvdTime(Structure):
    _pack_ = 1
    _fields_ = [
        ("hour",    c_uint8),
        ("minute",  c_uint8),
        ("second",  c_uint8),
        ("frame_u", c_uint8),
    ]

    @property
    def frame_rate(self) -> float:
        code = (self.frame_u >> 6) & 0x3
        if code == 0b01:
            return 25.0
        if code == 0b11:
            return 30000.0 / 1001
        return 0.0

    @property
    def frames(self) -> int:
        bcd = self.frame_u & 0x3F
        return ((bcd >> 4) * 10) + (bcd & 0xF)

    @property
    def total_seconds(self) -> float:
        hh = ((self.hour >> 4) * 10) + (self.hour & 0xF)
        mm = ((self.minute >> 4) * 10) + (self.minute & 0xF)
        ss = ((self.second >> 4) * 10) + (self.second & 0xF)
        sec = hh * 3600 + mm * 60 + ss
        fr = self.frame_rate
        if fr > 0:
            sec += self.frames / fr
        return sec


class VideoAttr(Structure):
    _pack_ = 1
    _fields_ = [
        # byte 0
        ("mpeg_version",         c_uint8, 2),
        ("video_format",         c_uint8, 2),
        ("display_aspect_ratio", c_uint8, 2),
        ("permitted_df",         c_uint8, 2),
        # byte 1
        ("line21_cc_1",          c_uint8, 1),
        ("line21_cc_2",          c_uint8, 1),
        ("unknown1_video",       c_uint8, 1),
        ("bit_rate",             c_uint8, 1),
        ("picture_size",         c_uint8, 2),
        ("letterboxed",          c_uint8, 1),
        ("film_mode",            c_uint8, 1),
    ]


class _AudioAppKaraoke(Structure):
    _pack_ = 1
    _fields_ = [
        ("unknown4",           c_uint8, 1),
        ("channel_assignment", c_uint8, 3),
        ("version",            c_uint8, 2),
        ("mc_intro",           c_uint8, 1),
        ("mode",               c_uint8, 1),
    ]


class _AudioAppSurround(Structure):
    _pack_ = 1
    _fields_ = [
        ("unknown5",      c_uint8, 4),
        ("dolby_encoded", c_uint8, 1),
        ("unknown6",      c_uint8, 3),
    ]


class _AudioAppInfo(Union):
    _pack_ = 1
    _fields_ = [
        ("karaoke",  _AudioAppKaraoke),
        ("surround", _AudioAppSurround),
    ]


class AudioAttr(Structure):
    _pack_ = 1
    _fields_ = [
        # byte 0
        ("audio_format",           c_uint8, 3),
        ("multichannel_extension", c_uint8, 1),
        ("lang_type",              c_uint8, 2),
        ("application_mode",       c_uint8, 2),
        # byte 1
        ("quantization",      c_uint8, 2),
        ("sample_frequency",  c_uint8, 2),
        ("unknown1_audio",    c_uint8, 1),
        ("channels",          c_uint8, 3),
        # bytes 2-7
        ("lang_code",      c_uint16),
        ("lang_extension", c_uint8),
        ("code_extension", c_uint8),
        ("unknown3",       c_uint8),
        ("app_info",       _AudioAppInfo),
    ]


class SubpAttr(Structure):
    _pack_ = 1
    _fields_ = [
        # byte 0
        ("code_mode", c_uint8, 3),
        ("zero1",     c_uint8, 3),
        ("type",      c_uint8, 2),
        # bytes 1-5
        ("zero2",          c_uint8),
        ("lang_code",      c_uint16),
        ("lang_extension", c_uint8),
        ("code_extension", c_uint8),
    ]


class MultichannelExt(Structure):
    _pack_ = 1
    _fields_ = [
        ("byte0",   c_uint8),  # zero1:7 ach0_gme:1
        ("byte1",   c_uint8),  # zero2:7 ach1_gme:1
        ("byte2",   c_uint8),  # zero3:4 ach2_*:4
        ("byte3",   c_uint8),  # zero4:4 ach3_*:4
        ("byte4",   c_uint8),  # zero5:4 ach4_*:4
        ("zero6",   c_uint8 * 19),
    ]


# ---------------------------------------------------------------------------
# Cell & PGC structures
# ---------------------------------------------------------------------------

class UserOps(Structure):
    _pack_ = 1
    _fields_ = [("raw", c_uint8 * 4)]


class CellPlayback(Structure):
    _pack_ = 1
    _fields_ = [
        # bytes 0-1 (16 bits of flags)
        ("block_mode",        c_uint16, 2),
        ("block_type",        c_uint16, 2),
        ("seamless_play",     c_uint16, 1),
        ("interleaved",       c_uint16, 1),
        ("stc_discontinuity", c_uint16, 1),
        ("seamless_angle",    c_uint16, 1),
        ("zero_1_cell",       c_uint16, 1),
        ("playback_mode",     c_uint16, 1),
        ("restricted",        c_uint16, 1),
        ("cell_type",         c_uint16, 5),
        # bytes 2-3
        ("still_time",        c_uint8),
        ("cell_cmd_nr",       c_uint8),
        # bytes 4-7
        ("playback_time",     DvdTime),
        # bytes 8-23
        ("first_sector",            c_uint32),
        ("first_ilvu_end_sector",   c_uint32),
        ("last_vobu_start_sector",  c_uint32),
        ("last_sector",             c_uint32),
    ]


class CellPosition(Structure):
    _pack_ = 1
    _fields_ = [
        ("vob_id_nr", c_uint16),
        ("zero_1",    c_uint8),
        ("cell_nr",   c_uint8),
    ]


class _VmCmd(Structure):
    _pack_ = 1
    _fields_ = [("bytes", c_uint8 * 8)]


class PgcCommandTbl(Structure):
    _pack_ = 1
    _fields_ = [
        ("nr_of_pre",  c_uint16),
        ("nr_of_post", c_uint16),
        ("nr_of_cell", c_uint16),
        ("last_byte",  c_uint16),
        ("pre_cmds",   POINTER(_VmCmd)),
        ("post_cmds",  POINTER(_VmCmd)),
        ("cell_cmds",  POINTER(_VmCmd)),
    ]


class Pgc(Structure):
    _pack_ = 1
    _fields_ = [
        ("zero_1",            c_uint16),
        ("nr_of_programs",    c_uint8),
        ("nr_of_cells",       c_uint8),
        ("playback_time",     DvdTime),
        ("prohibited_ops",    UserOps),
        ("audio_control",     c_uint16 * 8),
        ("subp_control",      c_uint32 * 32),
        ("next_pgc_nr",       c_uint16),
        ("prev_pgc_nr",       c_uint16),
        ("goup_pgc_nr",       c_uint16),
        ("pg_playback_mode",  c_uint8),
        ("still_time",        c_uint8),
        ("palette",           c_uint32 * 16),
        ("command_tbl_offset", c_uint16),
        ("program_map_offset", c_uint16),
        ("cell_playback_offset", c_uint16),
        ("cell_position_offset", c_uint16),
        ("command_tbl",        POINTER(PgcCommandTbl)),
        ("program_map",        c_void_p),  # pgc_program_map_t* (uint8*)
        ("cell_playback",      POINTER(CellPlayback)),
        ("cell_position",      POINTER(CellPosition)),
        ("ref_count",          c_int),
    ]


class PgciSrp(Structure):
    _pack_ = 1
    _fields_ = [
        ("entry_id",       c_uint8),
        ("block_flags",    c_uint8),  # block_mode:2 block_type:2 zero_1:4
        ("ptl_id_mask",    c_uint16),
        ("pgc_start_byte", c_uint32),
        ("pgc",            POINTER(Pgc)),
    ]


class Pgcit(Structure):
    _pack_ = 1
    _fields_ = [
        ("nr_of_pgci_srp", c_uint16),
        ("zero_1",         c_uint16),
        ("last_byte",      c_uint32),
        ("pgci_srp",       POINTER(PgciSrp)),
        ("ref_count",      c_int),
    ]


class _PgciLu(Structure):
    _pack_ = 1
    _fields_ = [
        ("lang_code",       c_uint16),
        ("lang_extension",  c_uint8),
        ("exists",          c_uint8),
        ("lang_start_byte", c_uint32),
        ("pgcit",           POINTER(Pgcit)),
    ]


class PgciUt(Structure):
    _pack_ = 1
    _fields_ = [
        ("nr_of_lus", c_uint16),
        ("zero_1",    c_uint16),
        ("last_byte", c_uint32),
        ("lu",        POINTER(_PgciLu)),
    ]


# ---------------------------------------------------------------------------
# Title & PTT tables
# ---------------------------------------------------------------------------

class _PlaybackType(Structure):
    _pack_ = 1
    _fields_ = [("raw", c_uint8)]


class TitleInfo(Structure):
    _pack_ = 1
    _fields_ = [
        ("pb_ty",            _PlaybackType),
        ("nr_of_angles",     c_uint8),
        ("nr_of_ptts",       c_uint16),
        ("parental_id",      c_uint16),
        ("title_set_nr",     c_uint8),
        ("vts_ttn",          c_uint8),
        ("title_set_sector", c_uint32),
    ]


class TtSrpt(Structure):
    _pack_ = 1
    _fields_ = [
        ("nr_of_srpts", c_uint16),
        ("zero_1",      c_uint16),
        ("last_byte",   c_uint32),
        ("title",       POINTER(TitleInfo)),
    ]


class _PttInfo(Structure):
    _pack_ = 1
    _fields_ = [("pgcn", c_uint16), ("pgn", c_uint16)]


class _Ttu(Structure):
    _pack_ = 1
    _fields_ = [("nr_of_ptts", c_uint16), ("ptt", POINTER(_PttInfo))]


class VtsPttSrpt(Structure):
    _pack_ = 1
    _fields_ = [
        ("nr_of_srpts", c_uint16),
        ("zero_1",      c_uint16),
        ("last_byte",   c_uint32),
        ("title",       POINTER(_Ttu)),
        ("ttu_offset",  POINTER(c_uint32)),
    ]


# ---------------------------------------------------------------------------
# VMGI (Video Manager Information) management table
# ---------------------------------------------------------------------------

class VmgiMat(Structure):
    _pack_ = 1
    _fields_ = [
        ("vmg_identifier",          c_char * 12),
        ("vmg_last_sector",         c_uint32),
        ("zero_1",                  c_uint8 * 12),
        ("vmgi_last_sector",        c_uint32),
        ("zero_2",                  c_uint8),
        ("specification_version",   c_uint8),
        ("vmg_category",            c_uint32),
        ("vmg_nr_of_volumes",       c_uint16),
        ("vmg_this_volume_nr",      c_uint16),
        ("disc_side",               c_uint8),
        ("zero_3",                  c_uint8 * 19),
        ("vmg_nr_of_title_sets",    c_uint16),
        ("provider_identifier",     c_char * 32),
        ("vmg_pos_code",            c_uint64),
        ("zero_4",                  c_uint8 * 24),
        ("vmgi_last_byte",          c_uint32),
        ("first_play_pgc",          c_uint32),
        ("zero_5",                  c_uint8 * 56),
        ("vmgm_vobs",               c_uint32),
        ("tt_srpt",                 c_uint32),
        ("vmgm_pgci_ut",            c_uint32),
        ("ptl_mait",                c_uint32),
        ("vts_atrt",                c_uint32),
        ("txtdt_mgi",               c_uint32),
        ("vmgm_c_adt",              c_uint32),
        ("vmgm_vobu_admap",         c_uint32),
        ("zero_6",                  c_uint8 * 32),

        ("vmgm_video_attr",         VideoAttr),
        ("zero_7",                  c_uint8),
        ("nr_of_vmgm_audio_streams", c_uint8),
        ("vmgm_audio_attr",         AudioAttr),
        ("zero_8",                  AudioAttr * 7),
        ("zero_9",                  c_uint8 * 17),
        ("nr_of_vmgm_subp_streams", c_uint8),
        ("vmgm_subp_attr",          SubpAttr),
        ("zero_10",                 SubpAttr * 27),
    ]


# ---------------------------------------------------------------------------
# VTSI (Video Title Set Information) management table
# ---------------------------------------------------------------------------

class VtsiMat(Structure):
    _pack_ = 1
    _fields_ = [
        ("vts_identifier",          c_char * 12),
        ("vts_last_sector",         c_uint32),
        ("zero_1",                  c_uint8 * 12),
        ("vtsi_last_sector",        c_uint32),
        ("zero_2",                  c_uint8),
        ("specification_version",   c_uint8),
        ("vts_category",            c_uint32),
        ("zero_3",                  c_uint16),
        ("zero_4",                  c_uint16),
        ("zero_5",                  c_uint8),
        ("zero_6",                  c_uint8 * 19),
        ("zero_7",                  c_uint16),
        ("zero_8",                  c_uint8 * 32),
        ("zero_9",                  c_uint64),
        ("zero_10",                 c_uint8 * 24),
        ("vtsi_last_byte",          c_uint32),
        ("zero_11",                 c_uint32),
        ("zero_12",                 c_uint8 * 56),
        ("vtsm_vobs",               c_uint32),
        ("vtstt_vobs",              c_uint32),
        ("vts_ptt_srpt",            c_uint32),
        ("vts_pgcit",               c_uint32),
        ("vtsm_pgci_ut",            c_uint32),
        ("vts_tmapt",               c_uint32),
        ("vtsm_c_adt",              c_uint32),
        ("vtsm_vobu_admap",         c_uint32),
        ("vts_c_adt",               c_uint32),
        ("vts_vobu_admap",          c_uint32),
        ("zero_13",                 c_uint8 * 24),

        ("vtsm_video_attr",         VideoAttr),
        ("zero_14",                 c_uint8),
        ("nr_of_vtsm_audio_streams", c_uint8),
        ("vtsm_audio_attr",         AudioAttr),
        ("zero_15",                 AudioAttr * 7),
        ("zero_16",                 c_uint8 * 17),
        ("nr_of_vtsm_subp_streams", c_uint8),
        ("vtsm_subp_attr",          SubpAttr),
        ("zero_17",                 SubpAttr * 27),
        ("zero_18",                 c_uint8 * 2),

        ("vts_video_attr",          VideoAttr),
        ("zero_19",                 c_uint8),
        ("nr_of_vts_audio_streams", c_uint8),
        ("vts_audio_attr",          AudioAttr * 8),
        ("zero_20",                 c_uint8 * 17),
        ("nr_of_vts_subp_streams",  c_uint8),
        ("vts_subp_attr",           SubpAttr * 32),
        ("zero_21",                 c_uint16),
        ("vts_mu_audio_attr",       MultichannelExt * 8),
    ]


# ---------------------------------------------------------------------------
# Top-level IFO handle (struct of pointers)
# ---------------------------------------------------------------------------

class _PtlMait(Structure):
    pass  # opaque for now


class _VtsAtrt(Structure):
    pass  # opaque


class _TxtdtMgi(Structure):
    pass  # opaque


class _CAdt(Structure):
    pass  # opaque


class _VobuAdmap(Structure):
    pass  # opaque


class _VtsTmapt(Structure):
    pass  # opaque


class IfoHandle(Structure):
    _pack_ = 1
    _fields_ = [
        # VMGI
        ("vmgi_mat",        POINTER(VmgiMat)),
        ("tt_srpt",         POINTER(TtSrpt)),
        ("first_play_pgc",  POINTER(Pgc)),
        ("ptl_mait",        POINTER(_PtlMait)),
        ("vts_atrt",        POINTER(_VtsAtrt)),
        ("txtdt_mgi",       POINTER(_TxtdtMgi)),
        # Common
        ("pgci_ut",         POINTER(PgciUt)),
        ("menu_c_adt",      POINTER(_CAdt)),
        ("menu_vobu_admap", POINTER(_VobuAdmap)),
        # VTSI
        ("vtsi_mat",        POINTER(VtsiMat)),
        ("vts_ptt_srpt",    POINTER(VtsPttSrpt)),
        ("vts_pgcit",       POINTER(Pgcit)),
        ("vts_tmapt",       POINTER(_VtsTmapt)),
        ("vts_c_adt",       POINTER(_CAdt)),
        ("vts_vobu_admap",  POINTER(_VobuAdmap)),
    ]


# ---------------------------------------------------------------------------
# Function prototypes
# ---------------------------------------------------------------------------

_lib.DVDOpen.argtypes = [c_char_p]
_lib.DVDOpen.restype = DvdReaderP


# DVDOpen2 takes (priv, logger_cb, path). We expose it so callers can silence
# libdvdread's "Couldn't find device name" chatter on folder inputs.
_DVDLogCbT = ctypes.CFUNCTYPE(None, c_void_p, c_int, c_char_p, c_void_p)


class DvdLoggerCb(Structure):
    _fields_ = [("pf_log", _DVDLogCbT)]


@_DVDLogCbT
def _silent_log(priv, level, fmt, va):
    pass


_SILENT_LOGGER = DvdLoggerCb(_silent_log)

_lib.DVDOpen2.argtypes = [c_void_p, POINTER(DvdLoggerCb), c_char_p]
_lib.DVDOpen2.restype = DvdReaderP

_lib.DVDClose.argtypes = [DvdReaderP]
_lib.DVDClose.restype = None

_lib.DVDUDFVolumeInfo.argtypes = [DvdReaderP, c_char_p, c_uint, POINTER(c_uint8), c_uint]
_lib.DVDUDFVolumeInfo.restype = c_int

_lib.DVDISOVolumeInfo.argtypes = [DvdReaderP, c_char_p, c_uint, POINTER(c_uint8), c_uint]
_lib.DVDISOVolumeInfo.restype = c_int

_lib.DVDDiscID.argtypes = [DvdReaderP, POINTER(c_uint8)]
_lib.DVDDiscID.restype = c_int

_lib.DVDOpenFile.argtypes = [DvdReaderP, c_int, c_int]
_lib.DVDOpenFile.restype = DvdFileP

_lib.DVDCloseFile.argtypes = [DvdFileP]
_lib.DVDCloseFile.restype = None

_lib.DVDReadBlocks.argtypes = [DvdFileP, c_int, c_size_t, POINTER(c_uint8)]
_lib.DVDReadBlocks.restype = c_ssize_t

_lib.DVDFileSize.argtypes = [DvdFileP]
_lib.DVDFileSize.restype = c_ssize_t

_lib.ifoOpen.argtypes = [DvdReaderP, c_int]
_lib.ifoOpen.restype = POINTER(IfoHandle)

_lib.ifoClose.argtypes = [POINTER(IfoHandle)]
_lib.ifoClose.restype = None


# ---------------------------------------------------------------------------
# Pythonic helpers
# ---------------------------------------------------------------------------

class DvdReadError(RuntimeError):
    pass


@contextmanager
def open_disc(path: str | Path, *, silent: bool = True) -> Iterator[DvdReaderP]:
    """Open a DVD (folder, VIDEO_TS folder, ISO, or block device).
    `silent=True` (default) routes libdvdread INFO/DEBUG messages to /dev/null
    via a no-op logger callback — folder inputs otherwise trip the harmless
    'Couldn't find device name' warning."""
    encoded = str(path).encode("utf-8")
    if silent:
        handle = _lib.DVDOpen2(None, ctypes.byref(_SILENT_LOGGER), encoded)
    else:
        handle = _lib.DVDOpen(encoded)
    if not handle:
        raise DvdReadError(f"DVDOpen failed for {path!r}")
    try:
        yield handle
    finally:
        _lib.DVDClose(handle)


@contextmanager
def open_ifo(dvd: DvdReaderP, title: int) -> Iterator[POINTER(IfoHandle)]:
    """title=0 opens VMG (VIDEO_TS.IFO); title>=1 opens VTS_xx_0.IFO."""
    ifo = _lib.ifoOpen(dvd, title)
    if not ifo:
        raise DvdReadError(f"ifoOpen({title}) failed")
    try:
        yield ifo
    finally:
        _lib.ifoClose(ifo)


def get_volume_info(dvd: DvdReaderP) -> tuple[str, bytes]:
    """Returns (volume_id, volume_set_id). Falls back to ISO9660 if UDF returns nothing."""
    volid = ctypes.create_string_buffer(33)
    volsetid = (c_uint8 * 128)()
    rc = _lib.DVDUDFVolumeInfo(dvd, volid, 33, volsetid, 128)
    if rc != 0:
        # Fallback for non-UDF discs
        rc = _lib.DVDISOVolumeInfo(dvd, volid, 33, volsetid, 128)
        if rc != 0:
            return ("", b"")
    return (volid.value.decode("latin-1", errors="replace").strip(), bytes(volsetid))


def get_disc_id(dvd: DvdReaderP) -> str:
    """Returns hex string of the 128-bit disc ID (MD5 of IFO files)."""
    buf = (c_uint8 * 16)()
    rc = _lib.DVDDiscID(dvd, buf)
    if rc != 0:
        return ""
    return bytes(buf).hex()


# Helper functions that decode the packed enum fields into human strings.
# These mirror the meanings documented in the DVD-Video spec (and used by
# libdvdread/lsdvd).

_VIDEO_FORMAT = {0: "NTSC", 1: "PAL"}
_ASPECT = {0: "4:3", 1: "reserved", 2: "reserved", 3: "16:9"}
_PICTURE_SIZE_NTSC = {0: "720x480", 1: "704x480", 2: "352x480", 3: "352x240"}
_PICTURE_SIZE_PAL = {0: "720x576", 1: "704x576", 2: "352x576", 3: "352x288"}
_MPEG_VERSION = {0: "mpeg1", 1: "mpeg2"}
_AUDIO_FORMAT = {
    0: "ac3", 1: "reserved", 2: "mpeg1", 3: "mpeg2ext",
    4: "lpcm", 5: "sdds", 6: "dts", 7: "reserved",
}
_AUDIO_QUANT = {0: "16bit", 1: "20bit", 2: "24bit", 3: "drc"}
_AUDIO_SAMPLE = {0: "48kHz", 1: "96kHz"}
_AUDIO_APPMODE = {0: "unspecified", 1: "karaoke", 2: "surround"}
_AUDIO_LANG_TYPE = {0: "unspecified", 1: "language_included"}
_SUBP_TYPE = {0: "not_specified", 1: "language", 2: "other"}
_SUBP_CODE_MODE = {0: "run_length", 1: "extended", 2: "other"}


def video_attr_to_dict(v: VideoAttr) -> dict:
    fmt = _VIDEO_FORMAT.get(v.video_format, f"unknown_{v.video_format}")
    sizes = _PICTURE_SIZE_NTSC if v.video_format == 0 else _PICTURE_SIZE_PAL
    return {
        "codec": _MPEG_VERSION.get(v.mpeg_version, f"unknown_{v.mpeg_version}"),
        "format": fmt,
        "aspect_ratio": _ASPECT.get(v.display_aspect_ratio, f"unknown_{v.display_aspect_ratio}"),
        "picture_size": sizes.get(v.picture_size, f"unknown_{v.picture_size}"),
        "letterboxed": bool(v.letterboxed),
        "film_mode": bool(v.film_mode),
        "line21_cc_1": bool(v.line21_cc_1),
        "line21_cc_2": bool(v.line21_cc_2),
    }


def _lang_code_to_str(lang_code: int) -> str:
    if lang_code == 0:
        return ""
    hi = (lang_code >> 8) & 0xFF
    lo = lang_code & 0xFF
    if 0x20 <= hi < 0x7F and 0x20 <= lo < 0x7F:
        return chr(hi) + chr(lo)
    return ""


def audio_attr_to_dict(a: AudioAttr) -> dict:
    return {
        "codec":      _AUDIO_FORMAT.get(a.audio_format, f"unknown_{a.audio_format}"),
        "channels":   a.channels + 1,  # field stores N-1
        "sample_rate": _AUDIO_SAMPLE.get(a.sample_frequency, "?"),
        "quantization": _AUDIO_QUANT.get(a.quantization, "?"),
        "language":   _lang_code_to_str(a.lang_code),
        "lang_type":  _AUDIO_LANG_TYPE.get(a.lang_type, "?"),
        "app_mode":   _AUDIO_APPMODE.get(a.application_mode, "?"),
        "multichannel_extension": bool(a.multichannel_extension),
        "code_extension": a.code_extension,
    }


def subp_attr_to_dict(s: SubpAttr) -> dict:
    return {
        "language":      _lang_code_to_str(s.lang_code),
        "type":          _SUBP_TYPE.get(s.type, f"unknown_{s.type}"),
        "code_mode":     _SUBP_CODE_MODE.get(s.code_mode, f"unknown_{s.code_mode}"),
        "code_extension": s.code_extension,
    }


def read_blocks(dvd_file: DvdFileP, offset: int, count: int) -> bytes:
    """Read `count` 2048-byte logical blocks starting at `offset`. Raises on error."""
    buf = (c_uint8 * (count * DVD_VIDEO_LB_LEN))()
    got = _lib.DVDReadBlocks(dvd_file, offset, count, buf)
    if got < 0:
        raise DvdReadError(f"DVDReadBlocks failed at offset {offset}")
    return bytes(buf)[: got * DVD_VIDEO_LB_LEN]


@contextmanager
def open_vob(dvd: DvdReaderP, vts_number: int, *, menu: bool = False) -> Iterator[DvdFileP]:
    """Open VTS_XX_[1-9].VOB (or VTS_XX_0.VOB for menu) for raw block reads."""
    domain = DvdReadDomain.MENU_VOBS if menu else DvdReadDomain.TITLE_VOBS
    f = _lib.DVDOpenFile(dvd, vts_number, domain)
    if not f:
        raise DvdReadError(f"DVDOpenFile(vts={vts_number}, domain={domain}) failed")
    try:
        yield f
    finally:
        _lib.DVDCloseFile(f)


# ---------------------------------------------------------------------------
# Sanity checks (run at import time when DVDREAD_DEBUG is set)
# ---------------------------------------------------------------------------

def _self_check() -> None:
    """Assert known struct sizes match the libdvdread spec."""
    import os
    if not os.environ.get("DVDREAD_DEBUG"):
        return
    expected = {
        DvdTime: 4,
        VideoAttr: 2,
        AudioAttr: 8,
        SubpAttr: 6,
        CellPlayback: 24,
        CellPosition: 4,
        TitleInfo: 12,
        MultichannelExt: 24,
    }
    for cls, want in expected.items():
        got = ctypes.sizeof(cls)
        assert got == want, f"{cls.__name__}: sizeof={got}, expected={want}"


_self_check()
