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

from . import disc_open_enumerate as _doe
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


def _evaluate_title_for_analyzer(
    title: dict,
    vts: dict,
    disc_state: Optional[_doe.DiscState] = None,
) -> _pf.EvaluatorResult:
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
        disc_state=disc_state,
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

# MakeMKV-equivalent codec labels per stream kind. Used to populate the
# ``makemkv_codec`` parity field alongside our ffmpeg-style ``codec``. The
# captured MakeMKV ``mmcon_titles.json`` for the corpus shows these exact
# strings in the ``codec`` column.
_MAKEMKV_VIDEO_CODEC = {
    "mpeg1": "Mpeg1",
    "mpeg2": "Mpeg2",
}
_MAKEMKV_AUDIO_CODEC = {
    "ac3":      "DD",        # Dolby Digital
    "mpeg1":    "MP2",       # MPEG-1 Layer II audio (audio_format=2)
    "mpeg2ext": "MP2",       # MPEG-2 ext (audio_format=3); MakeMKV displays MP2
    "lpcm":     "PCM",       # Linear PCM
    "sdds":     "SDDS",      # rare
    "dts":      "DTS",
}
# MakeMKV displays DVD subpicture as an empty codec string (only the
# language is shown). Closed captions are labelled "CC" in MakeMKV's output.
_MAKEMKV_SUB_CODEC = {
    "dvd_subtitle": "",
    "eia_608":      "CC",
}

# ISO 639-1 → ISO 639-2 (bibliographic) lookup. MakeMKV emits 639-2.
# Cover the languages seen across the corpus + common ones; unknowns
# fall back to the original 2-letter code (so we never silently drop
# information).
_ISO639_1_TO_2 = {
    "en": "eng", "fr": "fre", "es": "spa", "de": "ger", "it": "ita",
    "ja": "jpn", "ko": "kor", "zh": "chi", "ru": "rus", "pt": "por",
    "nl": "dut", "sv": "swe", "no": "nor", "da": "dan", "fi": "fin",
    "pl": "pol", "tr": "tur", "ar": "ara", "he": "heb", "hi": "hin",
    "th": "tha", "vi": "vie", "el": "gre", "cs": "cze", "hu": "hun",
    "ro": "rum", "uk": "ukr", "bg": "bul", "sk": "slo", "sl": "slv",
    "ms": "may", "id": "ind",
}


def _makemkv_lang(lang: str) -> str:
    """Match MakeMKV's language emission: ISO 639-2 (3-letter) when the
    code resolves, empty string for 'und'/unknown."""
    if not lang or lang.lower() == "und":
        return ""
    iso2 = _ISO639_1_TO_2.get(lang.lower())
    if iso2:
        return iso2
    # Already 3-letter or non-standard — pass through.
    return lang.lower()


