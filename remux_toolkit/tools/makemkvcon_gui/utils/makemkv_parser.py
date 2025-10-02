# remux_toolkit/tools/makemkvcon_gui/utils/makemkv_parser.py
import re
from collections import defaultdict

def parse_label_from_info(output: str):
    """Extract disc label from CINFO:2"""
    m = re.search(r'CINFO:2,\d+,\d+,"([^"]+)"', output)
    return m.group(1) if m else None

def parse_disc_info(output: str) -> dict:
    """Extract comprehensive disc-level information"""
    disc_info = {}
    for line in output.splitlines():
        if line.startswith("CINFO:"):
            try:
                parts = line.split(",", 3)
                if len(parts) >= 4:
                    code = int(parts[0].split(":")[1])
                    value = parts[3].strip('"')

                    # Map CINFO codes to meaningful names
                    if code == 1: disc_info["type"] = value
                    elif code == 2: disc_info["label"] = value
                    elif code == 3: disc_info["language_code"] = value
                    elif code == 4: disc_info["language_name"] = value
                    elif code == 6: disc_info["comment"] = value
                    elif code == 32: disc_info["volume_name"] = value
            except (ValueError, IndexError):
                pass
    return disc_info

def count_titles_from_info(output: str) -> int:
    """Count unique titles, preferring TCOUNT if available"""
    # First check for TCOUNT message
    for line in output.splitlines():
        if line.startswith("TCOUNT:"):
            try:
                return int(line.split(":")[1])
            except (ValueError, IndexError):
                pass

    # Fallback: count unique TINFO entries
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
    """Format channel count into readable string"""
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
    """Convert ISO 639-2 language code to readable name"""
    if not code:
        return None
    return _LANG_NAMES.get(code.lower(), code.title())

def _has(words, blob: str) -> bool:
    """Check if any word appears in blob (word boundary aware)"""
    return any(re.search(rf"\b{re.escape(w)}\b", blob, re.IGNORECASE) for w in words)

def _parse_codec_from_ids(codes: dict, kind: str) -> str | None:
    """
    Parse codec using structured fields first (CodecId, CodecShort, CodecLong)
    Falls back to blob parsing if structured fields unavailable
    """
    codec_id = codes.get(5, "")  # ap_iaCodecId
    codec_short = codes.get(6, "")  # ap_iaCodecShort
    codec_long = codes.get(7, "")  # ap_iaCodecLong

    # Prefer CodecShort for display
    if codec_short:
        return codec_short

    # Fallback to parsing the combined blob
    codec_blob = f"{codec_id} {codec_short} {codec_long}"

    if kind == "Video":
        if _has(["HEVC", "H.265", "H265"], codec_blob): return "HEVC (H.265)"
        if _has(["AVC", "H.264", "H264"], codec_blob): return "H.264/AVC"
        if _has(["VC-1", "VC1"], codec_blob): return "VC-1"
        if _has(["MPEG-2", "Mpeg2", "MPEG2"], codec_blob): return "MPEG-2"
        return "Video"
    elif kind == "Audio":
        if _has(["Atmos"], codec_blob): return "Dolby Atmos"
        if _has(["TrueHD"], codec_blob): return "Dolby TrueHD"
        if _has(["E-AC3", "EAC3", "DD+", "Dolby Digital Plus"], codec_blob): return "Dolby Digital Plus (E-AC-3)"
        if _has(["AC3", "AC-3", "Dolby Digital", "DD "], codec_blob): return "Dolby Digital (AC-3)"
        if _has(["DTS-HD MA", "DTS HD MA", "DTS-HD Master Audio"], codec_blob): return "DTS-HD Master Audio"
        if _has(["DTS-HD", "DTS HD"], codec_blob): return "DTS-HD High Resolution"
        if _has(["DTS:X", "DTSX"], codec_blob): return "DTS:X"
        if _has(["DTS"], codec_blob): return "DTS"
        if _has(["LPCM", "PCM"], codec_blob): return "PCM"
        if _has(["FLAC"], codec_blob): return "FLAC"
        if _has(["AAC"], codec_blob): return "AAC"
        return "Audio"
    elif kind == "Subtitles":
        if _has(["PGS"], codec_blob): return "PGS"
        if _has(["VobSub", "DVD"], codec_blob): return "VobSub"
        if _has(["SRT"], codec_blob): return "SRT"
        return "Subtitles"
    return None

