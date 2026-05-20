"""
Analyzer brain — consumes the inspector's JSON, applies MakeMKV-style
curation, and emits a structure the GUI can consume. Drops nothing; every
title remains in the output. Curation decisions are surfaced as flags so the
GUI can hide / collapse but never lose data.

Two important design rules from the project owner:

  1. **Strict deduplication.** Two titles are duplicates only if they reference
     the exact same content — same VTS, same PGC, same first/last cell sector
     ranges, same active audio mask, same active subtitle mask. Anything less
     is treated as distinct, even if duration matches. The reasoning: a user
     who wants "the version with commentary" and "the version without" must
     never have one silently disappear.

  2. **Never exclude.** Duplicates get `duplicate_of` pointing at the survivor;
     short fillers get `hidden_by_default = True`. Both stay in the output.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from . import title_pre_filter as _pf


SCHEMA = "remux-toolkit/dvd-analyzer/v1"

# Title length thresholds (seconds) used purely for default-visibility, not
# for exclusion. A user can always show hidden titles.
SHORT_TITLE_S = 60.0   # below this, considered "short" — usually shown
FILLER_TITLE_S = 5.0   # below this, considered filler — hidden by default
                       # when audio+sub counts are also zero
COMPILATION_CHAPTER_RATIO = 2.0  # if T_A.chapters >= 2× T_B.chapters AND T_A
                                  # contains T_B's cell range, T_A is flagged
                                  # as a compilation containing T_B


_trace_log = logging.getLogger("remux_toolkit.analyzer.title_eval")


# ---------------------------------------------------------------------------
# Title classification — delegates fully to title_pre_filter
# (the port of FUN_007ef130 + FUN_007ed1f0 + FUN_007ec6f0). ``analyze()``
# requires the inspector's full ``title_sets`` list to run the PTT/PGC
# walk; reports without that context cause an explicit error rather
# than falling back to a heuristic.
# ---------------------------------------------------------------------------


def _classify_secondary(title: dict) -> str:
    """Pick the GUI display bucket when ``evaluate_title`` classifies a
    title as "added". The four buckets — ``feature`` / ``short`` /
    ``stub`` / ``filler`` — are visibility groupings only; they don't
    affect whether the title is ripped.
    """
    duration = title.get("duration_seconds", 0) or 0
    audio_count = len(title.get("audio_streams", []))
    sub_count = len(title.get("subtitle_streams", []))

    if duration < FILLER_TITLE_S and audio_count == 0 and sub_count == 0:
        return "filler"
    if duration < FILLER_TITLE_S:
        return "stub"
    if duration < SHORT_TITLE_S:
        return "short"
    return "feature"


def _evaluate_title_for_analyzer(title: dict, vts: dict) -> _pf.EvaluatorResult:
    """Run title_pre_filter.evaluate_title with cellwalk-equivalent
    inputs derived from the inspector dict.

    The cellwalk gate uses ``cellwalk_would_keep_any_cells`` (a
    duration-based proxy for cell_trim's content predicate). This
    matches MakeMKV's per-corpus behavior:

      - Jack T3/T4 (0.501s, single-cell): no cells survive → silent
        because VTS has audio.
      - TERRA T8 (1.501s, single-cell): same.
      - Condor T2/T3 (7-10s, single-cell): cell survives (>= 5s) → added.
      - ANGEL T5 (7.5s, single-cell): cell survives → added.
      - DRAGONAUT T5 / ANGEL T6 (0.501s, single-cell): no cells survive +
        no audio in VTS → MSG:3026 fake.
    """
    # Collect the PGC's cells (if include_cells was on in inspector).
    pgcn = title.get("pgc")
    cells: list[dict] = []
    if vts and pgcn is not None:
        for p in vts.get("pgcs", []):
            if p.get("pgc") == pgcn:
                cells = p.get("cells") or []
                break

    # Mirror cell_trim's cellwalk verdict (FUN_007f3eb0 → method[0x10]
    # → FUN_007f3e30): 0 cells "survive" when no cell passes the
    # byzantine content predicate (cell_trim.cell_is_content_byzantine,
    # the port of cell_validator_primary). If we don't have cell-level
    # data (inspector run without include_cells), fall back to
    # num_cells (be permissive — over-report MSG:3028 rather than
    # silent-drop real content).
    if cells:
        has_content = _pf.cellwalk_keeps_any_cells(cells)
        cells_after_trim = sum(1 for _ in cells) if has_content else 0
    else:
        cells_after_trim = int(title.get("num_cells") or 0)

    actual = title.get("duration_seconds_cell_sum")
    if actual is None:
        actual = title.get("duration_seconds")

    result = _pf.evaluate_title(
        title, vts,
        cells_after_trim=cells_after_trim,
        actual_duration_after_trim_s=float(actual or 0.0),
    )
    _trace_log.debug(
        "title=%d vts=%s pgc=%s %s",
        title.get("title"), title.get("vts"), title.get("pgc"),
        result.trace,
    )
    return result


# ---------------------------------------------------------------------------
# Strict deduplication
# ---------------------------------------------------------------------------

def _dedupe_key(title: dict) -> Optional[tuple]:
    """The strict-equality fingerprint. Two titles with the same key reference
    the exact same on-disc content with the exact same active stream set.
    Returns None if the title is missing fields we need (e.g. unresolved
    error in inspector output) — such titles never participate in dedup.
    """
    pgc = title.get("pgc")
    if pgc is None:
        return None

    audio_langs = tuple(
        (s.get("codec"), s.get("language"), s.get("channels"))
        for s in title.get("audio_streams", [])
    )
    sub_langs = tuple(
        (s.get("type"), s.get("language"))
        for s in title.get("subtitle_streams", [])
    )

    # Cell sector ranges only available if the inspector ran with include_cells.
    # Fall back to (num_cells, num_programs) which is still discriminating but
    # less strict. Either way we tag what the dedup decision was based on.
    cell_ranges: tuple
    cell_basis: str
    cells = title.get("cells")
    if cells:
        cell_ranges = tuple((int(c["first_sector"]), int(c["last_sector"])) for c in cells)
        cell_basis = "sectors"
    else:
        cell_ranges = (int(title.get("num_cells", 0)),)
        cell_basis = "cell_count"

    key = (
        title["vts"],
        pgc,
        round(title.get("duration_seconds", 0), 1),
        title.get("num_chapters"),
        audio_langs,
        sub_langs,
        cell_ranges,
    )
    return key, cell_basis


def _find_strict_duplicates(titles: list[dict]) -> dict[int, tuple[int, str]]:
    """Returns {duplicate_title_num: (survivor_title_num, dedup_basis)}.

    The "survivor" is the title with the lowest title number among the
    duplicate group — keeps behavior deterministic and stable across runs.
    Titles in the result are duplicates of their entry's first element."""
    by_key: dict[tuple, list[tuple[int, str]]] = {}
    for t in titles:
        info = _dedupe_key(t)
        if info is None:
            continue
        key, basis = info
        by_key.setdefault(key, []).append((t["title"], basis))

    duplicates: dict[int, tuple[int, str]] = {}
    for key, members in by_key.items():
        if len(members) <= 1:
            continue
        members.sort()  # by title number ascending
        survivor_num, basis = members[0]
        for dup_num, _ in members[1:]:
            duplicates[dup_num] = (survivor_num, basis)
    return duplicates


# ---------------------------------------------------------------------------
# Compilation detection
# ---------------------------------------------------------------------------

def _find_compilations(titles: list[dict]) -> dict[int, list[int]]:
    """Identify titles whose cell sector ranges fully contain another title's
    cell sector ranges. Returns {compilation_title_num: [contained_title_nums]}.

    Requires cell-level data; returns {} if cells absent."""
    compilations: dict[int, list[int]] = {}
    titles_with_cells = [t for t in titles if t.get("cells")]
    if not titles_with_cells:
        return compilations

    def cell_set(t: dict) -> set[int]:
        s: set[int] = set()
        for c in t["cells"]:
            s.update(range(int(c["first_sector"]), int(c["last_sector"]) + 1))
        return s

    cells_by_title = {t["title"]: cell_set(t) for t in titles_with_cells}

    for outer in titles_with_cells:
        outer_set = cells_by_title[outer["title"]]
        contained = []
        for inner in titles_with_cells:
            if inner["title"] == outer["title"]:
                continue
            inner_set = cells_by_title[inner["title"]]
            if not inner_set:
                continue
            if inner_set.issubset(outer_set) and inner_set != outer_set:
                contained.append(inner["title"])
        if contained:
            compilations[outer["title"]] = sorted(contained)
    return compilations


# ---------------------------------------------------------------------------
# Default-visibility rules
# ---------------------------------------------------------------------------

def _hidden_by_default(title: dict, *, is_duplicate: bool,
                       contained_in_compilation: bool) -> bool:
    """Conservative rule: hide ONLY titles that almost certainly aren't useful
    rip targets. Errs toward visibility. The user can always show hidden.

    Hidden:
      - `fake_title` — matches MakeMKV's silent-drop pattern (single-cell
        menu trampoline, zero declared+actual duration). Hidden by default
        because MakeMKV drops these entirely; user can still rip if they
        explicitly opt in.
      - `filler` (under 5s with zero streams) — pure padding
      - `stub` (under 5s with one or more streams) — some discs populate
        audio/sub attribute slots on what's still functionally a navigation
        placeholder; CRUSADE_DISC_1 T6 is the canonical case (0.4s with
        1 audio + 3 subs declared by the PGC). Both are equally useless
        as rip targets.
      - Anything marked `duplicate_of` another title.

    Not hidden:
      - `short` (5s–60s) — could be intro / stinger / featurette; visible,
        but the user-tunable minlength preference handles whether to
        auto-check or not.
      - Compilation members — user often wants both the play-all and the
        individual episodes available; hiding is their call.
    """
    classification = title["classification"]
    if classification in ("fake_title", "filler", "stub"):
        return True
    if is_duplicate:
        return True
    return False


# ---------------------------------------------------------------------------
# GUI dict shape (drop-in for DVDProbeWorker output)
# ---------------------------------------------------------------------------

def _to_gui_streams(title: dict) -> list[dict]:
    """Flatten audio + subtitle streams into the GUI's expected list format.
    Video is synthesized as the first stream from the inspector's video dict."""
    video = title.get("video") or {}
    streams: list[dict] = [{
        "index": 0,
        "kind": "Video",
        "codec": video.get("codec", ""),
        "language": "",
        "channels": 0,
        "channel_layout": "",
        "sample_rate": 0,
        "width": int(video.get("picture_size", "0x0").split("x")[0]) if "x" in video.get("picture_size", "") else 0,
        "height": int(video.get("picture_size", "0x0").split("x")[1]) if "x" in video.get("picture_size", "") else 0,
        "aspect_ratio": video.get("aspect_ratio", ""),
        "format": video.get("format", ""),
    }]
    idx = 1
    for s in title.get("audio_streams", []):
        chans = s.get("channels", 0)
        streams.append({
            "index": idx,
            "kind": "Audio",
            "codec": s.get("codec", ""),
            "language": s.get("language", ""),
            "channels": chans,
            "channel_layout": f"{chans}ch" if chans else "",
            "sample_rate": 48000 if s.get("sample_rate") == "48kHz" else (96000 if s.get("sample_rate") == "96kHz" else 0),
            "width": 0,
            "height": 0,
        })
        idx += 1
    for s in title.get("subtitle_streams", []):
        streams.append({
            "index": idx,
            "kind": "Subtitles",
            "codec": "dvd_subtitle",
            "language": s.get("language", ""),
            "channels": 0,
            "channel_layout": "",
            "sample_rate": 0,
            "width": 0,
            "height": 0,
        })
        idx += 1
    for cc in title.get("closed_captions", []):
        streams.append({
            "index": idx,
            "kind": "Subtitles",
            "codec": "eia_608",
            "language": "eng",
            "channels": 0,
            "channel_layout": "",
            "sample_rate": 0,
            "width": 0,
            "height": 0,
            "extra": {"cc_channel": cc["channel"]},
        })
        idx += 1
    return streams


def _hms(s: float) -> str:
    """Format seconds as ``h:mm:ss`` for MakeMKV-equivalent MSG output."""
    if s is None or s < 0:
        s = 0.0
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    return f"{h}:{m:02d}:{sec:02d}"


def _seconds_to_hms(s: float) -> str:
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    return f"{h}:{m:02d}:{sec:02d}"


def _to_gui_title(title: dict) -> dict:
    """Convert a single analyzed title into the dict shape the existing GUI
    consumes (matches `title_info_to_dict` from ffmpeg_parser.py)."""
    dur_s = title.get("duration_seconds", 0) or 0
    video = title.get("video") or {}
    return {
        "title_num":        title["title"],
        "duration":         _seconds_to_hms(dur_s),
        "duration_seconds": dur_s,
        "chapters":         title.get("num_chapters", 0),
        "video_codec":      (video.get("codec") or "").upper(),
        "audio_count":      len(title.get("audio_streams", [])),
        "subtitle_count":   len(title.get("subtitle_streams", []))
                            + len(title.get("closed_captions", [])),
        "streams":          _to_gui_streams(title),
        # --- analyzer-specific extensions ---
        "vts":                  title.get("vts"),
        "vts_ttn":              title.get("vts_ttn"),
        "pgc":                  title.get("pgc"),
        "num_cells":            title.get("num_cells", 0),
        "frame_rate":           title.get("frame_rate", 0),
        "classification":      title.get("classification"),
        "hidden_by_default":    title.get("hidden_by_default", False),
        "duplicate_of":         title.get("duplicate_of"),
        "duplicate_basis":      title.get("duplicate_basis"),
        "contains_titles":      title.get("contains_titles", []),
        "closed_caption_channels": [cc["channel"] for cc in title.get("closed_captions", [])],
    }


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def analyze(report: dict) -> dict:
    """Apply curation to an inspector report. Returns:

        {
          "schema": SCHEMA,
          "source_path": str,
          "volume_id": str,
          "disc_id_md5": str,
          "titles":       {title_num: gui_dict},
          "title_order":  [title_nums in inspector order],
          "summary": {
              "total_titles":  N,
              "shown_by_default": N,
              "duplicates":     N,
              "compilations":   N,
          },
        }
    """
    from . import mkv_msg_log

    raw_titles = report.get("titles", [])
    title_sets = report.get("title_sets", [])
    vts_by_no = {ts.get("vts"): ts for ts in title_sets}

    # ``analyze`` requires the inspector's title_sets to run the
    # FUN_007ec6f0 port (PTT/PGC walk + cellwalk gate). Without it,
    # there's no faithful answer to "is this title silent / fake /
    # added" — we error explicitly rather than fall back to a
    # heuristic.
    if not title_sets:
        raise ValueError(
            "analyze() requires inspector.title_sets — got an empty list. "
            "Build the report via inspector.inspect_disc() or supply a "
            "matching title_sets entry for each title's vts."
        )

    # First pass: run the FUN_007ec6f0 port for each title. MSG:3009/
    # 3010/3011/3012/3015/3016/3025/3026/3028/3040/3041 are emitted
    # inside evaluate_title (and its callees) when the corresponding
    # gate fires; we record the verdict + the GUI classification.
    for t in raw_titles:
        vts = vts_by_no.get(t.get("vts"), {}) or {}
        verdict = _evaluate_title_for_analyzer(t, vts)
        t["evaluator_verdict"] = verdict.classification
        t["evaluator_msg_code"] = verdict.msg_code
        t["evaluator_reason"] = verdict.reason
        t["evaluator_trace"] = verdict.trace
        if verdict.classification in ("fake_title", "silent",
                                      "skipped_init", "skipped_nav",
                                      "skipped_short"):
            t["classification"] = "fake_title"
            # Strip phantom-drop bookkeeping for silent titles —
            # MakeMKV doesn't emit MSG:3034 for them.
            t.pop("_phantom_audio_slots_dropped", None)
        else:
            t["classification"] = _classify_secondary(t)
            # MSG:3034 — emit only for added titles (MakeMKV's
            # behaviour). The inspector stages the dropped slots
            # at ``_phantom_audio_slots_dropped``; we emit + clear.
            dropped = t.pop("_phantom_audio_slots_dropped", None)
            if dropped:
                for slot in dropped:
                    mkv_msg_log.emit(3034, slot, t.get("title") or 0,
                                      title=t.get("title"),
                                      vts=t.get("vts"),
                                      reason="phantom-stream-filter")

    # Strict-equality dedup.
    dup_map = _find_strict_duplicates(raw_titles)
    for t in raw_titles:
        if t["title"] in dup_map:
            survivor, basis = dup_map[t["title"]]
            t["duplicate_of"] = survivor
            t["duplicate_basis"] = basis
            # MSG:3027 — title duplicate-of-another
            mkv_msg_log.emit(3027, t.get("vts") or 0, survivor, t["title"],
                             title=t["title"], duplicate_of=survivor)

    # Compilation detection (only if cells present).
    comp_map = _find_compilations(raw_titles)
    for t in raw_titles:
        if t["title"] in comp_map:
            t["contains_titles"] = comp_map[t["title"]]
    contained = {n for nums in comp_map.values() for n in nums}

    # Default-visibility decision.
    for t in raw_titles:
        is_dup = t.get("duplicate_of") is not None
        in_comp = t["title"] in contained
        t["hidden_by_default"] = _hidden_by_default(
            t, is_duplicate=is_dup, contained_in_compilation=in_comp,
        )

    titles_dict: dict[int, dict] = {t["title"]: _to_gui_title(t) for t in raw_titles}
    title_order = [t["title"] for t in raw_titles]

    return {
        "schema":       SCHEMA,
        "source_path":  report.get("source_path", ""),
        "volume_id":    report.get("volume_id", ""),
        "disc_id_md5":  report.get("disc_id_md5", ""),
        "titles":       titles_dict,
        "title_order":  title_order,
        "summary": {
            "total_titles":     len(raw_titles),
            "shown_by_default": sum(1 for t in raw_titles if not t.get("hidden_by_default")),
            "duplicates":       sum(1 for t in raw_titles if t.get("duplicate_of") is not None),
            "compilations":     len(comp_map),
            "fake_titles":      sum(1 for t in raw_titles
                                    if t.get("classification") == "fake_title"),
        },
    }
