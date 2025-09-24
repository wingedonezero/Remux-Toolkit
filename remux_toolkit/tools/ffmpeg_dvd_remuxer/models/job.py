# remux_toolkit/tools/ffmpeg_dvd_remuxer/models/job.py
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any

@dataclass
class Job:
    source_path: Path
    group_name: Optional[str] = None
    base_name: str = ""
    status: str = "Queued"
    titles_info: list[dict] = field(default_factory=list)
    selected_titles: set[int] = field(default_factory=set)

    # Internal reference to the GUI item for easy updates
    _gui_item: Optional[Any] = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if not self.base_name:
            if self.source_path.is_dir() and self.source_path.name.lower() in ("video_ts", "bmdv"):
                self.base_name = self.source_path.parent.name
            else:
                self.base_name = self.source_path.stem
