"""
core/mux/native — Python loader for the libmkv_shim C++ wrapper.

The shim is a small (~500 lines) C++ wrapper over libebml + libmatroska,
compiled into ``libmkv_shim.so`` next to this module. The build is a
one-time gcc invocation that the GUI exposes as a button (see ``build_shim``).

This module provides:

  * :func:`get_shim_status` — inspect without loading. Use this for the
    "is it built?" UI banner.
  * :func:`build_shim` — invoke ``build.sh``; stream output to a callback.
  * :func:`load_shim` — load the .so and return a typed :class:`Shim`
    object exposing the C API as bound methods. Raises
    :class:`ShimUnavailable` if the .so isn't built/loadable.

Status enum guides the UI:

  * ``OK`` — built, loadable, ready to use
  * ``NOT_BUILT`` — .so doesn't exist; offer Build button
  * ``STALE`` — source newer than .so; offer Rebuild button
  * ``BUILD_FAILED`` — last build attempt errored; show error_message
  * ``LOAD_FAILED`` — .so exists but ctypes refused; show error_message
  * ``MISSING_HEADERS`` — libebml-dev / libmatroska-dev not installed;
    show ``missing_packages`` so the UI can render the apt command
"""

from __future__ import annotations

import ctypes
import enum
import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional


# ----------------------------------------------------------------------
# Module paths
# ----------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent
_SHIM_SO = _THIS_DIR / "libmkv_shim.so"
_SHIM_SRC = _THIS_DIR / "libmkv_shim.cpp"
_SHIM_HDR = _THIS_DIR / "libmkv_shim.h"
_BUILD_SCRIPT = _THIS_DIR / "build.sh"

_REQUIRED_HEADERS = [
    Path("/usr/include/ebml/EbmlHead.h"),
    Path("/usr/include/matroska/KaxSegment.h"),
]


# ----------------------------------------------------------------------
# Status types
# ----------------------------------------------------------------------

class ShimStatus(enum.Enum):
    OK = "ok"
    NOT_BUILT = "not_built"
    STALE = "stale"
    BUILD_FAILED = "build_failed"
    LOAD_FAILED = "load_failed"
    MISSING_HEADERS = "missing_headers"


@dataclass
class ShimInfo:
    status: ShimStatus
    so_path: Path = _SHIM_SO
    src_path: Path = _SHIM_SRC
    last_build_mtime: Optional[float] = None
    version: Optional[str] = None
    error_message: Optional[str] = None
    missing_packages: List[str] = field(default_factory=list)


class ShimUnavailable(Exception):
    """Raised by :func:`load_shim` when the shim isn't usable."""
    def __init__(self, info: ShimInfo) -> None:
        msg = f"native shim status={info.status.value}"
        if info.error_message:
            msg += f": {info.error_message}"
        super().__init__(msg)
        self.info = info


# ----------------------------------------------------------------------
# Track-info struct mirroring the C side
# ----------------------------------------------------------------------

class _ShimTrackInfo(ctypes.Structure):
    """Mirror of ``mkv_shim_track_info`` in libmkv_shim.h."""
    _fields_ = [
        ("codec_id",              ctypes.c_char_p),
        ("codec_subid",           ctypes.c_char_p),
        ("lang",                  ctypes.c_char_p),
        ("name",                  ctypes.c_char_p),
        ("codec_private",         ctypes.c_char_p),
        ("codec_private_size",    ctypes.c_uint32),
        ("mkv_flags",             ctypes.c_uint32),
        ("default_duration_ns",   ctypes.c_int64),
        ("min_cache",             ctypes.c_uint32),
        ("pixel_h",               ctypes.c_int),
        ("pixel_v",               ctypes.c_int),
        ("display_h",             ctypes.c_int),
        ("display_v",             ctypes.c_int),
        ("stereo_mode",           ctypes.c_int),
        # Colour metadata (KaxVideoColour). 0 = unspecified.
        ("color_primaries",       ctypes.c_int),
        ("color_transfer",        ctypes.c_int),
        ("color_matrix",          ctypes.c_int),
        ("color_range",           ctypes.c_int),
        ("sample_rate",           ctypes.c_int),
        ("channels_count",        ctypes.c_int),
        ("bits_per_sample",       ctypes.c_int),
        ("offset_sequence_id_ref", ctypes.c_uint8),
    ]


# Track types — must match mkv_shim_track_type
TRACK_TYPE_UNKNOWN  = 0
TRACK_TYPE_VIDEO    = 1
TRACK_TYPE_AUDIO    = 2
TRACK_TYPE_SUBTITLE = 3

# Frame flags — must match mkv_shim_frame_flags
FRAME_KEYFRAME      = 1
FRAME_CLUSTER_START = 2
FRAME_CHAPTER_MARK  = 4
FRAME_DISCARDABLE   = 8
FRAME_OLD_BLOCK     = 16
FRAME_AUTO_DURATION = 32

