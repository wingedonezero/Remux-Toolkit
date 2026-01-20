# remux_toolkit/tools/ffmpeg_dvd_gui/utils/ffmpeg_parser.py
"""
Utilities for parsing FFmpeg/FFprobe output for DVD remuxing.
"""
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional


def which(cmd: str) -> Optional[str]:
    """Find command in PATH, returns path or None."""
    return shutil.which(cmd)


def check_tool_available(tool_path: str) -> tuple[bool, str]:
    """
    Check if a tool is available and return version info.
    Returns (available, version_or_error).
    """
    try:
        result = subprocess.run(
            [tool_path, "-version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            # Extract first line of version info
            version = result.stdout.split('\n')[0] if result.stdout else "available"
            return True, version
        return False, f"Exit code {result.returncode}"
    except FileNotFoundError:
        return False, "Not found in PATH"
    except subprocess.TimeoutExpired:
        return False, "Timed out"
    except Exception as e:
        return False, str(e)


@dataclass
class StreamInfo:
    """Information about a single stream."""
    index: int
    codec_type: str  # "video", "audio", "subtitle"
    codec_name: str
    language: str
    channels: int = 0
    channel_layout: str = ""
    sample_rate: int = 0
    width: int = 0
    height: int = 0
    extra: dict = None

    def __post_init__(self):
        if self.extra is None:
            self.extra = {}


@dataclass
class TitleInfo:
    """Information about a DVD title."""
    title_num: int
    duration: str
    duration_seconds: float
    chapters: int
    streams: list[StreamInfo]
    video_codec: str = ""
    audio_count: int = 0
    subtitle_count: int = 0


def duration_to_seconds(duration_str: str) -> float:
    """Convert duration string (HH:MM:SS.mmm or seconds) to float seconds."""
    if not duration_str:
        return 0.0
    try:
        # Try parsing as float first (already in seconds)
        return float(duration_str)
    except ValueError:
        pass

    # Parse HH:MM:SS.mmm format
    match = re.match(r'(\d+):(\d+):(\d+(?:\.\d+)?)', duration_str)
    if match:
        h, m, s = match.groups()
        return int(h) * 3600 + int(m) * 60 + float(s)
    return 0.0


def seconds_to_duration(seconds: float) -> str:
    """Convert seconds to HH:MM:SS format."""
    if seconds <= 0:
        return "0:00:00"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}:{m:02d}:{s:02d}"


def format_bytes_human(size_bytes: int) -> str:
    """Format bytes to human readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def probe_dvd_title(ffprobe_path: str, dvd_path: str, title_num: int, timeout: int = 60) -> Optional[TitleInfo]:
    """
    Probe a specific DVD title using ffprobe.
    Returns TitleInfo or None if title doesn't exist.
    """
    cmd = [
        ffprobe_path,
        "-f", "dvdvideo",
        "-title", str(title_num),
        "-v", "error",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        "-print_format", "json",
        "-i", dvd_path
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)

        # Parse format info
        fmt = data.get("format", {})
        duration_str = fmt.get("duration", "0")
        duration_secs = duration_to_seconds(duration_str)

        # Parse chapters
        chapters = data.get("chapters", [])
        chapter_count = len(chapters)

        # Parse streams
        streams = []
        video_codec = ""
        audio_count = 0
        subtitle_count = 0

        for stream in data.get("streams", []):
            codec_type = stream.get("codec_type", "")
            codec_name = stream.get("codec_name", "unknown")

            si = StreamInfo(
                index=stream.get("index", 0),
                codec_type=codec_type,
                codec_name=codec_name,
                language=stream.get("tags", {}).get("language", "und"),
                channels=stream.get("channels", 0),
                channel_layout=stream.get("channel_layout", ""),
                sample_rate=int(stream.get("sample_rate", 0) or 0),
                width=stream.get("width", 0),
                height=stream.get("height", 0),
            )
            streams.append(si)

            if codec_type == "video":
                if not video_codec:
                    video_codec = codec_name.upper()
            elif codec_type == "audio":
                audio_count += 1
            elif codec_type == "subtitle":
                subtitle_count += 1

        return TitleInfo(
            title_num=title_num,
            duration=seconds_to_duration(duration_secs),
            duration_seconds=duration_secs,
            chapters=chapter_count,
            streams=streams,
            video_codec=video_codec,
            audio_count=audio_count,
            subtitle_count=subtitle_count,
        )

    except subprocess.TimeoutExpired:
        return None
    except json.JSONDecodeError:
        return None
    except Exception:
        return None


def probe_all_dvd_titles(ffprobe_path: str, dvd_path: str, max_titles: int = 99) -> dict[int, TitleInfo]:
    """
    Probe all titles on a DVD.
    Returns dict of {title_num: TitleInfo}.
    """
    titles = {}

    # DVD titles are numbered 1-99
    for title_num in range(1, max_titles + 1):
        info = probe_dvd_title(ffprobe_path, dvd_path, title_num)
        if info:
            titles[title_num] = info
        else:
            # Once we hit a non-existent title, there are likely no more
            # But some discs have gaps, so we continue for a few more
            # Actually, let's be more thorough - check a few more titles
            if title_num > 10 and not titles:
                # No titles found in first 10, probably not a valid DVD
                break
            # Continue checking even after a miss
            pass

    return titles


def parse_ffmpeg_progress(line: str) -> Optional[dict]:
    """
    Parse FFmpeg progress output line.
    Returns dict with parsed values or None.

    Example line:
    frame=  100 fps=50 q=-1.0 size=   51200kB time=00:05:30.00 bitrate=1270.4kbits/s speed=2.0x
    """
    result = {}

    # Parse time
    time_match = re.search(r'time=(\d+:\d+:\d+\.\d+)', line)
    if time_match:
        result['time'] = time_match.group(1)
        result['time_seconds'] = duration_to_seconds(time_match.group(1))

    # Parse speed
    speed_match = re.search(r'speed=\s*([\d.]+)x', line)
    if speed_match:
        result['speed'] = float(speed_match.group(1))

    # Parse size
    size_match = re.search(r'size=\s*(\d+)kB', line)
    if size_match:
        result['size_kb'] = int(size_match.group(1))

    # Parse bitrate
    bitrate_match = re.search(r'bitrate=\s*([\d.]+)kbits/s', line)
    if bitrate_match:
        result['bitrate_kbps'] = float(bitrate_match.group(1))

    # Parse frame
    frame_match = re.search(r'frame=\s*(\d+)', line)
    if frame_match:
        result['frame'] = int(frame_match.group(1))

    return result if result else None


def title_info_to_dict(info: TitleInfo) -> dict:
    """Convert TitleInfo to a dict for storage in Job."""
    return {
        "title_num": info.title_num,
        "duration": info.duration,
        "duration_seconds": info.duration_seconds,
        "chapters": info.chapters,
        "video_codec": info.video_codec,
        "audio_count": info.audio_count,
        "subtitle_count": info.subtitle_count,
        "streams": [
            {
                "index": s.index,
                "kind": s.codec_type.capitalize() if s.codec_type != "subtitle" else "Subtitles",
                "codec": s.codec_name,
                "language": s.language,
                "channels": s.channels,
                "channel_layout": s.channel_layout,
                "sample_rate": s.sample_rate,
                "width": s.width,
                "height": s.height,
            }
            for s in info.streams
        ]
    }