def _extract_stream_flags(codes: dict) -> list[str]:
    """
    Extract all stream flags from AP_AVStreamFlag bitmask
    Based on apdefs.h definitions
    """
    flags = []
    flag_code = codes.get(22)  # ap_iaStreamFlags
    if flag_code:
        try:
            flag_val = int(flag_code)
            # From apdefs.h AP_AVStreamFlag definitions
            if flag_val & 1: flags.append("Director's Comments")
            if flag_val & 2: flags.append("Alternate Director's Comments")
            if flag_val & 4: flags.append("For Visually Impaired")
            if flag_val & 256: flags.append("Core Audio")
            if flag_val & 512: flags.append("Secondary Audio")
            if flag_val & 1024: flags.append("Has Core Audio")
            if flag_val & 2048: flags.append("Derived Stream")
            if flag_val & 4096: flags.append("Forced Subtitles")
            if flag_val & 16384: flags.append("Profile Secondary Stream")
            if flag_val & 32768: flags.append("Offset Sequence ID Present")
        except (ValueError, TypeError):
            pass

    # Also check MKV-specific flags if available
    mkv_flags = codes.get(38)  # ap_iaMkvFlags
    if mkv_flags:
        try:
            mkv_val = int(mkv_flags)
            # Add any MKV-specific flag parsing here if needed
        except (ValueError, TypeError):
            pass

    # Fallback: parse from text descriptions
    desc_text = f"{codes.get(6, '')} {codes.get(7, '')} {codes.get(39, '')}"  # Include MkvFlagsText
    if not flags:  # Only use text parsing if no bitfield flags found
        if "forced" in desc_text.lower(): flags.append("Forced Subtitles")
        if "comment" in desc_text.lower(): flags.append("Commentary")
        if "description" in desc_text.lower(): flags.append("Audio Description")

    return flags

