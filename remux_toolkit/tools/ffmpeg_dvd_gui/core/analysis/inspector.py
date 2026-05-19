"""
DVD inspector — opens a disc with libdvdread and emits a deterministic JSON
description of its IFO structure: volume info, VTS layout, per-title video /
audio / subtitle attributes, PGC timing, and (optionally) per-cell sector
ranges.

The output is intentionally schema'd so downstream tools (the analyzer,
the demuxer, the rip orchestrator) can consume it without re-parsing IFOs.

CLI:
    python -m remux_toolkit.tools.ffmpeg_dvd_gui.core.analysis.inspector \\
        /path/to/VIDEO_TS_or_folder_or.iso [--no-cells] [--output report.json]
        [--compare-makemkv [--makemkv-bin makemkvcon]]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from ...bindings import libdvdread as dr
from ...utils.paths import find_dvd_roots_with_structure
from . import ifo_validate as ifov


SCHEMA = "remux-toolkit/dvd-inspector/v1"


# ---------------------------------------------------------------------------
# Disc → dict
# ---------------------------------------------------------------------------

def inspect_disc(path: str | Path, *, include_cells: bool = True,
                 filter_phantom_streams: bool = False) -> dict:
    """
    All IFO-derived values must be copied out of libdvdread's struct memory
    *before* the corresponding ifoClose() runs, otherwise we read freed memory.

    ``filter_phantom_streams``: when True, the inspector scans each title's
    VOB sectors (up to ~8 MB per title) and drops audio / subtitle streams
    that the IFO declares but the VOB never delivers. This is what MakeMKV
    does — its audio/sub counts reflect the post-scan reality, not the
    pre-scan IFO declaration. Default False for speed (typical use); the
    cross-validation harness sets True.
    """
    path = str(path)
    css_state = ifov.detect_css(path)
    with dr.open_disc(path) as dvd:
        vol_id, vol_set = dr.get_volume_info(dvd)
        disc_id = dr.get_disc_id(dvd)

        # Probe + validate VMG before we read its contents — captures
        # main-vs-BUP divergence and impossible counts. Always runs;
        # cheap (≤2 sector reads).
        vmg_report = ifov.inspect_ifo_source(dvd, 0, is_vmg=True)

        with dr.open_ifo(dvd, 0) as vmg:
            vmgi = vmg.contents.vmgi_mat.contents
            tt_srpt = vmg.contents.tt_srpt.contents
            num_vts = int(vmgi.vmg_nr_of_title_sets)
            num_titles = int(tt_srpt.nr_of_srpts)

            vmg_info = {
                "identifier": bytes(vmgi.vmg_identifier).decode("latin-1", errors="replace").strip("\x00 "),
                "specification_version": int(vmgi.specification_version),
                "num_title_sets": num_vts,
                "num_titles": num_titles,
                "provider_identifier": bytes(vmgi.provider_identifier).decode("latin-1", errors="replace").strip("\x00 "),
                "first_play_pgc_offset": int(vmgi.first_play_pgc),
                "vmg_category": int(vmgi.vmg_category),
                "ifo_validation": ifov.report_to_dict(vmg_report),
            }

            raw_titles = [
                {
                    "title": i + 1,
                    "vts": int(tt_srpt.title[i].title_set_nr),
                    "vts_ttn": int(tt_srpt.title[i].vts_ttn),
                    "num_angles": int(tt_srpt.title[i].nr_of_angles),
                    "num_chapters": int(tt_srpt.title[i].nr_of_ptts),
                    "parental_id": int(tt_srpt.title[i].parental_id),
                }
                for i in range(num_titles)
            ]

        title_sets: list[dict] = []
        vts_reports: list[ifov.IfoReport] = []
        for vts_no in range(1, num_vts + 1):
            vts_report = ifov.inspect_ifo_source(dvd, vts_no, is_vmg=False)
            vts_reports.append(vts_report)
            try:
                with dr.open_ifo(dvd, vts_no) as vts_ifo:
                    d = _vts_to_dict(vts_no, vts_ifo, include_cells=include_cells)
                    d["ifo_validation"] = ifov.report_to_dict(vts_report)
                    title_sets.append(d)
            except dr.DvdReadError as e:
                title_sets.append({
                    "vts": vts_no,
                    "error": str(e),
                    "ifo_validation": ifov.report_to_dict(vts_report),
                })

        titles = [_resolve_title(t, title_sets) for t in raw_titles]

        if filter_phantom_streams:
            from . import stream_presence as sp
            _apply_phantom_filter_to_titles(dvd, titles)

        all_reports = [vmg_report, *vts_reports]
        ifo_summary = {
            "main_used": sum(1 for r in all_reports
                             if r.probe.effective_source == "main"),
            "bup_fallback": sum(1 for r in all_reports
                                if r.probe.effective_source == "bup"),
            "missing": sum(1 for r in all_reports
                           if r.probe.effective_source == "missing"),
            "diverged": sum(1 for r in all_reports
                            if r.probe.content_matches is False),
            "errors": sum(1 for r in all_reports
                          for i in r.issues if i.severity == "error"),
            "warnings": sum(1 for r in all_reports
                            for i in r.issues if i.severity == "warn"),
        }

        return {
            "schema": SCHEMA,
            "source_path": path,
            "volume_id": vol_id,
            "volume_set_id_hex": vol_set.hex() if vol_set else "",
            "disc_id_md5": disc_id,
            "css": css_state,
            "vmg": vmg_info,
            "title_sets": title_sets,
            "titles": titles,
            "ifo_summary": ifo_summary,
        }


def _vts_to_dict(vts_no: int, vts_ifo, *, include_cells: bool) -> dict:
    m = vts_ifo.contents.vtsi_mat.contents

    audio = [dr.audio_attr_to_dict(m.vts_audio_attr[i])
             for i in range(m.nr_of_vts_audio_streams)]
    subp = [dr.subp_attr_to_dict(m.vts_subp_attr[i])
            for i in range(m.nr_of_vts_subp_streams)]

    pgcs = []
    if vts_ifo.contents.vts_pgcit:
        pgcit = vts_ifo.contents.vts_pgcit.contents
        for i in range(pgcit.nr_of_pgci_srp):
            srp = pgcit.pgci_srp[i]
            if not srp.pgc:
                continue
            pgcs.append(_pgc_to_dict(i + 1, srp.pgc.contents, include_cells=include_cells))

    ptt_by_title: dict[int, list[dict]] = {}
    if vts_ifo.contents.vts_ptt_srpt:
        ptt_srpt = vts_ifo.contents.vts_ptt_srpt.contents
        for ti in range(ptt_srpt.nr_of_srpts):
            ttu = ptt_srpt.title[ti]
            entries = []
            for pi in range(ttu.nr_of_ptts):
                entries.append({
                    "ptt": pi + 1,
                    "pgc": int(ttu.ptt[pi].pgcn),
                    "program": int(ttu.ptt[pi].pgn),
                })
            ptt_by_title[ti + 1] = entries

    return {
        "vts": vts_no,
        "identifier": m.vts_identifier.decode("latin-1", errors="replace").strip(),
        "specification_version": int(m.specification_version),
        "video": dr.video_attr_to_dict(m.vts_video_attr),
        "audio_streams": audio,
        "subtitle_streams": subp,
        "pgcs": pgcs,
        "ptt_map": ptt_by_title,
    }


def _pgc_to_dict(pgc_idx: int, pgc, *, include_cells: bool) -> dict:
    # Always walk the cells to compute the sum-of-cells duration — that number
    # is what ffmpeg/MakeMKV report and what the eventual MKV's MediaInfo will
    # show. PGC.playback_time is a single field set at authoring time and
    # routinely runs longer than the actual cell sum on TV-series discs.
    cell_sum_s = 0.0
    cells_data: list[dict] = []
    if pgc.cell_playback:
        for ci in range(pgc.nr_of_cells):
            cp = pgc.cell_playback[ci]
            cell_secs = cp.playback_time.total_seconds
            cell_sum_s += cell_secs
            if include_cells:
                cells_data.append({
                    "cell": ci + 1,
                    "first_sector": int(cp.first_sector),
                    "last_sector": int(cp.last_sector),
                    "duration_seconds": round(cell_secs, 3),
                    "block_type": int(cp.block_type),
                    "block_mode": int(cp.block_mode),
                    "seamless_play": bool(cp.seamless_play),
                    "interleaved": bool(cp.interleaved),
                    "stc_discontinuity": bool(cp.stc_discontinuity),
                    "seamless_angle": bool(cp.seamless_angle),
                    "still_time": int(cp.still_time),
                    "cell_type": int(cp.cell_type),
                })

    # Active stream masks: top bit set means the stream is active for this PGC.
    # audio_control entries are uint16 (top bit = 0x8000), subp_control are
    # uint32 (top bit = 0x80000000). These let us emit per-title stream lists
    # that match ffprobe/MakeMKV instead of the raw VTS-declared count.
    active_audio = [i for i in range(8) if pgc.audio_control[i] & 0x8000]
    active_subp  = [i for i in range(32) if pgc.subp_control[i] & 0x80000000]

    out = {
        "pgc": pgc_idx,
        "num_programs": int(pgc.nr_of_programs),
        "num_cells": int(pgc.nr_of_cells),
        "duration_seconds": round(pgc.playback_time.total_seconds, 3),
        "duration_seconds_cell_sum": round(cell_sum_s, 3),
        "frame_rate": round(pgc.playback_time.frame_rate, 3),
        "active_audio_stream_indices": active_audio,
        "active_subtitle_stream_indices": active_subp,
    }
    if include_cells:
        out["cells"] = cells_data
    return out


def _apply_phantom_filter_to_titles(dvd, titles: list[dict]) -> None:
    """Mutates each title in-place to drop AUDIO streams that the VOB
    doesn't actually carry. Called when ``inspect_disc`` was invoked with
    ``filter_phantom_streams=True``.

    Why audio-only:
      * Audio PES are dense (every ~100 ms on a typical 256-kbps AC3
        track) — if we scan 32 MB and don't see audio for slot N, that
        slot is genuinely missing. Confidence is high.
      * Subtitle PES are sparse — one per signage card / dialogue. A
        sparse sub may first appear minutes into a long title. We'd
        need to scan the entire VOB to be sure, which is too slow for
        the inspector. Instead the phantom report is surfaced as
        diagnostic metadata and the rip pipeline can choose whether to
        act on it.

    Slow path: opens each title's VTS VOBs and scans up to 32 MB. Skip
    titles with zero declared audio (no point scanning).
    """
    from . import stream_presence as sp

    for t in titles:
        vts = t.get("vts")
        pgc = t.get("pgc")
        if not vts or not pgc:
            continue
        n_audio = len(t.get("audio_streams", []))
        n_sub = len(t.get("subtitle_streams", []))
        if n_audio == 0 and n_sub == 0:
            continue
        try:
            rep = sp.detect_phantom_streams(dvd, vts, pgc)
        except Exception:
            # Phantom detection is best-effort; on failure, leave the
            # title's stream lists as the IFO declared them.
            continue
        t["stream_presence"] = sp.report_to_dict(rep)
        if not rep.missing_audio_indices:
            continue
        # Audio-only filter: drop tracks for slots not seen in the
        # post-scan observed set.
        active_audio = t.get("active_audio_slots") or list(range(n_audio))
        a_missing = set(rep.missing_audio_indices)
        kept_audio = []
        for i, s in enumerate(t.get("audio_streams", [])):
            slot = active_audio[i] if i < len(active_audio) else i
            if slot in a_missing:
                continue
            kept_audio.append(s)
        t["audio_streams"] = kept_audio
        t["phantoms_dropped"] = {
            "audio": len(rep.missing_audio_indices),
            "subtitle_diagnostic": len(rep.missing_subp_indices),
        }


def _resolve_title(title: dict, title_sets: list[dict]) -> dict:
    """Inline VTS metadata + PGC timing onto each title row, filtering streams
    by the PGC's active audio/subp masks. The VTS declares M streams; the PGC
    selects a subset for that title — only those should appear in `audio_streams`
    / `subtitle_streams`. The full declared lists are kept under
    `vts_declared_audio` / `vts_declared_subtitle` for diagnostics."""
    vts_dict = next((ts for ts in title_sets if ts.get("vts") == title["vts"]), None)
    out = dict(title)
    if not vts_dict or "error" in vts_dict:
        return out

    out["video"] = vts_dict.get("video")
    declared_audio = vts_dict.get("audio_streams", [])
    declared_subp  = vts_dict.get("subtitle_streams", [])

    out["audio_streams"] = list(declared_audio)
    out["subtitle_streams"] = list(declared_subp)
    out["vts_declared_audio_count"] = len(declared_audio)
    out["vts_declared_subtitle_count"] = len(declared_subp)

    # Closed captions are encoded into the MPEG-2 video's line21 scanlines,
    # not as separate subpicture streams. They are extractable at demux time
    # (FFmpeg with `-c:s mov_text` does this); MakeMKV always extracts them.
    # We surface the availability so the ripper can choose.
    video = vts_dict.get("video") or {}
    out["closed_captions"] = []
    if video.get("line21_cc_1"):
        out["closed_captions"].append({"channel": "CC1", "source": "line21_field1"})
    if video.get("line21_cc_2"):
        out["closed_captions"].append({"channel": "CC2", "source": "line21_field2"})

    ptt_entries = vts_dict.get("ptt_map", {}).get(title["vts_ttn"], [])
    if ptt_entries:
        first_pgc = ptt_entries[0]["pgc"]
        pgc = next((p for p in vts_dict.get("pgcs", []) if p.get("pgc") == first_pgc), None)
        if pgc:
            out["pgc"] = pgc["pgc"]
            out["duration_seconds"] = pgc["duration_seconds"]
            out["duration_seconds_cell_sum"] = pgc.get("duration_seconds_cell_sum")
            out["num_cells"] = pgc["num_cells"]
            out["frame_rate"] = pgc["frame_rate"]
            # Prefer PGC.nr_of_programs over tt_srpt.nr_of_ptts for the
            # chapter count: PTT entries can be repeated pointers to the
            # same program (ANGEL T5 declares 2 PTTs, PGC has 1 program).
            # MakeMKV reports nr_of_programs as the chapter count, and
            # mkvmerge / players show one chapter per program.
            if pgc.get("num_programs") is not None:
                out["num_chapters"] = pgc["num_programs"]

            active_audio_idx = pgc.get("active_audio_stream_indices", [])
            active_subp_idx  = pgc.get("active_subtitle_stream_indices", [])
            # Mask indices may exceed declared count for malformed discs;
            # only keep ones backed by an actual declared stream attribute.
            out["audio_streams"] = [declared_audio[i] for i in active_audio_idx
                                    if i < len(declared_audio)]
            out["subtitle_streams"] = [declared_subp[i] for i in active_subp_idx
                                       if i < len(declared_subp)]
            # Preserve the slot indices in IFO order so the phantom-stream
            # filter can map back to the declared-slot index used by the
            # VOB scan (which uses 0xBD+0x80+slot for audio, 0x20+slot
            # for subs).
            out["active_audio_slots"] = [i for i in active_audio_idx
                                         if i < len(declared_audio)]
            out["active_subp_slots"] = [i for i in active_subp_idx
                                        if i < len(declared_subp)]
    return out


# ---------------------------------------------------------------------------
# makemkvcon comparison
# ---------------------------------------------------------------------------

def _run_makemkvcon_info(disc_path: str, makemkv_bin: str = "makemkvcon",
                         minlength: int = 0) -> dict:
    """Run `makemkvcon -r --noscan info` and parse its robot-readable output.
    Passes `--minlength=0` by default so MakeMKV surfaces every title regardless
    of the user's persisted preference — comparison data should be complete.
    The user's installed config is not modified."""
    src_arg = _makemkv_src_arg(disc_path)
    proc = subprocess.run(
        [makemkv_bin, f"--minlength={minlength}", "-r", "--noscan", "info", src_arg],
        capture_output=True, text=True, timeout=120,
    )
    # makemkvcon may exit non-zero even when info is valid; trust stdout.
    return _parse_makemkv_robot(proc.stdout)


