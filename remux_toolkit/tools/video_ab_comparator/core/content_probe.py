# remux_toolkit/tools/video_ab_comparator/core/content_probe.py
"""
Content-type detection for frame-match eligibility and reporting.

Classifies each source as progressive / interlaced / soft_telecine /
unknown by cross-validating ffprobe stream metadata against MediaInfo's
MPEG-2 picture-header analysis (repeat_first_field / scan order flags —
the same data DGIndex reads, but as a fast metadata scan).

The comparator analyzes frames AS-IS in both sources regardless of
content type — no IVTC/deinterlace normalization is done. Content type
only decides whether sliding frame matching is attempted:

- progressive + progressive + matching fps → frame matching
- anything else → audio-correlation alignment, loudly, with the reason
  recorded so the report says exactly why frames weren't matched.

MPEG-1/2 is excluded from frame matching even when classified
progressive: its container timestamps and index grids are not reliable
enough for the frame-index arithmetic the matcher depends on.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional, Tuple


def _detect_mediainfo_properties(video_path: str) -> dict[str, Any]:
    """Read scan type / fps mode / pulldown flags via the mediainfo CLI.

    Returns an empty dict when mediainfo is not installed or fails.
    """
    if not shutil.which("mediainfo"):
        return {}

    inform = (
        "Video;"
        "mi_fps=%FrameRate%\\n"
        "mi_fps_mode=%FrameRate_Mode%\\n"
        "mi_scan_type=%ScanType%\\n"
        "mi_scan_order=%ScanOrder%\\n"
        "mi_original_fps=%FrameRate_Original%\\n"
        "mi_codec=%Format%\\n"
    )
    cmd = ["mediainfo", f"--Inform={inform}", str(video_path)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return {}

        props: dict[str, Any] = {}
        for line in result.stdout.strip().splitlines():
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if not value:
                continue
            if key in ("mi_fps", "mi_original_fps"):
                try:
                    props[key] = float(value)
                except ValueError:
                    pass
            else:
                props[key] = value
        return props
    except Exception as exc:
        print(f"[ContentProbe] MediaInfo detection failed: {exc}")
        return {}


def _classify_content_type(
    ffprobe_props: dict[str, Any],
    mi_props: dict[str, Any],
) -> Tuple[str, str]:
    """Classify content by cross-validating ffprobe and MediaInfo.

    Returns ``(content_type, confidence)`` where content_type is one of
    ``progressive`` / ``interlaced`` / ``soft_telecine`` / ``unknown``
    and confidence is ``high`` / ``medium`` / ``low``.
    """
    codec = ffprobe_props.get("codec_name", "")
    is_mpeg2 = codec in ("mpeg2video", "mpeg1video")
    is_dvd = ffprobe_props.get("is_dvd", False)

    fp_interlaced = ffprobe_props.get("interlaced", False)
    fp_field_order = ffprobe_props.get("field_order", "unknown")
    fp_is_vfr = ffprobe_props.get("is_vfr", False)

    mi_fps_mode = mi_props.get("mi_fps_mode", "")
    mi_scan_type = mi_props.get("mi_scan_type", "")
    mi_scan_order = mi_props.get("mi_scan_order", "")
    has_mediainfo = bool(mi_props)

    # 1. Non-MPEG2: progressive encode is the normal case
    if not is_mpeg2:
        if fp_interlaced:
            print("[ContentProbe] Non-MPEG2 interlaced (H.264/HEVC interlaced encode)")
            return "interlaced", "medium"
        return "progressive", "high"

    # 2. MPEG-2 with MediaInfo: MPEG-2 flag analysis
    if has_mediainfo:
        is_mi_vfr = mi_fps_mode.upper() == "VFR"
        is_mi_pulldown = "pulldown" in mi_scan_order.lower()
        is_mi_interlaced = mi_scan_type.lower() == "interlaced"

        if is_mi_vfr and is_mi_pulldown:
            confidence = "high" if (fp_is_vfr or fp_field_order == "progressive") else "medium"
            print(f"[ContentProbe] Soft telecine detected (MediaInfo: {mi_fps_mode}, {mi_scan_order})")
            return "soft_telecine", confidence

        if is_mi_vfr and not is_mi_pulldown:
            print(f"[ContentProbe] VFR MPEG-2 without standard pulldown (ScanOrder: {mi_scan_order})")
            return "soft_telecine", "medium"

        if not is_mi_vfr and is_mi_interlaced:
            confidence = "high" if fp_interlaced else "medium"
            print(f"[ContentProbe] Pure interlaced (MediaInfo: {mi_fps_mode}, {mi_scan_type})")
            return "interlaced", confidence

        if not is_mi_vfr and not is_mi_interlaced:
            print(f"[ContentProbe] Progressive MPEG-2 (MediaInfo: {mi_fps_mode}, {mi_scan_type})")
            return "progressive", "medium"

    # 3. MPEG-2 without MediaInfo: ffprobe-only fallback
    print("[ContentProbe] MediaInfo unavailable — ffprobe only (less reliable for MPEG-2)")
    if fp_is_vfr:
        return "soft_telecine", "low"
    if fp_interlaced and is_dvd:
        return "interlaced", "low"
    if fp_interlaced:
        return "interlaced", "low"
    return "unknown", "low"


def detect_video_properties(video_path: str) -> dict[str, Any]:
    """Detect fps / scan / cadence / codec properties for one source.

    Returns a dict with at minimum: fps, is_vfr, interlaced, field_order,
    content_type, detection_confidence, codec_name, is_dvd, is_sd,
    width, height, duration_ms, detection_source.
    """
    props: dict[str, Any] = {
        "fps": 23.976,
        "original_fps": None,
        "is_vfr": False,
        "is_soft_telecine": False,
        "interlaced": False,
        "field_order": "progressive",
        "content_type": "unknown",
        "detection_confidence": "low",
        "is_sd": False,
        "is_dvd": False,
        "codec_name": "",
        "duration_ms": 0.0,
        "width": 0,
        "height": 0,
        "detection_source": "fallback",
    }

    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-select_streams", "v:0",
            "-show_entries",
            "stream=r_frame_rate,avg_frame_rate,field_order,nb_frames,duration,codec_name,width,height",
            "-show_entries", "format=duration",
            "-of", "json",
            str(video_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print("[ContentProbe] ffprobe failed, using defaults")
            return props

        data = json.loads(result.stdout)
        if not data.get("streams"):
            print("[ContentProbe] No video streams found")
            return props

        stream = data["streams"][0]
        props["detection_source"] = "ffprobe"

        r_frame_rate = stream.get("r_frame_rate", "24000/1001")
        avg_frame_rate = stream.get("avg_frame_rate", r_frame_rate)

        def parse_rate(rate: str, default: float) -> float:
            try:
                if "/" in str(rate):
                    n, d = str(rate).split("/")
                    return float(n) / float(d) if float(d) != 0 else default
                return float(rate) if rate else default
            except Exception:
                return default

        r_fps = parse_rate(r_frame_rate, 23.976)
        a_fps = parse_rate(avg_frame_rate, r_fps)

        fps_diff_pct = abs(r_fps - a_fps) / r_fps * 100 if r_fps > 0 else 0
        if fps_diff_pct > 1.0:
            props["is_vfr"] = True
            props["fps"] = a_fps
            props["original_fps"] = r_fps
            if abs(r_fps - 23.976) < 0.1 and 24.0 < a_fps < 25.0:
                props["is_soft_telecine"] = True
        else:
            props["fps"] = r_fps

        props["width"] = stream.get("width", 0)
        props["height"] = stream.get("height", 0)

        field_order = stream.get("field_order", "progressive")
        if field_order in ("tt", "tb"):
            props["interlaced"] = True
            props["field_order"] = "tff"
        elif field_order in ("bb", "bt"):
            props["interlaced"] = True
            props["field_order"] = "bff"
        elif field_order == "progressive":
            props["interlaced"] = False
            props["field_order"] = "progressive"
        else:
            props["field_order"] = "unknown"

        duration_str = stream.get("duration")
        if duration_str and duration_str != "N/A":
            props["duration_ms"] = float(duration_str) * 1000.0
        else:
            fmt_duration = data.get("format", {}).get("duration")
            if fmt_duration and fmt_duration != "N/A":
                props["duration_ms"] = float(fmt_duration) * 1000.0

        height = props["height"]
        props["is_sd"] = 0 < height <= 576

        codec = stream.get("codec_name", "")
        props["codec_name"] = codec
        is_dvd_codec = codec in ("mpeg2video", "mpeg1video")
        props["is_dvd"] = (
            is_dvd_codec
            and height in (480, 486, 576, 578)
            and props["width"] in (720, 704, 352)
        )

        mi_props = _detect_mediainfo_properties(video_path)
        if mi_props:
            props["detection_source"] = "ffprobe+mediainfo"
            props["mediainfo"] = mi_props

            mi_orig = mi_props.get("mi_original_fps")
            if mi_orig and mi_orig > 0:
                props["original_fps"] = mi_orig

            if mi_props.get("mi_fps_mode", "").upper() == "VFR":
                props["is_vfr"] = True
                if "pulldown" in mi_props.get("mi_scan_order", "").lower():
                    props["is_soft_telecine"] = True

        content_type, confidence = _classify_content_type(props, mi_props)
        props["content_type"] = content_type
        props["detection_confidence"] = confidence

        dvd_note = " (DVD)" if props["is_dvd"] else ""
        sd_note = " (SD)" if props["is_sd"] and not props["is_dvd"] else ""
        print(
            f"[ContentProbe] {Path(video_path).name}: {content_type}{dvd_note}{sd_note} "
            f"@ {props['fps']:.3f}fps, codec={codec}, scan={props['field_order']} "
            f"[confidence: {confidence}, source: {props['detection_source']}]"
        )

        return props

    except Exception as e:
        print(f"[ContentProbe] Detection failed: {e}")
        return props


def frame_match_eligibility(
    props_a: dict[str, Any],
    props_b: dict[str, Any],
) -> Tuple[bool, str]:
    """Decide whether sliding frame matching should be attempted.

    Frame matching is only realistic for progressive, constant-rate,
    same-fps pairs; everything else falls back to audio correlation.
    Returns ``(eligible, reason)`` — reason is "" when eligible, else a
    ``skipped-*`` string recorded in the alignment details.
    """
    for label, props in (("A", props_a), ("B", props_b)):
        codec = props.get("codec_name", "")
        if codec in ("mpeg2video", "mpeg1video"):
            return False, f"skipped-mpeg2-content (source {label})"

        ctype = props.get("content_type", "unknown")
        if ctype == "interlaced":
            return False, f"skipped-interlaced-content (source {label})"
        if ctype == "soft_telecine":
            return False, f"skipped-soft-telecine-content (source {label})"
        if ctype != "progressive":
            return False, f"skipped-unknown-content-type (source {label})"

        if props.get("is_vfr"):
            return False, f"skipped-vfr-content (source {label})"

    fps_a = props_a.get("fps", 0.0) or 0.0
    fps_b = props_b.get("fps", 0.0) or 0.0
    if fps_a <= 0 or fps_b <= 0:
        return False, "skipped-unknown-fps"
    fps_ratio = max(fps_a, fps_b) / min(fps_a, fps_b)
    if fps_ratio > 1.01:
        return False, f"skipped-cross-fps ({fps_a:.3f} vs {fps_b:.3f})"

    return True, ""