def parse_info_details(output: str) -> dict:
    """
    Parse complete title and stream information from makemkvcon info output
    Returns dict mapping title_index -> title_info with comprehensive attributes
    """
    info = defaultdict(lambda: {"streams": []})
    tinfo_map = defaultdict(dict)
    sinfo_map = defaultdict(lambda: defaultdict(dict))

    # Parse all TINFO and SINFO lines
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

    # Process title-level information
    for t_idx, codes in tinfo_map.items():
        title_info = info[t_idx]

        # Parse chapters (ap_iaChapterCount = 8)
        chapters_str = codes.get(8, '0')
        if chapters_str and chapters_str.isdigit():
            title_info["chapters"] = int(chapters_str)
        else:
            chapter_match = re.search(r'(\d+)', str(chapters_str))
            title_info["chapters"] = int(chapter_match.group(1)) if chapter_match else 0

        # Basic attributes from apdefs.h AP_ItemAttributeId
        title_info["type"] = codes.get(1, "")  # ap_iaType
        title_info["name"] = codes.get(2, "")  # ap_iaName
        title_info["duration"] = codes.get(9, "")  # ap_iaDuration
        title_info["size"] = codes.get(10, "")  # ap_iaDiskSize
        title_info["size_bytes"] = codes.get(11, "")  # ap_iaDiskSizeBytes
        title_info["bitrate"] = codes.get(13, "")  # ap_iaBitrate
        title_info["angle_info"] = codes.get(15, "")  # ap_iaAngleInfo
        title_info["source"] = codes.get(16, "")  # ap_iaSourceFileName
        title_info["datetime"] = codes.get(23, "")  # ap_iaDateTime
        title_info["original_title_id"] = codes.get(24, "")  # ap_iaOriginalTitleId
        title_info["segments_count"] = codes.get(25, "0")  # ap_iaSegmentsCount
        title_info["segments_map"] = codes.get(26, "")  # ap_iaSegmentsMap
        title_info["output_filename"] = codes.get(27, "")  # ap_iaOutputFileName
        title_info["tree_info"] = codes.get(30, "")  # ap_iaTreeInfo
        title_info["panel_title"] = codes.get(31, "")  # ap_iaPanelTitle
        title_info["order_weight"] = codes.get(33, "")  # ap_iaOrderWeight
        title_info["seamless_info"] = codes.get(36, "")  # ap_iaSeamlessInfo
        title_info["panel_text"] = codes.get(37, "")  # ap_iaPanelText
        title_info["comment"] = codes.get(49, "")  # ap_iaComment
        title_info["offset_sequence_id"] = codes.get(50, "")  # ap_iaOffsetSequenceId

    # Process stream-level information
    for t_idx, streams in sinfo_map.items():
        for s_idx, codes in streams.items():
            kind = codes.get(1, "Unknown")  # ap_iaType

            # Use structured codec parsing
            detected_codec = _parse_codec_from_ids(codes, kind)

            lang_code = codes.get(3, "")  # ap_iaLangCode
            stream_info = {
                "kind": kind,
                "index": s_idx,
                "name": codes.get(2, ""),  # ap_iaName
                "lang_code": lang_code,
                "lang": _pretty_lang_from_code(lang_code),
                "lang_name": codes.get(4, ""),  # ap_iaLangName
                "codec": detected_codec,
                "codec_id": codes.get(5, ""),  # ap_iaCodecId
                "codec_short": codes.get(6, ""),  # ap_iaCodecShort
                "codec_long": codes.get(7, ""),  # ap_iaCodecLong
                "flags": _extract_stream_flags(codes),

                # Video-specific attributes
                "res": codes.get(19, ""),  # ap_iaVideoSize
                "ar": codes.get(20, ""),  # ap_iaVideoAspectRatio
                "fps": codes.get(21, ""),  # ap_iaVideoFrameRate

                # Audio-specific attributes
                "channels_count": codes.get(14, ""),  # ap_iaAudioChannelsCount
                "channels_layout": codes.get(40, ""),  # ap_iaAudioChannelLayoutName
                "sample_rate": codes.get(17, ""),  # ap_iaAudioSampleRate
                "sample_size": codes.get(18, ""),  # ap_iaAudioSampleSize

                # Output conversion info
                "output_codec_short": codes.get(41, ""),  # ap_iaOutputCodecShort
                "output_conversion_type": codes.get(42, ""),  # ap_iaOutputConversionType
                "output_audio_sample_rate": codes.get(43, ""),  # ap_iaOutputAudioSampleRate
                "output_audio_sample_size": codes.get(44, ""),  # ap_iaOutputAudioSampleSize
                "output_audio_channels": codes.get(45, ""),  # ap_iaOutputAudioChannelsCount
                "output_audio_layout": codes.get(46, ""),  # ap_iaOutputAudioChannelLayoutName
                "output_audio_layout_code": codes.get(47, ""),  # ap_iaOutputAudioChannelLayout
                "output_audio_mix_desc": codes.get(48, ""),  # ap_iaOutputAudioMixDescription

                # Other attributes
                "bitrate": codes.get(13, ""),  # ap_iaBitrate
                "metadata_lang_code": codes.get(28, ""),  # ap_iaMetadataLanguageCode
                "metadata_lang_name": codes.get(29, ""),  # ap_iaMetadataLanguageName
                "mkv_flags": codes.get(38, ""),  # ap_iaMkvFlags
                "mkv_flags_text": codes.get(39, ""),  # ap_iaMkvFlagsText

                # Keep raw data for debugging
                "raw_codes": dict(codes),
            }

            # Format channels for display
            if stream_info["channels_count"]:
                formatted = _format_channels(stream_info["channels_count"])
                if formatted:
                    stream_info["channels_display"] = formatted

            info[t_idx]["streams"].append(stream_info)

    return dict(info)

def duration_to_seconds(d: str | None):
    """Convert HH:MM:SS or MM:SS duration string to seconds"""
    if not d:
        return None
    parts = d.split(":")
    try:
        if len(parts) == 3:
            h, m, s = map(int, parts)
            return h * 3600 + m * 60 + s
        if len(parts) == 2:
            m, s = map(int, parts)
            return m * 60 + s
    except Exception:
        return None
    return None

def parse_exit_code_message(returncode: int) -> str:
    """
    Convert makemkvcon exit code to human-readable message
    Based on official documentation
    """
    if returncode == 0:
        return "Success"
    elif returncode == 1:
        return "Failed (check log for details)"
    elif returncode == 2:
        return "Failed (invalid command line arguments)"
    else:
        return f"Failed (exit code {returncode})"