# Track flags — must match mkv_shim_track_flags
TRACK_FLAG_DEFAULT = 1
TRACK_FLAG_FORCED  = 2
TRACK_FLAG_LACING  = 128


# ----------------------------------------------------------------------
# Status / build
# ----------------------------------------------------------------------

def get_shim_status() -> ShimInfo:
    """Return current shim status without loading the .so into the process."""
    info = ShimInfo(status=ShimStatus.NOT_BUILT)

    missing = [str(p) for p in _REQUIRED_HEADERS if not p.exists()]
    if missing:
        return ShimInfo(
            status=ShimStatus.MISSING_HEADERS,
            error_message="missing C++ headers: " + ", ".join(missing),
            missing_packages=["libebml-dev", "libmatroska-dev"],
        )

    if not _SHIM_SO.exists():
        return info  # NOT_BUILT

    info.last_build_mtime = _SHIM_SO.stat().st_mtime

    # Stale check
    for src_path in (_SHIM_SRC, _SHIM_HDR):
        if src_path.exists() and src_path.stat().st_mtime > info.last_build_mtime:
            return ShimInfo(
                status=ShimStatus.STALE,
                last_build_mtime=info.last_build_mtime,
                error_message=f"{src_path.name} modified after last build",
            )

    # Try loading (returns OK if successful)
    try:
        lib = ctypes.CDLL(str(_SHIM_SO))
        lib.mkv_writer_version.restype = ctypes.c_char_p
        version = lib.mkv_writer_version()
        info.version = version.decode("utf-8", errors="replace") if version else "?"
        info.status = ShimStatus.OK
    except OSError as e:
        info.status = ShimStatus.LOAD_FAILED
        info.error_message = str(e)
    return info