def _makemkv_src_arg(disc_path: str) -> str:
    p = Path(disc_path)
    if p.is_file() and p.suffix.lower() in {".iso", ".img"}:
        return f"iso:{p.as_posix()}"
    # Folder containing VIDEO_TS (or VIDEO_TS itself)
    if p.name == "VIDEO_TS":
        p = p.parent
    return f"file:{p.as_posix()}"


_ROBOT_LINE_RE = re.compile(r"^(\w+):(.+)$")


def _split_csv(s: str) -> list[str]:
    """Split makemkv's CSV-like format, respecting quoted strings."""
    out: list[str] = []
    buf: list[str] = []
    in_q = False
    i = 0
    while i < len(s):
        c = s[i]
        if c == '"':
            in_q = not in_q
        elif c == "," and not in_q:
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    out.append("".join(buf))
    return [t.strip().strip('"') for t in out]


# MakeMKV attribute IDs (from makemkvgui/inc/lgpl/apdefs.h, ap_ItemAttributeId).
_AP_TYPE              = 1
_AP_NAME              = 2
_AP_LANG_CODE         = 3
_AP_LANG_NAME         = 4
_AP_CODEC_ID          = 5
_AP_CODEC_SHORT       = 6
_AP_CODEC_LONG        = 7
_AP_CHAPTER_COUNT     = 8
_AP_DURATION          = 9
_AP_DISK_SIZE         = 10
_AP_DISK_SIZE_BYTES   = 11
# Attribute 24 is the libdvdread title number — lets us match 1:1 against
# our index without content-based fuzzy matching.
_AP_ORIGINAL_TITLE_ID = 24
_AP_SEGMENTS_COUNT    = 25
_AP_SEGMENTS_MAP      = 26
_AP_OUTPUT_FILE_NAME  = 27
_AP_COMMENT           = 49


