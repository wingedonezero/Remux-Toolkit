# remux_toolkit/tools/ffmpeg_dvd_gui/models/job.py
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Job:
    source_type: str  # "iso" or "folder"
    source_path: str  # Path to VIDEO_TS or ISO
    child_name: str  # Display name
    label_hint: str | None = None
    titles_total: int | None = None
    titles_info: dict | None = None  # {title_num: {duration, chapters, streams, ...}}
    disc_info: dict | None = None  # Disc-level metadata
    selected_titles: set[int] | None = None  # None => all
    status: str = "Queued"
    out_dir: Path | None = None
    log_path: Path | None = None
    relative_path: Path | None = None
    drop_root: Path | None = None
    preserve_structure: bool = True
