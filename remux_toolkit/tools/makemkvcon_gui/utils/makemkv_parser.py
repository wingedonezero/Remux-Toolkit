# remux_toolkit/tools/makemkvcon_gui/utils/makemkv_parser.py
import re
from collections import defaultdict

def parse_label_from_info(output: str):
    m = re.search(r'CINFO:2,\d+,\d+,"([^"]+)"', output)
    return m.group(1) if m else None

def count_titles_from_info(output: str) -> int:
    titles = set()
    for line in output.splitlines():
        if line.startswith("TINFO:"):
            try:
                idx = int(line.split(":")[1].split(",")[0])
                titles.add(idx)
            except Exception:
                pass
    return len(titles)

def _format_channels(count_str: str | None) -> str | None:
    if not count_str or not count_str.isdigit():
        return None
    count = int(count_str)
    if count == 1: return "1.0 (Mono)"
    if count == 2: return "2.0 (Stereo)"
    if count == 6: return "5.1"
    if count == 8: return "7.1"
    return f"{count} Channels"

_LANG_NAMES = {
    "eng": "English", "spa": "Spanish", "fra": "French", "deu": "German",
    "ita": "Italian", "jpn": "Japanese", "zho": "Chinese", "kor": "Korean",
    "por": "Portuguese", "rus": "Russian", "und": "Undetermined",
}

def _pretty_lang_from_code(code: str):
    if not code:
        return None
    return _LANG_NAMES.get(code.lower(), code.title())

def _has(words, blob: str) -> bool:
    return any(re.search(rf"\b{re.escape(w)}\b", blob, re.IGNORECASE) for w in words)

def _codec_from_blob(kind: str, blob: str):
    if kind == "Video":
        if _has(["HEVC", "H.265", "H265"], blob): return "HEVC (H.265)"
        if _has(["AVC", "H.264", "H264"], blob): return "H.264/AVC"
        if _has(["VC-1", "VC1"], blob): return "VC-1"
        if _has(["MPEG-2", "Mpeg2", "MPEG2"], blob): return "MPEG-2"
        return "Video"
    elif kind == "Audio":
        if _has(["Atmos"], blob): return "Dolby Atmos"
        if _has(["TrueHD"], blob): return "Dolby TrueHD"
        if _has(["E-AC3", "EAC3", "DD+", "Dolby Digital Plus"], blob): return "Dolby Digital Plus (E-AC-3)"
        if _has(["AC3", "AC-3", "Dolby Digital", "DD "], blob): return "Dolby Digital (AC-3)"
        if _has(["DTS-HD MA", "DTS HD MA", "DTS-HD Master Audio"], blob): return "DTS-HD Master Audio"
        if _has(["DTS-HD", "DTS HD"], blob): return "DTS-HD High Resolution"
        if _has(["DTS:X", "DTSX"], blob): return "DTS:X"
        if _has(["DTS"], blob): return "DTS"
        if _has(["LPCM", "PCM"], blob): return "PCM"
        if _has(["FLAC"], blob): return "FLAC"
        if _has(["AAC"], blob): return "AAC"
        return "Audio"
    elif kind == "Subtitles":
        if _has(["PGS"], blob): return "PGS"
        if _has(["VobSub", "DVD"], blob): return "VobSub"
        if _has(["SRT"], blob): return "SRT"
        return "Subtitles"
    return None

def _extract_stream_flags(codes: dict) -> list[str]:
    flags = []
    flag_code = codes.get(5)
    if flag_code:
        try:
            flag_val = int(flag_code)
            if flag_val & 1: flags.append("Director's Comments")
            if flag_val & 2: flags.append("Alternate Director's Comments")
            if flag_val & 4: flags.append("For Visually Impaired")
            if flag_val & 256: flags.append("Core Audio")
            if flag_val & 512: flags.append("Secondary Audio")
            if flag_val & 4096: flags.append("Forced Subtitles")
        except (ValueError, TypeError):
            pass
    desc_text = f"{codes.get(6, '')} {codes.get(7, '')} {codes.get(8, '')}"
    if "forced" in desc_text.lower(): flags.append("Forced Subtitles")
    if "comment" in desc_text.lower(): flags.append("Commentary")
    if "description" in desc_text.lower(): flags.append("Audio Description")
    return flags

def parse_info_details(output: str) -> dict:
    info = defaultdict(lambda: {"streams": []})
    tinfo_map = defaultdict(dict)
    sinfo_map = defaultdict(lambda: defaultdict(dict))

    for line in output.splitlines():
        line = line.strip()
        try:
            prefix, rest = line.split(":", 1)
            if prefix == "TINFO":
                parts = rest.split(",", 3)
                if len(parts) >= 4:
                    t_str, c_str, _, val = parts
                    tinfo_map[int(t_str)][int(c_str)] = val.strip('"')
            elif prefix == "SINFO":
                parts = rest.split(",", 4)
                if len(parts) >= 5:
                    t_str, s_str, c_str, _, val = parts
                    sinfo_map[int(t_str)][int(s_str)][int(c_str)] = val.strip('"')
        except Exception:
            continue

    for t_idx, codes in tinfo_map.items():
        title_info = info[t_idx]
        chapters_str = codes.get(8, '0')
        if chapters_str and chapters_str.isdigit():
            title_info["chapters"] = int(chapters_str)
        else:
            chapter_match = re.search(r'(\d+)', str(chapters_str))
            title_info["chapters"] = int(chapter_match.group(1)) if chapter_match else 0
        title_info["duration"] = codes.get(9, "")
        title_info["size"] = codes.get(10, "")
        title_info["source"] = codes.get(16, "")
        title_info["name"] = codes.get(2, "")
        title_info["angle_info"] = codes.get(15, "")
        title_info["segments_count"] = codes.get(25, "0")
        title_info["original_title_id"] = codes.get(24, "")
        title_info["datetime"] = codes.get(23, "")

    for t_idx, streams in sinfo_map.items():
        for s_idx, codes in streams.items():
            kind = codes.get(1, "Unknown")
            codec_blob = f"{codes.get(6, '')} {codes.get(7, '')} {codes.get(8, '')}"
            detected_codec = _codec_from_blob(kind, codec_blob)
            lang_code = codes.get(3, "")
            stream_info = {
                "kind": kind, "index": s_idx,
                "lang_code": lang_code, "lang": _pretty_lang_from_code(lang_code),
                "codec": detected_codec, "flags": _extract_stream_flags(codes),
                "res": codes.get(19, ""), "ar": codes.get(20, ""), "fps": codes.get(21, ""),
                "channels_count": codes.get(22, ""), "channels_layout": codes.get(40, ""),
                "sample_rate": codes.get(17, ""), "sample_size": codes.get(18, ""),
                "bitrate": codes.get(13, ""), "name": codes.get(2, ""), "lang_name": codes.get(4, ""),
                "raw_codes": dict(codes), "raw": codes.get(8, ""),
            }
            if stream_info["channels_count"]:
                formatted = _format_channels(stream_info["channels_count"])
                if formatted: stream_info["channels_display"] = formatted
            info[t_idx]["streams"].append(stream_info)
    return dict(info)

def duration_to_seconds(d: str | None):
    if not d: return None
    parts = d.split(":")
    try:
        if len(parts) == 3:
            h, m, s = map(int, parts); return h*3600 + m*60 + s
        if len(parts) == 2:
            m, s = map(int, parts); return m*60 + s
    except Exception:
        return None
    return None