def _parse_makemkv_robot(text: str) -> dict:
    """
    Parse makemkvcon's -r robot output. Returns:
      {
        "disc_name": str,
        "titles": [
          {"title": int, "duration_seconds": float, "chapters": int,
           "streams": [{"track": int, "type_id": int, ...}]}
        ]
      }
    """
    out: dict = {"disc_name": "", "titles": []}
    titles: dict[int, dict] = {}

    for line in text.splitlines():
        m = _ROBOT_LINE_RE.match(line)
        if not m:
            continue
        tag, rest = m.group(1), m.group(2)
        cols = _split_csv(rest)

        if tag == "CINFO":
            # CINFO:id,code,value
            attr_id = int(cols[0])
            value = cols[2] if len(cols) >= 3 else ""
            if attr_id == _AP_NAME:
                out["disc_name"] = value
        elif tag == "TINFO":
            # TINFO:title,id,code,value
            t_idx = int(cols[0])
            attr_id = int(cols[1])
            value = cols[3] if len(cols) >= 4 else ""
            t = titles.setdefault(t_idx, {"title": t_idx, "streams": []})
            if attr_id == _AP_DURATION:
                t["duration_seconds"] = _hms_to_seconds(value)
            elif attr_id == _AP_CHAPTER_COUNT:
                t["chapters"] = int(value) if value.isdigit() else 0
            elif attr_id == _AP_NAME:
                t["name"] = value
            elif attr_id == _AP_ORIGINAL_TITLE_ID:
                # "01"-padded libdvdread title number; lets us match directly.
                try:
                    t["original_title_id"] = int(value.lstrip("0") or "0")
                except ValueError:
                    pass
            elif attr_id == _AP_COMMENT:
                t["comment"] = value
            elif attr_id == _AP_SEGMENTS_MAP:
                t["segments_map"] = value
            elif attr_id == _AP_OUTPUT_FILE_NAME:
                t["output_filename"] = value
        elif tag == "SINFO":
            # SINFO:title,track,id,code,value
            t_idx = int(cols[0])
            tr_idx = int(cols[1])
            attr_id = int(cols[2])
            value = cols[4] if len(cols) >= 5 else ""
            t = titles.setdefault(t_idx, {"title": t_idx, "streams": []})
            stream = next((s for s in t["streams"] if s["track"] == tr_idx), None)
            if stream is None:
                stream = {"track": tr_idx}
                t["streams"].append(stream)
            if attr_id == _AP_TYPE:
                stream["type"] = value
            elif attr_id == _AP_CODEC_SHORT:
                stream["codec"] = value
            elif attr_id == _AP_LANG_CODE:
                stream["language"] = value

    for t in titles.values():
        t["audio_count"] = sum(1 for s in t["streams"] if s.get("type") == "Audio")
        t["subtitle_count"] = sum(1 for s in t["streams"] if s.get("type") == "Subtitles")
    out["titles"] = [titles[k] for k in sorted(titles)]
    return out