def build_shim(
    *,
    force: bool = False,
    output_callback: Optional[Callable[[str], None]] = None,
) -> ShimInfo:
    """Compile the shim. Streams build output line-by-line to ``output_callback``.

    Returns the updated :class:`ShimInfo` (re-evaluated after build).

    Set ``force=True`` to skip the "already up-to-date" optimization. The
    build script always touches the .so when it completes successfully.
    """
    if not force:
        current = get_shim_status()
        if current.status == ShimStatus.OK:
            return current

    # Header pre-check (so the UI can present a clean error before invoking gcc).
    missing = [str(p) for p in _REQUIRED_HEADERS if not p.exists()]
    if missing:
        return ShimInfo(
            status=ShimStatus.MISSING_HEADERS,
            error_message="missing C++ headers: " + ", ".join(missing),
            missing_packages=["libebml-dev", "libmatroska-dev"],
        )

    if not _BUILD_SCRIPT.exists():
        return ShimInfo(
            status=ShimStatus.BUILD_FAILED,
            error_message=f"build script not found: {_BUILD_SCRIPT}",
        )

    if output_callback is None:
        output_callback = lambda _line: None  # noqa: E731

    try:
        proc = subprocess.Popen(
            ["/bin/bash", str(_BUILD_SCRIPT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(_THIS_DIR),
        )
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            output_callback(line.rstrip())
        rc = proc.wait()
    except FileNotFoundError as e:
        return ShimInfo(
            status=ShimStatus.BUILD_FAILED,
            error_message=f"build invocation failed: {e}",
        )
    except Exception as e:
        return ShimInfo(
            status=ShimStatus.BUILD_FAILED,
            error_message=f"build invocation crashed: {e!r}",
        )

    if rc != 0:
        return ShimInfo(
            status=ShimStatus.BUILD_FAILED,
            error_message=f"build.sh exited with code {rc}",
        )

    return get_shim_status()


# ----------------------------------------------------------------------
# Loaded shim
# ----------------------------------------------------------------------

# Module-level cache: load_shim returns the same handle for repeated calls.
_loaded_shim: Optional["Shim"] = None
_load_lock = threading.Lock()


def load_shim() -> "Shim":
    """Load the shim and return a typed wrapper.

    Raises :class:`ShimUnavailable` if status != OK. UI should call
    :func:`get_shim_status` and handle non-OK statuses (build, install
    headers, etc.) BEFORE calling this.
    """
    global _loaded_shim
    with _load_lock:
        if _loaded_shim is not None:
            return _loaded_shim
        info = get_shim_status()
        if info.status != ShimStatus.OK:
            raise ShimUnavailable(info)
        _loaded_shim = Shim(_SHIM_SO)
        return _loaded_shim


class Shim:
    """Wraps the loaded libmkv_shim.so with typed ctypes bindings.

    Usage::

        shim = load_shim()
        w = shim.open(b"/tmp/out.mkv", b"my-app")
        shim.set_timestamp_scale(w, 1_000_000)
        shim.set_title(w, b"My Title")
        ti = shim.make_track_info(codec_id=b"V_MPEG2", ...)
        track = shim.add_track(w, shim.TRACK_TYPE_VIDEO, ti)
        shim.write_headers(w)
        shim.start_cluster(w, 0)
        shim.add_simple_block(w, track, frame_bytes, len(frame_bytes), 0,
                              shim.FRAME_KEYFRAME)
        shim.end_cluster(w)
        shim.finalize(w, max_duration_ns)
        shim.close(w)
    """

    # Re-export constants on the instance for convenience.
    TRACK_TYPE_UNKNOWN  = TRACK_TYPE_UNKNOWN
    TRACK_TYPE_VIDEO    = TRACK_TYPE_VIDEO
    TRACK_TYPE_AUDIO    = TRACK_TYPE_AUDIO
    TRACK_TYPE_SUBTITLE = TRACK_TYPE_SUBTITLE
    FRAME_KEYFRAME      = FRAME_KEYFRAME
    FRAME_CLUSTER_START = FRAME_CLUSTER_START
    FRAME_CHAPTER_MARK  = FRAME_CHAPTER_MARK
    FRAME_DISCARDABLE   = FRAME_DISCARDABLE
    FRAME_OLD_BLOCK     = FRAME_OLD_BLOCK
    FRAME_AUTO_DURATION = FRAME_AUTO_DURATION
    TRACK_FLAG_DEFAULT  = TRACK_FLAG_DEFAULT
    TRACK_FLAG_FORCED   = TRACK_FLAG_FORCED
    TRACK_FLAG_LACING   = TRACK_FLAG_LACING
    TrackInfo = _ShimTrackInfo

    def __init__(self, so_path: Path) -> None:
        self._lib = ctypes.CDLL(str(so_path))
        self._setup_signatures()

    def _setup_signatures(self) -> None:
        L = self._lib
        # mkv_writer_t* mkv_writer_open(const char* path, const char* writing_app);
        L.mkv_writer_open.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        L.mkv_writer_open.restype = ctypes.c_void_p

        L.mkv_writer_close.argtypes = [ctypes.c_void_p]
        L.mkv_writer_close.restype = ctypes.c_int

        L.mkv_writer_set_title.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        L.mkv_writer_set_title.restype = ctypes.c_int

        L.mkv_writer_set_timestamp_scale.argtypes = [ctypes.c_void_p, ctypes.c_int64]
        L.mkv_writer_set_timestamp_scale.restype = ctypes.c_int

        L.mkv_writer_add_track.argtypes = [
            ctypes.c_void_p, ctypes.c_int,
            ctypes.POINTER(_ShimTrackInfo),
        ]
        L.mkv_writer_add_track.restype = ctypes.c_void_p

        L.mkv_writer_write_headers.argtypes = [ctypes.c_void_p]
        L.mkv_writer_write_headers.restype = ctypes.c_int

        L.mkv_writer_start_cluster.argtypes = [ctypes.c_void_p, ctypes.c_int64]
        L.mkv_writer_start_cluster.restype = ctypes.c_int

        L.mkv_writer_add_simple_block.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_char_p, ctypes.c_uint32,
            ctypes.c_int64, ctypes.c_uint32,
        ]
        L.mkv_writer_add_simple_block.restype = ctypes.c_int

        L.mkv_writer_add_block_group.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_char_p, ctypes.c_uint32,
            ctypes.c_int64, ctypes.c_int64, ctypes.c_uint32,
        ]
        L.mkv_writer_add_block_group.restype = ctypes.c_int

        L.mkv_writer_end_cluster.argtypes = [ctypes.c_void_p]
        L.mkv_writer_end_cluster.restype = ctypes.c_int

        L.mkv_writer_finalize.argtypes = [ctypes.c_void_p, ctypes.c_int64]
        L.mkv_writer_finalize.restype = ctypes.c_int

        L.mkv_writer_add_chapter.argtypes = [
            ctypes.c_void_p, ctypes.c_int64, ctypes.c_int64,
            ctypes.c_char_p, ctypes.c_char_p,
        ]
        L.mkv_writer_add_chapter.restype = ctypes.c_int

        L.mkv_writer_add_attachment.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_char_p, ctypes.c_uint32,
        ]
        L.mkv_writer_add_attachment.restype = ctypes.c_int

        L.mkv_writer_set_track_stats.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_uint64, ctypes.c_uint64, ctypes.c_int64,
        ]
        L.mkv_writer_set_track_stats.restype = ctypes.c_int

        L.mkv_writer_last_error.argtypes = []
        L.mkv_writer_last_error.restype = ctypes.c_char_p

        L.mkv_writer_version.argtypes = []
        L.mkv_writer_version.restype = ctypes.c_char_p

    # ------------------ thin Python wrappers ------------------

    def _check(self, ok_or_handle, op: str):
        """Raise RuntimeError with last_error() if a call failed."""
        if ok_or_handle in (None, 0):  # NULL pointer or non-zero return
            err = self._lib.mkv_writer_last_error()
            err_text = err.decode("utf-8", errors="replace") if err else "(no message)"
            raise RuntimeError(f"libmkv_shim {op} failed: {err_text}")
        return ok_or_handle

    def version(self) -> str:
        v = self._lib.mkv_writer_version()
        return v.decode("utf-8", errors="replace") if v else "?"

    def open(self, path: bytes, writing_app: bytes) -> int:
        h = self._lib.mkv_writer_open(path, writing_app)
        return self._check(h, "open")

    def close(self, writer: int) -> None:
        self._lib.mkv_writer_close(writer)

    def set_title(self, writer: int, title: bytes) -> None:
        rc = self._lib.mkv_writer_set_title(writer, title)
        if rc != 0:
            self._check(None, "set_title")  # always raises

    def set_timestamp_scale(self, writer: int, scale: int) -> None:
        rc = self._lib.mkv_writer_set_timestamp_scale(writer, scale)
        if rc != 0:
            self._check(None, "set_timestamp_scale")

    def add_track(self, writer: int, track_type: int,
                  info: _ShimTrackInfo) -> int:
        h = self._lib.mkv_writer_add_track(writer, track_type, ctypes.byref(info))
        return self._check(h, "add_track")

    def write_headers(self, writer: int) -> None:
        rc = self._lib.mkv_writer_write_headers(writer)
        if rc != 0:
            self._check(None, "write_headers")

    def start_cluster(self, writer: int, timecode_ns: int) -> None:
        rc = self._lib.mkv_writer_start_cluster(writer, timecode_ns)
        if rc != 0:
            self._check(None, "start_cluster")

    def add_simple_block(self, writer: int, track: int, data: bytes, size: int,
                         timecode_ns: int, flags: int) -> None:
        rc = self._lib.mkv_writer_add_simple_block(
            writer, track, data, size, timecode_ns, flags,
        )
        if rc != 0:
            self._check(None, "add_simple_block")

    def add_block_group(self, writer: int, track: int, data: bytes, size: int,
                        timecode_ns: int, duration_ns: int, flags: int) -> None:
        """Write a KaxBlockGroup with explicit BlockDuration. Use for
        subtitle events whose duration is known and differs from the
        track's DefaultDuration."""
        rc = self._lib.mkv_writer_add_block_group(
            writer, track, data, size, timecode_ns, duration_ns, flags,
        )
        if rc != 0:
            self._check(None, "add_block_group")

    def set_track_stats(self, writer: int, track: int,
                        total_bytes: int, num_frames: int,
                        total_duration_ns: int) -> None:
        """Attach per-track statistics that will be emitted as KaxTags
        at finalize. Call after all blocks for the track are added."""
        rc = self._lib.mkv_writer_set_track_stats(
            writer, track, total_bytes, num_frames, total_duration_ns,
        )
        if rc != 0:
            self._check(None, "set_track_stats")

    def end_cluster(self, writer: int) -> None:
        rc = self._lib.mkv_writer_end_cluster(writer)
        if rc != 0:
            self._check(None, "end_cluster")

    def add_chapter(self, writer: int, start_ns: int, end_ns: int,
                    name: bytes, lang: bytes = b"und") -> None:
        rc = self._lib.mkv_writer_add_chapter(writer, start_ns, end_ns, name, lang)
        if rc != 0:
            self._check(None, "add_chapter")

    def add_attachment(self, writer: int, name: bytes, mime: bytes,
                       data: bytes) -> None:
        rc = self._lib.mkv_writer_add_attachment(writer, name, mime, data, len(data))
        if rc != 0:
            self._check(None, "add_attachment")

    def finalize(self, writer: int, max_duration_ns: int) -> None:
        rc = self._lib.mkv_writer_finalize(writer, max_duration_ns)
        if rc != 0:
            self._check(None, "finalize")


__all__ = [
    "ShimStatus",
    "ShimInfo",
    "ShimUnavailable",
    "Shim",
    "get_shim_status",
    "build_shim",
    "load_shim",
    "TRACK_TYPE_VIDEO", "TRACK_TYPE_AUDIO", "TRACK_TYPE_SUBTITLE",
    "FRAME_KEYFRAME", "FRAME_CLUSTER_START", "FRAME_CHAPTER_MARK",
    "FRAME_DISCARDABLE", "FRAME_OLD_BLOCK", "FRAME_AUTO_DURATION",
    "TRACK_FLAG_DEFAULT", "TRACK_FLAG_FORCED", "TRACK_FLAG_LACING",
]
