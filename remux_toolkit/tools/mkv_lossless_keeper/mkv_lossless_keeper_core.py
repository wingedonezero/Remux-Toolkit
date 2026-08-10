# remux_toolkit/tools/mkv_lossless_keeper/mkv_lossless_keeper_core.py
"""MKV Lossless Keeper — core logic.

Removes selected lossy audio track types, and filters audio/subtitle tracks by
language, using mkvmerge (same mechanism as unchecking a track in MKVToolNix
GUI: the kept track ids are passed via --audio-tracks / --subtitle-tracks).
Output goes to the same folder with a MKVToolNix-style " (1)" suffix. Each
result is verified afterwards by re-analyzing the output file.
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass, field

from PyQt6.QtCore import QThread, pyqtSignal

MKVMERGE = "mkvmerge"

# ----------------------------------------------------------------------------
# Codec classification — audio
# ----------------------------------------------------------------------------

# Category key -> (label shown in GUI, default checked)
REMOVABLE_CATEGORIES = {
    "dts_core":  ("DTS (core / plain DTS)", True),
    "dts_hra":   ("DTS-HD High Resolution / DTS Express", True),
    "ac3":       ("AC-3 (Dolby Digital)", True),
    "eac3":      ("E-AC-3 (Dolby Digital Plus)", True),
    "aac":       ("AAC", True),
    "mp3":       ("MP3 / MP2", True),
    "opus":      ("Opus", False),
    "vorbis":    ("Vorbis", False),
}

# These are never offered for codec-based removal (the language filter can
# still exclude them).
LOSSLESS_LABEL = "TrueHD / DTS-HD MA / FLAC / PCM / ALAC (lossless — never removed by codec)"


def classify_audio(codec: str, codec_id: str) -> str | None:
    """Return the removable-category key for an audio track, or None if the
    track is lossless / unknown (never removed by codec)."""
    c = codec.lower()
    cid = (codec_id or "").upper()

    # Lossless first — these must win over substring matches below.
    if "truehd" in c or cid == "A_TRUEHD":
        return None
    if "master audio" in c:               # DTS-HD Master Audio
        return None
    if "flac" in c or cid == "A_FLAC":
        return None
    if "alac" in c or cid == "A_ALAC":
        return None
    if cid.startswith("A_PCM") or c.startswith("pcm") or "wavpack" in c:
        return None

    if cid == "A_DTS" or c.startswith("dts"):
        if "high resolution" in c or "express" in c:
            return "dts_hra"
        return "dts_core"
    if cid == "A_EAC3" or "e-ac-3" in c or "eac3" in c or "eac-3" in c:
        return "eac3"
    if cid == "A_AC3" or c == "ac-3" or c == "ac3" or "ac-3" in c:
        return "ac3"
    if cid.startswith("A_AAC") or "aac" in c:
        return "aac"
    if cid in ("A_MPEG/L3", "A_MPEG/L2") or "mp3" in c or c.startswith("mpeg"):
        return "mp3"
    if cid == "A_OPUS" or "opus" in c:
        return "opus"
    if cid == "A_VORBIS" or "vorbis" in c:
        return "vorbis"

    return None  # unknown codec: play it safe, keep it


# ----------------------------------------------------------------------------
# Codec classification — subtitles
# ----------------------------------------------------------------------------

# Category key -> (label shown in GUI, default checked). Any subtitle type not
# classified here is only affected by the language filter.
SUB_CATEGORIES = {
    "srt":    ("SRT (SubRip)", False),
    "ass":    ("SSA / ASS", False),
    "pgs":    ("PGS (Blu-ray bitmap)", False),
    "vobsub": ("VobSub (DVD bitmap)", False),
}


def classify_subtitle(codec: str, codec_id: str) -> str | None:
    """Return the sub-category key for a subtitle track, or None for any other
    type (only removable via the language filter)."""
    c = codec.lower()
    cid = (codec_id or "").upper()

    if cid == "S_TEXT/UTF8" or cid == "S_TEXT/SRT" or "subrip" in c:
        return "srt"
    if cid in ("S_TEXT/ASS", "S_TEXT/SSA", "S_ASS", "S_SSA") or "substation" in c:
        return "ass"
    if cid == "S_HDMV/PGS" or "pgs" in c:
        return "pgs"
    if cid == "S_VOBSUB" or "vobsub" in c:
        return "vobsub"
    return None


# ----------------------------------------------------------------------------
# Language matching
# ----------------------------------------------------------------------------

# Equivalence groups so "en", "eng" and "english" all match the same tracks.
# Covers ISO 639-1, 639-2/B, 639-2/T and the plain English name for common
# languages; anything not listed matches only its exact code.
_LANG_EQUIV = [
    ("en", "eng", "english"),
    ("ja", "jpn", "japanese"),
    ("es", "spa", "spanish"),
    ("fr", "fre", "fra", "french"),
    ("de", "ger", "deu", "german"),
    ("it", "ita", "italian"),
    ("pt", "por", "portuguese"),
    ("ru", "rus", "russian"),
    ("zh", "chi", "zho", "chinese"),
    ("ko", "kor", "korean"),
    ("ar", "ara", "arabic"),
    ("hi", "hin", "hindi"),
    ("nl", "dut", "nld", "dutch"),
    ("pl", "pol", "polish"),
    ("sv", "swe", "swedish"),
    ("no", "nor", "norwegian"),
    ("da", "dan", "danish"),
    ("fi", "fin", "finnish"),
    ("cs", "cze", "ces", "czech"),
    ("hu", "hun", "hungarian"),
    ("th", "tha", "thai"),
    ("tr", "tur", "turkish"),
    ("el", "gre", "ell", "greek"),
    ("he", "heb", "hebrew"),
    ("uk", "ukr", "ukrainian"),
    ("vi", "vie", "vietnamese"),
    ("id", "ind", "indonesian"),
    ("ro", "rum", "ron", "romanian"),
]

_LANG_GROUPS: dict[str, frozenset] = {}
for _group in _LANG_EQUIV:
    _fs = frozenset(_group)
    for _code in _group:
        _LANG_GROUPS[_code] = _fs


def parse_lang_list(text: str) -> list[str]:
    """Split a user-entered language list into normalized tokens."""
    return [t for t in re.split(r"[,;\s]+", (text or "").strip().lower()) if t]


def _expand_langs(codes) -> set[str]:
    out = set()
    for c in codes:
        out |= _LANG_GROUPS.get(c, {c})
    return out


def _lang_keep(track: "Track", want: set[str], keep_und: bool) -> bool:
    """True if the language filter keeps this track. `want` is the expanded
    token set; empty means the filter is off (keep everything)."""
    if not want:
        return True
    langs = set()
    tl = (track.language or "").lower()
    if tl and tl != "und":
        langs.add(tl)
    ietf = (track.lang_ietf or "").lower()
    if ietf and ietf != "und":
        langs.add(ietf)
        langs.add(ietf.split("-")[0])
    if not langs:
        return keep_und  # untagged / und track
    return bool(_expand_langs(langs) & want)


# ----------------------------------------------------------------------------
# Analysis data
# ----------------------------------------------------------------------------

@dataclass
class Track:
    tid: int
    codec: str
    codec_id: str
    language: str
    lang_ietf: str
    name: str
    category: str | None  # removable category key, or None


@dataclass
class FileAnalysis:
    path: str
    audio: list[Track] = field(default_factory=list)
    subs: list[Track] = field(default_factory=list)
    n_video: int = 0
    error: str = ""


@dataclass
class FilterSettings:
    audio_remove_cats: set = field(default_factory=set)
    audio_keep_langs: list = field(default_factory=list)   # normalized tokens
    sub_remove_cats: set = field(default_factory=set)
    sub_keep_langs: list = field(default_factory=list)     # normalized tokens
    keep_und: bool = True

    def is_noop(self) -> bool:
        return (not self.audio_remove_cats and not self.audio_keep_langs
                and not self.sub_remove_cats and not self.sub_keep_langs)


@dataclass
class FilePlan:
    keep_audio: list = field(default_factory=list)
    remove_audio: list = field(default_factory=list)
    keep_subs: list = field(default_factory=list)
    remove_subs: list = field(default_factory=list)
    skip: str | None = None


def analyze_file(path: str) -> FileAnalysis:
    fa = FileAnalysis(path=path)
    try:
        out = subprocess.run(
            [MKVMERGE, "-J", path],
            capture_output=True, text=True, timeout=120,
        )
        data = json.loads(out.stdout)
    except Exception as e:
        fa.error = f"analyze failed: {e}"
        return fa
    if data.get("errors"):
        fa.error = "; ".join(data["errors"])
        return fa
    for t in data.get("tracks", []):
        ttype = t.get("type")
        props = t.get("properties", {})
        if ttype == "video":
            fa.n_video += 1
            continue
        if ttype not in ("audio", "subtitles"):
            continue
        codec = t.get("codec", "?")
        cid = props.get("codec_id", "")
        track = Track(
            tid=t["id"],
            codec=codec,
            codec_id=cid,
            language=props.get("language", "und"),
            lang_ietf=props.get("language_ietf", ""),
            name=props.get("track_name", ""),
            category=(classify_audio(codec, cid) if ttype == "audio"
                      else classify_subtitle(codec, cid)),
        )
        (fa.audio if ttype == "audio" else fa.subs).append(track)
    return fa


def plan_for_file(fa: FileAnalysis, fs: FilterSettings) -> FilePlan:
    """Decide which tracks to keep/remove. plan.skip is None when the file
    should be processed."""
    if fa.error:
        return FilePlan(skip=f"error: {fa.error}")

    audio_want = _expand_langs(fs.audio_keep_langs)
    sub_want = _expand_langs(fs.sub_keep_langs)
    plan = FilePlan()

    for t in fa.audio:
        keep = (_lang_keep(t, audio_want, fs.keep_und)
                and t.category not in fs.audio_remove_cats)
        (plan.keep_audio if keep else plan.remove_audio).append(t)

    for t in fa.subs:
        keep = (_lang_keep(t, sub_want, fs.keep_und)
                and t.category not in fs.sub_remove_cats)
        (plan.keep_subs if keep else plan.remove_subs).append(t)

    if not plan.remove_audio and not plan.remove_subs:
        plan.skip = "no tracks match the filters — nothing to remove"
    elif fa.audio and not plan.keep_audio:
        plan.skip = "would remove ALL audio tracks — skipped for safety"
    return plan


def next_output_path(src: str) -> str:
    """MKVToolNix-style: 'Name (1).mkv' in the same folder, counting up."""
    folder = os.path.dirname(src)
    base, ext = os.path.splitext(os.path.basename(src))
    n = 1
    while True:
        candidate = os.path.join(folder, f"{base} ({n}){ext}")
        if not os.path.exists(candidate):
            return candidate
        n += 1


def build_command(fa: FileAnalysis, plan: FilePlan, out_path: str) -> list[str]:
    """mkvmerge command for the plan. Track selection flags are only passed
    for track types that actually lose tracks."""
    cmd = [MKVMERGE, "--gui-mode", "-o", out_path]
    if plan.remove_audio:
        cmd += ["--audio-tracks", ",".join(str(t.tid) for t in plan.keep_audio)]
    if plan.remove_subs:
        if plan.keep_subs:
            cmd += ["--subtitle-tracks", ",".join(str(t.tid) for t in plan.keep_subs)]
        else:
            cmd += ["--no-subtitles"]
    cmd.append(fa.path)
    return cmd


def verify_output(src_fa: FileAnalysis, out_path: str, plan: FilePlan) -> list[str]:
    """Re-analyze the output and return a list of problems (empty = OK)."""
    if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        return ["output file missing or empty"]
    out_fa = analyze_file(out_path)
    if out_fa.error:
        return [f"could not re-analyze output: {out_fa.error}"]

    problems = []
    if out_fa.n_video != src_fa.n_video:
        problems.append(f"video track count changed ({src_fa.n_video} -> {out_fa.n_video})")

    for kind, expected, got in (("audio", plan.keep_audio, out_fa.audio),
                                ("subtitle", plan.keep_subs, out_fa.subs)):
        if len(got) != len(expected):
            problems.append(f"{kind} track count is {len(got)}, expected {len(expected)}")
            continue
        for exp, act in zip(expected, got):
            if exp.codec != act.codec:
                problems.append(f"kept {kind} track mismatch: expected {exp.codec}, found {act.codec}")
    return problems


# ----------------------------------------------------------------------------
# Worker threads
# ----------------------------------------------------------------------------

class AnalyzeWorker(QThread):
    file_done = pyqtSignal(object)          # FileAnalysis
    progress = pyqtSignal(int, int)
    finished_all = pyqtSignal()

    def __init__(self, paths):
        super().__init__()
        self.paths = paths
        self._abort = False

    def abort(self):
        self._abort = True

    def run(self):
        for i, p in enumerate(self.paths, 1):
            if self._abort:
                break
            self.file_done.emit(analyze_file(p))
            self.progress.emit(i, len(self.paths))
        self.finished_all.emit()


class ProcessWorker(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)         # overall: files done, total
    file_progress = pyqtSignal(int, str)    # current file: percent, filename
    finished_all = pyqtSignal(list)         # list of (path, status, detail)

    def __init__(self, analyses, filters: FilterSettings):
        super().__init__()
        self.analyses = analyses
        self.filters = filters
        self._abort = False
        self._proc = None

    def abort(self):
        self._abort = True
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def run(self):
        results = []
        total = len(self.analyses)
        for i, fa in enumerate(self.analyses, 1):
            if self._abort:
                results.append((fa.path, "skipped", "aborted by user"))
                continue
            name = os.path.basename(fa.path)
            plan = plan_for_file(fa, self.filters)
            if plan.skip is not None:
                self.log.emit(f"SKIP  {name}: {plan.skip}")
                results.append((fa.path, "skipped", plan.skip))
                self.progress.emit(i, total)
                continue

            out_path = next_output_path(fa.path)
            removed_parts = []
            if plan.remove_audio:
                removed_parts.append("audio: " + ", ".join(
                    f"{t.codec} (id {t.tid})" for t in plan.remove_audio))
            if plan.remove_subs:
                removed_parts.append("subs: " + ", ".join(
                    f"{t.codec} {t.language} (id {t.tid})" for t in plan.remove_subs))
            removed_desc = "; ".join(removed_parts)
            self.log.emit(f"MUX   {name}: removing {removed_desc}")
            self.log.emit(f"      -> {os.path.basename(out_path)}")

            # --gui-mode is what the MKVToolNix GUI itself uses: progress and
            # warnings/errors arrive as parseable "#GUI#..." lines.
            cmd = build_command(fa, plan, out_path)
            self.file_progress.emit(0, name)
            errors, warnings = [], []
            try:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                              stderr=subprocess.STDOUT, text=True)
                for raw in self._proc.stdout:
                    for line in raw.replace("\r", "\n").split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        m = re.match(r"#GUI#progress\s+(\d+)%", line)
                        if m:
                            self.file_progress.emit(int(m.group(1)), name)
                        elif line.startswith("#GUI#warning"):
                            w = line[len("#GUI#warning"):].strip()
                            warnings.append(w)
                            self.log.emit(f"      WARNING: {w}")
                        elif line.startswith("#GUI#error"):
                            e = line[len("#GUI#error"):].strip()
                            errors.append(e)
                            self.log.emit(f"      ERROR: {e}")
                        elif line.startswith("#GUI#"):
                            pass  # other GUI-mode markers (begin/end etc.)
                        else:
                            self.log.emit(f"      {line}")
                rc = self._proc.wait()
            except Exception as e:
                results.append((fa.path, "error", f"mkvmerge failed to run: {e}"))
                self.progress.emit(i, total)
                continue
            finally:
                self._proc = None

            if self._abort:
                if os.path.exists(out_path):
                    try:
                        os.remove(out_path)
                    except OSError:
                        pass
                results.append((fa.path, "skipped", "aborted by user"))
                self.progress.emit(i, total)
                continue

            if rc == 2:
                detail = "mkvmerge error: " + (" | ".join(errors) or "unknown error")
                self.log.emit(f"ERROR {name}: {detail}")
                if os.path.exists(out_path):
                    try:
                        os.remove(out_path)
                    except OSError:
                        pass
                results.append((fa.path, "error", detail))
                self.progress.emit(i, total)
                continue

            self.file_progress.emit(100, name)
            warn = f" ({len(warnings)} mkvmerge warning(s))" if rc == 1 or warnings else ""

            problems = verify_output(fa, out_path, plan)
            if problems:
                detail = "VERIFY FAILED: " + "; ".join(problems)
                self.log.emit(f"FAIL  {name}: {detail}")
                results.append((fa.path, "verify-failed", detail))
            else:
                n_removed = len(plan.remove_audio) + len(plan.remove_subs)
                detail = (f"removed {n_removed} track(s) [{removed_desc}], "
                          f"kept {len(plan.keep_audio)} audio / {len(plan.keep_subs)} subs, "
                          f"verified OK{warn} -> {os.path.basename(out_path)}")
                self.log.emit(f"OK    {name}: verified{warn}")
                results.append((fa.path, "success", detail))
            self.progress.emit(i, total)
        self.finished_all.emit(results)