def _hms_to_seconds(s: str) -> float:
    parts = s.split(":")
    if len(parts) != 3:
        return 0.0
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return 0.0


def compare_against_makemkv(report: dict, mkv: dict) -> dict:
    """Match our titles to MakeMKV's using `ap_iaOriginalTitleId` (attr 24),
    which is the libdvdread title number — so the mapping is exact and 1:1,
    not a content-based heuristic. Titles MakeMKV's brain dropped (deduped or
    skipped) end up in `only_in_ours`; titles only MakeMKV sees (shouldn't
    happen for DVD) go in `only_in_makemkv`."""
    our_by_id = {t["title"]: t for t in report["titles"]}

    mkv_by_id: dict[int, dict] = {}
    mkv_without_orig: list[dict] = []
    for t in mkv["titles"]:
        oid = t.get("original_title_id")
        if oid is not None:
            mkv_by_id[oid] = t
        else:
            mkv_without_orig.append(t)

    matched = []
    dropped_by_mkv = []
    for our_id, ours in our_by_id.items():
        if our_id in mkv_by_id:
            theirs = mkv_by_id[our_id]
            our_dur = round(ours.get("duration_seconds", 0), 1)
            mkv_dur = round(theirs.get("duration_seconds", 0), 1)
            matched.append({
                "title": our_id,
                "our_duration": our_dur,
                "mkv_duration": mkv_dur,
                "duration_delta": round(mkv_dur - our_dur, 1),
                "our_chapters": ours["num_chapters"],
                "mkv_chapters": theirs.get("chapters", 0),
                "our_audio_count": len(ours.get("audio_streams", [])),
                "mkv_audio_count": theirs.get("audio_count", 0),
                "our_subtitle_count": len(ours.get("subtitle_streams", [])),
                "mkv_subtitle_count": theirs.get("subtitle_count", 0),
                "mkv_comment": theirs.get("comment", ""),
                "mkv_output_filename": theirs.get("output_filename", ""),
            })
        else:
            dropped_by_mkv.append({
                "title": our_id,
                "vts": ours["vts"],
                "vts_ttn": ours["vts_ttn"],
                "duration_seconds": round(ours.get("duration_seconds", 0), 1),
                "chapters": ours["num_chapters"],
            })

    return {
        "our_title_count": len(our_by_id),
        "mkv_title_count": len(mkv["titles"]),
        "mkv_disc_name": mkv.get("disc_name", ""),
        "matched": matched,
        "dropped_by_makemkv": dropped_by_mkv,
        "mkv_titles_without_original_id": mkv_without_orig,
    }