def _to_gui_streams(title: dict) -> list[dict]:
    """Flatten audio + subtitle streams into the GUI's expected list format.
    Video is synthesized as the first stream from the inspector's video dict.

    Each stream carries both our internal codec name (``codec``, ffmpeg-style
    e.g. ``ac3``/``mpeg1``/``dvd_subtitle``) AND a MakeMKV-equivalent label
    (``makemkv_codec``: ``DD``/``MP2``/``""``) for cross-validation parity.
    Same for language: ``language`` is ISO 639-1 (2-letter) with ``"und"``
    for unknown, ``makemkv_language`` is ISO 639-2 (3-letter) with ``""``
    for unknown — matching MakeMKV's emit.
    """
    video = title.get("video") or {}
    video_codec = video.get("codec", "")
    streams: list[dict] = [{
        "index": 0,
        "kind": "Video",
        "codec": video_codec,
        "makemkv_codec": _MAKEMKV_VIDEO_CODEC.get(video_codec, video_codec),
        "language": "",
        "makemkv_language": "",
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
        codec = s.get("codec", "")
        lang = s.get("language", "")
        streams.append({
            "index": idx,
            "kind": "Audio",
            "codec": codec,
            "makemkv_codec": _MAKEMKV_AUDIO_CODEC.get(codec, codec),
            "language": lang,
            "makemkv_language": _makemkv_lang(lang),
            "channels": chans,
            "channel_layout": f"{chans}ch" if chans else "",
            "sample_rate": 48000 if s.get("sample_rate") == "48kHz" else (96000 if s.get("sample_rate") == "96kHz" else 0),
            "width": 0,
            "height": 0,
        })
        idx += 1
    for s in title.get("subtitle_streams", []):
        lang = s.get("language", "")
        streams.append({
            "index": idx,
            "kind": "Subtitles",
            "codec": "dvd_subtitle",
            "makemkv_codec": _MAKEMKV_SUB_CODEC["dvd_subtitle"],  # ""
            "language": lang,
            "makemkv_language": _makemkv_lang(lang),
            "channels": 0,
            "channel_layout": "",
            "sample_rate": 0,
            "width": 0,
            "height": 0,
        })
        idx += 1
    # MakeMKV emits a single "CC" subtitle stream when the IFO declares
    # line21 captions, regardless of whether one or both of line21_cc_1 /
    # line21_cc_2 are flagged. Our ``closed_captions`` list may carry both
    # CC1 and CC2 channels; collapse them into a single track for the GUI
    # streams view (the channel detail stays available via the
    # ``closed_caption_channels`` field on the title).
    ccs = title.get("closed_captions", []) or []
    if ccs:
        primary = ccs[0]
        all_channels = ",".join(cc["channel"] for cc in ccs)
        streams.append({
            "index": idx,
            "kind": "Subtitles",
            "codec": "eia_608",
            "makemkv_codec": _MAKEMKV_SUB_CODEC["eia_608"],  # "CC"
            "language": "eng",
            "makemkv_language": "eng",
            "channels": 0,
            "channel_layout": "",
            "sample_rate": 0,
            "width": 0,
            "height": 0,
            "extra": {
                "cc_channel": primary["channel"],
                # Surface every CC channel the IFO declared so consumers
                # can rip multi-channel CC data even though MakeMKV's
                # display only lists one stream.
                "cc_channels": all_channels,
            },
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
    consumes (matches `title_info_to_dict` from ffmpeg_parser.py).

    Duration field semantics:
      * ``duration_seconds`` — MakeMKV-equivalent integer seconds via
        ``sum(int(cell.duration_s))``. Matches MakeMKV's MSG:3028 / title
        list display. Set by ``analyze()`` as
        ``duration_seconds_int_cell_sum`` and surfaced here.
      * ``duration_seconds_pgc`` — full-precision ``pgc.playback_time``
        from the IFO. Use this when sub-second accuracy matters.
      * ``duration`` — ``H:MM:SS`` rendering of the MakeMKV-equivalent
        integer seconds.
    """
    pgc_dur = float(title.get("duration_seconds", 0) or 0)
    mm_dur = int(title.get("duration_seconds_int_cell_sum", pgc_dur))
    video = title.get("video") or {}
    return {
        "title_num":            title["title"],
        "duration":             _seconds_to_hms(mm_dur),
        "duration_seconds":     mm_dur,
        "duration_seconds_pgc": round(pgc_dur, 3),
        # MakeMKV reports post-trim chapter count; we mirror that on
        # ``chapters``. The pre-trim count (from tt_srpt) is kept on
        # ``chapters_raw`` for callers that need the IFO-declared value.
        "chapters":         title.get(
            "num_chapters_post_trim", title.get("num_chapters", 0)),
        "chapters_raw":     title.get("num_chapters", 0),
        "video_codec":      (video.get("codec") or "").upper(),
        "audio_count":      len(title.get("audio_streams", [])),
        # MakeMKV emits a single CC subtitle stream per title regardless
        # of whether one or both line21 fields are flagged, so the count
        # treats closed_captions as "1 if any, else 0" — matches
        # MakeMKV's mmcon title-list ``subtitle_count``.
        "subtitle_count":   len(title.get("subtitle_streams", []))
                            + (1 if title.get("closed_captions") else 0),
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
# Phantom-stream scan (lazy, per "added" title) — Group C
# ---------------------------------------------------------------------------

def _run_phantom_scan_for_title(dvd, title: dict) -> None:
    """Mirror of FUN_006e3240's per-title placement of the stream-scan
    orchestrator. Runs the IFO-vs-VOB phantom-stream filter on the
    title's PGC, then mutates ``title["audio_streams"]`` to drop slots
    that the VOB didn't deliver. Emits MSG:3034 per dropped slot.

    Called only for titles that survived the silent-drop gate
    (matches MakeMKV: stream scan runs post-init / post-cellwalk /
    pre-MSG:3028). For silently-dropped titles the scan never runs,
    so no MSG:3034 fires — matching MakeMKV's emit pattern.

    Subtitle phantom detection is gathered for diagnostics but NOT
    applied — DVD subs are sparse PES (one per signage card) and a
    short scan window misses legit late-appearing subs. The full
    diagnostic stays available via ``stream_presence`` directly.
    """
    from . import stream_presence as _sp
    from . import mkv_msg_log

    vts = title.get("vts")
    pgc = title.get("pgc")
    if not vts or not pgc:
        return
    n_audio = len(title.get("audio_streams", []))
    n_sub = len(title.get("subtitle_streams", []))
    if n_audio == 0 and n_sub == 0:
        return
    try:
        rep = _sp.detect_phantom_streams(dvd, vts, pgc)
    except Exception:
        # Best-effort; leave the title's stream list as the IFO
        # declared on scan failure (matches MakeMKV's fail-open behaviour
        # in stream_scan_orchestrator's error paths).
        return
    title["stream_presence"] = _sp.report_to_dict(rep)
    if not rep.missing_audio_indices:
        return
    # Filter declared audio streams down to those the VOB delivered.
    active_audio = title.get("active_audio_slots") or list(range(n_audio))
    a_missing = set(rep.missing_audio_indices)
    kept_audio = []
    dropped_slots: list[int] = []
    for i, s in enumerate(title.get("audio_streams", [])):
        slot = active_audio[i] if i < len(active_audio) else i
        if slot in a_missing:
            dropped_slots.append(slot)
            continue
        kept_audio.append(s)
    title["audio_streams"] = kept_audio
    title["phantoms_dropped"] = {
        "audio": len(rep.missing_audio_indices),
        "subtitle_diagnostic": len(rep.missing_subp_indices),
    }
    tid = title.get("title") or 0
    for slot in dropped_slots:
        mkv_msg_log.emit(3034, slot, tid,
                          title=tid, vts=vts,
                          reason="phantom-stream-filter")


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def analyze(report: dict, *, dvd_path: Optional[str] = None) -> dict:
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

    When ``dvd_path`` is provided (or derivable from
    ``report['source_path']``) the analyzer runs the phantom-stream
    scan for each title that survives the silent-drop gate — mirroring
    MakeMKV's ``FUN_006e3240`` stream_scan_orchestrator placement
    (post-init, post-cellwalk, pre-MSG:3028-emit). Without a disc
    handle the scan is skipped (audio counts then reflect the raw IFO
    declaration, matching ``inspect_disc(filter_phantom_streams=False)``
    pre-Group C behaviour).
    """
    from . import mkv_msg_log
    from . import stream_presence as _sp
    from ...bindings import libdvdread as _dr

    raw_titles = report.get("titles", [])
    title_sets = report.get("title_sets", [])
    vts_by_no = {ts.get("vts"): ts for ts in title_sets}

    # Pre-compute the MakeMKV-equivalent display duration per title:
    # sum(int(cell.duration_s)) AFTER applying trim — what MakeMKV
    # prints in MSG:3028 "Title #N was added (M cell(s), H:MM:SS)" and
    # in its title list JSON. Our analyzer also exposes the
    # full-precision pgc.playback_time via duration_seconds_pgc.
    #
    # Group A1: also compute post-trim chapter count by counting the
    # PTTs whose first program's first cell falls within the kept cell
    # range. MakeMKV's chapter count reflects the trimmed title.
    from . import cell_trim as _ct
    from .title_pre_filter import (
        _cell_dict_to_meta as _cell_to_meta,
        _annotate_block_ranges as _annot,
        _DictPgcProxy as _PgcProxy,
    )
    for t in raw_titles:
        pgc_no = t.get("pgc")
        vts_no = t.get("vts")
        vts = vts_by_no.get(vts_no, {}) or {}
        cells = []
        pgc_dict = None
        for p in vts.get("pgcs", []):
            if p.get("pgc") == pgc_no:
                cells = p.get("cells") or []
                pgc_dict = p
                break
        if not cells:
            t["duration_seconds_int_cell_sum"] = int(
                t.get("duration_seconds", 0) or 0)
            t["num_chapters_post_trim"] = int(t.get("num_chapters", 0) or 0)
            continue

        # Run the trim deciders to get the kept cell range.
        cells_meta = [_cell_to_meta(c) for c in cells]
        _annot(cells_meta)
        pgc_proxy = _PgcProxy(pgc_dict or {})
        try:
            trim = _ct.decide_trim(cells_meta, pgc_proxy)
        except Exception:
            trim = _ct.TrimDecision()
        start_trim = trim.start_trim
        end_trim = trim.end_trim
        n = len(cells)
        kept = cells[start_trim:n - end_trim] if (start_trim or end_trim) else cells
        if not kept:
            kept = cells

        # MakeMKV-equivalent duration = sum of int(cell.duration) over
        # the post-trim kept range.
        t["duration_seconds_int_cell_sum"] = sum(
            int(c.get("duration_seconds", 0) or 0) for c in kept
        )

        # Post-trim chapter count: count PTTs whose program's first cell
        # falls within the kept range. Uses the PGC's program_map (now
        # exposed by inspector — entry N is the 1-based cell number
        # where program N starts) for correct cell mapping on
        # compilation-pattern PGCs (DRAGONAUT_P2 T31 is the canonical
        # case: 44 programs distributed unevenly across 100 cells, where
        # MakeMKV reports 7 chapters because only programs 38-44 start
        # in the kept cell range 93-99).
        vts_ttn = int(t.get("vts_ttn") or 0)
        ptts = (vts.get("ptt_map", {}) or {}).get(vts_ttn, [])
        program_map = (pgc_dict or {}).get("program_map", []) or []
        if (start_trim or end_trim) and ptts and program_map:
            kept_first_cell = start_trim + 1  # 1-based cell index
            kept_last_cell = n - end_trim     # 1-based inclusive
            kept_ptts = 0
            for ptt in ptts:
                if int(ptt.get("pgc", 0)) != pgc_no:
                    continue
                prog = int(ptt.get("program", 1))  # 1-based
                if 1 <= prog <= len(program_map):
                    prog_first_cell = program_map[prog - 1]
                    if kept_first_cell <= prog_first_cell <= kept_last_cell:
                        kept_ptts += 1
            t["num_chapters_post_trim"] = kept_ptts or 1
        else:
            t["num_chapters_post_trim"] = int(t.get("num_chapters", 0) or 0)

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

    # Resolve disc path for the phantom scan. Source priority:
    #   1. Explicit ``dvd_path`` kwarg.
    #   2. ``report['source_path']`` (set by inspector).
    # If neither is present / openable, the scan is skipped.
    resolved_path = dvd_path or report.get("source_path") or ""

    dvd_handle = None
    if resolved_path:
        try:
            # ``open_disc`` is the context-manager wrapper around
            # DVDOpen2 with the silent-logger callback. We can't
            # ``with``-block it across the analyze loop body, so call
            # the underlying open directly and close in the ``finally``.
            import ctypes as _ctypes
            dvd_handle = _dr._lib.DVDOpen2(
                None, _ctypes.byref(_dr._SILENT_LOGGER),
                resolved_path.encode("utf-8"),
            )
            if not dvd_handle:
                dvd_handle = None
        except Exception:
            dvd_handle = None

    # Group F (F.1): build the disc-enumeration state once per
    # analyze() call. Threaded into title_pre_filter / cellwalk_primary
    # so the IF/ELSE split + iVar30 selection can become data-driven.
    # F.1 returns an empty DiscState (matches pre-Group-F defaults);
    # F.2-F.5 progressively fill in the state vectors.
    disc_state = _doe.disc_open_enumerate(
        resolved_path or None,
        dvd_handle=dvd_handle,
        report=report,
    )

    try:
        # First pass: run the FUN_007ec6f0 port for each title. MSG:3009/
        # 3010/3011/3012/3015/3016/3025/3026/3028/3040/3041 are emitted
        # inside evaluate_title (and its callees) when the corresponding
        # gate fires; we record the verdict + the GUI classification.
        for t in raw_titles:
            vts = vts_by_no.get(t.get("vts"), {}) or {}
            verdict = _evaluate_title_for_analyzer(t, vts, disc_state=disc_state)
            t["evaluator_verdict"] = verdict.classification
            t["evaluator_msg_code"] = verdict.msg_code
            t["evaluator_reason"] = verdict.reason
            t["evaluator_trace"] = verdict.trace
            if verdict.classification in ("fake_title", "silent",
                                          "skipped_init", "skipped_nav",
                                          "skipped_short"):
                t["classification"] = "fake_title"
                # MakeMKV doesn't run the stream scan for silently
                # dropped titles (FUN_006e3240 only runs after the
                # silent-drop gate passes). We mirror that — no scan,
                # no MSG:3034.
            else:
                t["classification"] = _classify_secondary(t)
                # MSG:3034 — phantom-stream scan. Only runs for titles
                # that pass the silent-drop gate (matches MakeMKV's
                # post-gate placement of FUN_006e3240).
                if dvd_handle is not None:
                    _run_phantom_scan_for_title(dvd_handle, t)
    finally:
        if dvd_handle is not None:
            _dr._lib.DVDClose(dvd_handle)

    # Strict-equality dedup.
    dup_map = _find_strict_duplicates(raw_titles)
    for t in raw_titles:
        if t["title"] in dup_map:
            survivor, basis = dup_map[t["title"]]
            t["duplicate_of"] = survivor
            t["duplicate_basis"] = basis
            # MSG:3027 — title duplicate-of-another. MakeMKV's format is
            # "Title %3 in VTS %1 is equal to title %2 and was skipped"
            # so render order is (duplicate_title, vts, survivor_title);
            # the original implementation passed args in MakeMKV's
            # positional order (vts, survivor, duplicate) which produced
            # incorrect output text. Pass render order to match.
            mkv_msg_log.emit(3027, t["title"], t.get("vts") or 0, survivor,
                             title=t["title"], duplicate_of=survivor,
                             vts=t.get("vts") or 0)

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
