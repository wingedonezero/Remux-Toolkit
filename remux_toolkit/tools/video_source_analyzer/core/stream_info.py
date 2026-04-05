"""Layer 0: Stream Info extraction via MediaInfo and ffprobe."""

from __future__ import annotations

import subprocess
from .models import StreamInfo


def get_stream_info(filepath: str) -> StreamInfo:
    """Extract stream metadata via MediaInfo and ffprobe."""
    info = StreamInfo()

    # MediaInfo for container-level metadata
    try:
        mi_template = (
            "%Format%|%CodecID%|%Width%|%Height%|"
            "%FrameRate%|%FrameRate_Mode%|%FrameRate_Original%|"
            "%ScanType%|%ScanOrder%|%Duration%|%FrameCount%|%BitDepth%"
        )
        result = subprocess.run(
            ["mediainfo", f"--Inform=Video;{mi_template}", filepath],
            capture_output=True, text=True, timeout=15,
        )
        parts = result.stdout.strip().split("|")
        if len(parts) >= 12:
            info.codec = parts[0]
            info.codec_id = parts[1]
            info.width = int(parts[2]) if parts[2] else 0
            info.height = int(parts[3]) if parts[3] else 0
            info.fps = float(parts[4]) if parts[4] else 0.0
            info.fps_mode = parts[5]
            info.fps_original = parts[6]
            info.scan_type = parts[7]
            info.scan_order = parts[8]
            info.duration_sec = float(parts[9]) / 1000.0 if parts[9] else 0.0
            info.frame_count = int(parts[10]) if parts[10] else 0
            info.bit_depth = int(parts[11]) if parts[11] else 8
    except Exception:
        pass

    # If MediaInfo didn't get frame count, try ffprobe
    if info.frame_count == 0:
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-count_frames",
                    "-show_entries", "stream=nb_read_frames",
                    "-of", "csv=p=0",
                    filepath,
                ],
                capture_output=True, text=True, timeout=300,
            )
            val = result.stdout.strip()
            if val:
                info.frame_count = int(val)
        except Exception:
            pass

    return info