# ---------------------------------------------------------------------------
# ffprobe (dvdvideo demuxer) comparison
# ---------------------------------------------------------------------------

def _run_ffprobe_titles(disc_path: str, ffprobe_bin: str = "ffprobe",
                        max_titles: int = 99) -> list[dict]:
    """For each title FFmpeg's dvdvideo demuxer is willing to open, return:
       {title, duration_seconds, chapters, audio_count, subtitle_count}.

       FFmpeg's number is the authoritative "what the rip output will look
       like" reference — it walks the actual cell chain and decodes the
       stream's frame durations.
    """
    # FFmpeg's dvdvideo demuxer wants the disc *parent* (folder containing
    # VIDEO_TS) or the ISO file directly.
    p = Path(disc_path)
    if p.name == "VIDEO_TS":
        p = p.parent
    input_arg = str(p)

    results: list[dict] = []
    consecutive_failures = 0
    for tn in range(1, max_titles + 1):
        cmd = [
            ffprobe_bin, "-f", "dvdvideo", "-title", str(tn),
            "-v", "error", "-show_format", "-show_streams", "-show_chapters",
            "-print_format", "json", "-i", input_arg,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            consecutive_failures += 1
            if consecutive_failures >= 3:
                break
            continue
        if proc.returncode != 0 or not proc.stdout.strip():
            consecutive_failures += 1
            if consecutive_failures >= 3 and len(results) >= 1:
                # We've found titles; assume we've fallen off the end.
                break
            if consecutive_failures >= 10:
                break
            continue
        consecutive_failures = 0
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            continue

        fmt = data.get("format", {})
        try:
            duration = float(fmt.get("duration", "0"))
        except ValueError:
            duration = 0.0

        streams = data.get("streams", [])
        chapters = data.get("chapters", [])
        a_count = sum(1 for s in streams if s.get("codec_type") == "audio")
        s_count = sum(1 for s in streams if s.get("codec_type") == "subtitle")

        results.append({
            "title": tn,
            "duration_seconds": round(duration, 3),
            "chapters": len(chapters),
            "audio_count": a_count,
            "subtitle_count": s_count,
        })
    return results


def compare_against_ffprobe(report: dict, ff_titles: list[dict]) -> dict:
    """Direct 1:1 match by title number. Both ffprobe `-f dvdvideo -title N`
    and we use the same libdvdread title numbering, so there's no ambiguity —
    ffprobe title N == our title N. Mismatches here would indicate a real bug."""
    ff_by_id = {t["title"]: t for t in ff_titles}
    matched = []
    only_ours = []
    for ours in report["titles"]:
        tn = ours["title"]
        theirs = ff_by_id.get(tn)
        our_pgc = round(ours.get("duration_seconds", 0), 1)
        our_cs  = round(ours.get("duration_seconds_cell_sum") or 0, 1)
        if theirs is None:
            only_ours.append({
                "title": tn,
                "duration_seconds": our_pgc,
                "reason": "ffprobe failed to open this title (often: zero-stream or zero-duration title)",
            })
            continue
        ff_dur = round(theirs["duration_seconds"], 1)
        matched.append({
            "title": tn,
            "our_pgc_duration": our_pgc,
            "our_cell_sum":     our_cs,
            "ff_duration":      ff_dur,
            "pgc_vs_ff_delta":  round(ff_dur - our_pgc, 1),
            "cellsum_vs_ff_delta": round(ff_dur - our_cs, 1) if our_cs else None,
            "our_audio_count":    len(ours.get("audio_streams", [])),
            "ff_audio_count":     theirs.get("audio_count", 0),
            "our_subtitle_count": len(ours.get("subtitle_streams", [])),
            "ff_subtitle_count":  theirs.get("subtitle_count", 0),
            "our_chapters": ours["num_chapters"],
            "ff_chapters":  theirs.get("chapters", 0),
        })
    only_ff = [t for t in ff_titles if t["title"] not in {m["title"] for m in matched}]
    return {
        "our_title_count": len(report["titles"]),
        "ff_title_count":  len(ff_titles),
        "matched":         matched,
        "only_in_ours":    only_ours,
        "only_in_ffmpeg":  only_ff,
    }


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _resolve_disc_path(src: Path) -> Optional[Path | list[Path]]:
    """Normalize a user-supplied path to something libdvdread can open.

    Accepts:
      * an ISO file
      * a folder containing VIDEO_TS/
      * the VIDEO_TS folder itself
      * a folder containing the VTS files directly (no VIDEO_TS subdir)
        — libdvdread supports this layout, some rips put files at root
      * a parent folder containing one or more discs in subdirectories

    Returns a single Path, a list of Paths if multiple discs found, or None.
    """
    if src.is_file():
        return src  # assume ISO
    if not src.is_dir():
        return None
    if src.name == "VIDEO_TS":
        return src.parent
    if (src / "VIDEO_TS").is_dir():
        return src
    # libdvdread also accepts the VTS files at the folder root (no VIDEO_TS
    # subdir). Detect by presence of any VIDEO_TS.IFO or VTS_*_*.IFO file.
    has_vts_files = any(
        p.is_file() and (p.name.upper() == "VIDEO_TS.IFO"
                         or (p.name.upper().startswith("VTS_") and p.name.upper().endswith(".IFO")))
        for p in src.iterdir()
    )
    if has_vts_files:
        return src
    discs = find_dvd_roots_with_structure(src)
    if not discs:
        return None
    if len(discs) == 1:
        d = discs[0].disc_path
        return d.parent if d.name == "VIDEO_TS" else d
    return [d.disc_path for d in discs]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="dvd-inspector",
        description="Dump a deterministic JSON description of a DVD's IFO structure.",
    )
    ap.add_argument("path", help="Path to a VIDEO_TS folder, parent folder, or .iso file")
    ap.add_argument("--no-cells", action="store_true",
                    help="Omit per-cell sector ranges (smaller output)")
    ap.add_argument("--output", "-o", help="Write JSON to file (default: stdout)")
    ap.add_argument("--compare-makemkv", action="store_true",
                    help="Also run makemkvcon info and emit a diff")
    ap.add_argument("--makemkv-bin", default="makemkvcon",
                    help="Path to makemkvcon binary (default: PATH lookup)")
    ap.add_argument("--compare-ffprobe", action="store_true",
                    help="Also run ffprobe -f dvdvideo on every title and emit a diff")
    ap.add_argument("--ffprobe-bin", default="ffprobe",
                    help="Path to ffprobe binary (default: PATH lookup)")
    args = ap.parse_args(argv)

    src = Path(args.path)
    if not src.exists():
        print(f"error: path does not exist: {src}", file=sys.stderr)
        return 2

    # libdvdread takes a VIDEO_TS folder, its parent, or an ISO. If the user
    # pointed us at a wrapper directory like 'Beast Season/Disc 01/VIDEO_TS',
    # auto-discover the disc — but require unambiguous resolution.
    resolved = _resolve_disc_path(src)
    if resolved is None:
        print(f"error: no VIDEO_TS or ISO found under {src}", file=sys.stderr)
        return 2
    if isinstance(resolved, list):
        print(f"error: multiple discs found under {src}; specify one explicitly:",
              file=sys.stderr)
        for p in resolved:
            print(f"  {p}", file=sys.stderr)
        return 2
    src = resolved

    try:
        report = inspect_disc(src, include_cells=not args.no_cells)
    except dr.DvdReadError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.compare_makemkv:
        try:
            mkv = _run_makemkvcon_info(str(src), args.makemkv_bin)
            report["makemkv_comparison"] = compare_against_makemkv(report, mkv)
            report["makemkv_raw_titles"] = mkv["titles"]
        except FileNotFoundError:
            report["makemkv_comparison"] = {"error": f"{args.makemkv_bin} not found in PATH"}
        except subprocess.TimeoutExpired:
            report["makemkv_comparison"] = {"error": "makemkvcon timed out"}

    if args.compare_ffprobe:
        try:
            ff = _run_ffprobe_titles(str(src), args.ffprobe_bin)
            report["ffprobe_comparison"] = compare_against_ffprobe(report, ff)
            report["ffprobe_raw_titles"] = ff
        except FileNotFoundError:
            report["ffprobe_comparison"] = {"error": f"{args.ffprobe_bin} not found in PATH"}

    out_json = json.dumps(report, indent=2, sort_keys=True)

    if args.output:
        Path(args.output).write_text(out_json + "\n")
    else:
        print(out_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
