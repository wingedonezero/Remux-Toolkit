"""
EIA-608 closed-caption extraction via the CCExtractor system binary.

NTSC DVDs embed closed captions in the MPEG-2 video stream's user_data
extension (line 21 captions, per ATSC A/53). VideoAttr.line21_cc_1 /
line21_cc_2 in libdvdread flag whether the disc declares CCs available.

This module:
    1. Detects CC presence via the VTSI VideoAttr.
    2. Runs the system ``ccextractor`` binary on a ripped MKV to produce
       an SRT file.
    3. Merges the SRT back into the MKV via ``mkvmerge``.

MakeMKV bundles its own copy of CCExtractor (in ``mmccextr/``); we use
whatever ``ccextractor`` resolves on the system PATH. Verified against
0.96.5.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def have_ccextractor() -> bool:
    """True iff ``ccextractor`` is on PATH."""
    return shutil.which("ccextractor") is not None


def have_mkvmerge() -> bool:
    """True iff ``mkvmerge`` is on PATH."""
    return shutil.which("mkvmerge") is not None


def detect_cc_available(disc, vts_no: int) -> bool:
    """Return True iff the VTSI VideoAttr flags at least one CC field.

    VideoAttr.line21_cc_1: field 1 (the primary CC channel CC1+CC2)
    VideoAttr.line21_cc_2: field 2 (CC3+CC4, less commonly used)
    """
    from ...bindings import libdvdread as dr
    with dr.open_ifo(disc, vts_no) as vts:
        va = vts.contents.vtsi_mat.contents.vts_video_attr
        return bool(int(va.line21_cc_1) or int(va.line21_cc_2))


# ---------------------------------------------------------------------------
# CCExtractor invocation
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class CCExtractResult:
    success: bool
    srt_path: Optional[Path] = None
    bytes_written: int = 0
    error: str = ""


def extract_ccs_to_srt(mkv_path: Path, srt_path: Path,
                       *, prefer_teletext: bool = False,
                       timeout_s: float = 120.0) -> CCExtractResult:
    """Run ``ccextractor`` on an MKV; produce an SRT.

    ``prefer_teletext=True`` tells ccextractor to read teletext-style CCs
    (PAL DVDs). Default False = NTSC line-21 EIA-608. When the preferred
    mode produces nothing, we automatically fall back to the other one
    so callers don't need to know the disc's geographic origin.

    Returns ``CCExtractResult`` with ``success=True`` only when the SRT
    exists and has non-trivial content (≥ 32 bytes). On many discs the
    VideoAttr declares CC support but the actual stream is empty —
    we treat that as a no-op (success=False, no error).
    """
    if not have_ccextractor():
        return CCExtractResult(success=False, error="ccextractor not on PATH")
    if not mkv_path.exists():
        return CCExtractResult(success=False, error=f"input missing: {mkv_path}")

    def _try(args: list[str]) -> tuple[int, str]:
        cmd = ["ccextractor", str(mkv_path), "-o", str(srt_path), "-quiet"] + args
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return -1, "timed out"
        except OSError as e:
            return -1, f"ccextractor failed: {e}"
        return proc.returncode, proc.stderr[:500] if proc.returncode else ""

    # Primary attempt — teletext first if preferred, else default (EIA-608).
    primary_args = ["--codec", "teletext"] if prefer_teletext else []
    rc, err = _try(primary_args)
    if rc < 0:
        return CCExtractResult(success=False, error=err)
    if rc != 0:
        return CCExtractResult(
            success=False, error=f"ccextractor rc={rc}: {err}",
        )

    if (not srt_path.exists()) or srt_path.stat().st_size < 32:
        # Primary mode produced nothing; flip the mode and try once more.
        try:
            if srt_path.exists():
                srt_path.unlink()
        except OSError:
            pass
        fallback_args = ([] if prefer_teletext else ["--codec", "teletext"])
        rc, err = _try(fallback_args)
        if rc < 0:
            return CCExtractResult(success=False, error=err)
        if rc != 0:
            return CCExtractResult(
                success=False, error=f"ccextractor (fallback) rc={rc}: {err}",
            )

    if (not srt_path.exists()) or srt_path.stat().st_size < 32:
        return CCExtractResult(
            success=False, srt_path=srt_path, bytes_written=0,
            error="no CCs found in stream (tried both EIA-608 + teletext)",
        )
    return CCExtractResult(
        success=True, srt_path=srt_path,
        bytes_written=srt_path.stat().st_size,
    )


# ---------------------------------------------------------------------------
# mkvmerge: append SRT to MKV
# ---------------------------------------------------------------------------

def merge_srt_into_mkv(mkv_path: Path, srt_path: Path,
                       *, lang: str = "eng",
                       track_name: str = "Closed Captions",
                       timeout_s: float = 300.0) -> bool:
    """Use ``mkvmerge`` to append the SRT as a new subtitle track to the
    MKV. Operation is via a side-file then atomic replace — we never
    overwrite the original until mkvmerge has succeeded.

    Returns True on success, False on any error.
    """
    if not have_mkvmerge():
        _logger.warning("mkvmerge not on PATH; can't merge SRT into MKV")
        return False
    if not srt_path.exists() or srt_path.stat().st_size == 0:
        return False

    tmp_out = mkv_path.with_suffix(mkv_path.suffix + ".cc.tmp")
    cmd = [
        "mkvmerge", "-q", "-o", str(tmp_out),
        str(mkv_path),
        "--language", f"0:{lang}",
        "--track-name", f"0:{track_name}",
        str(srt_path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        _logger.warning("mkvmerge merge of CC SRT timed out")
        return False
    except OSError as e:
        _logger.warning("mkvmerge merge failed: %s", e)
        return False

    if proc.returncode != 0:
        _logger.warning("mkvmerge rc=%d: %s", proc.returncode, proc.stderr[:500])
        try:
            tmp_out.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    # Atomic replace: rename tmp over the original.
    tmp_out.replace(mkv_path)
    return True


# ---------------------------------------------------------------------------
# Composite: detect → extract → merge
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class CCAddResult:
    """Outcome of the full closed-captions pipeline for one MKV."""
    cc_declared: bool = False         # VideoAttr flagged CC presence
    extracted: bool = False           # ccextractor produced a non-empty SRT
    merged: bool = False              # SRT successfully merged into MKV
    srt_bytes: int = 0
    error: str = ""


def add_closed_captions_to_rip(mkv_path: Path, disc, vts_no: int,
                               *, lang: str = "eng",
                               srt_path: Optional[Path] = None,
                               keep_srt: bool = False) -> CCAddResult:
    """Full pipeline: detect declared CCs, extract via ccextractor, merge
    into the MKV. Idempotent — if the disc declares no CCs the pipeline
    is a no-op (returns ``cc_declared=False``, all other fields default).

    Automatically selects EIA-608 (NTSC) vs teletext (PAL) based on
    the disc's ``vts_video_attr.video_format`` (0 = NTSC, 1 = PAL); the
    other mode is tried as a fallback if the preferred one is empty.

    ``srt_path`` defaults to ``mkv_path.with_suffix('.cc.srt')``.
    When ``keep_srt=False`` (default), the intermediate SRT is removed
    after a successful merge.
    """
    out = CCAddResult()
    out.cc_declared = detect_cc_available(disc, vts_no)
    if not out.cc_declared:
        return out

    if srt_path is None:
        srt_path = mkv_path.with_suffix(".cc.srt")

    # PAL discs typically use teletext-style CCs; NTSC uses line-21
    # EIA-608. The extractor falls back automatically so this is a hint,
    # not a hard switch.
    from ...bindings import libdvdread as dr
    with dr.open_ifo(disc, vts_no) as vts:
        is_pal = int(vts.contents.vtsi_mat.contents.vts_video_attr.video_format) == 1
    ext = extract_ccs_to_srt(mkv_path, srt_path, prefer_teletext=is_pal)
    out.srt_bytes = ext.bytes_written
    if not ext.success:
        out.error = ext.error
        # Clean up an empty SRT file if ccextractor created one.
        if srt_path.exists() and srt_path.stat().st_size < 32:
            try:
                srt_path.unlink()
            except OSError:
                pass
        return out
    out.extracted = True

    out.merged = merge_srt_into_mkv(mkv_path, srt_path, lang=lang)
    if not out.merged:
        out.error = "mkvmerge failed"

    if not keep_srt and srt_path.exists():
        try:
            srt_path.unlink()
        except OSError:
            pass
    return out


__all__ = [
    "have_ccextractor",
    "have_mkvmerge",
    "detect_cc_available",
    "CCExtractResult",
    "extract_ccs_to_srt",
    "merge_srt_into_mkv",
    "CCAddResult",
    "add_closed_captions_to_rip",
]
