# remux_toolkit/tools/makemkvcon_gui/models/job.py
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

@dataclass
class Job:
    source_type: str
    source_path: str
    source_spec: str
    child_name: str
    group_root: str | None = None
    label_hint: str | None = None
    titles_total: int | None = None
    titles_info: dict | None = None
    selected_titles: set[int] | None = None  # None => all
    status: str = "Queued"
    out_dir: Path | None = None
    log_path: Path | None = None
    cmdline: str | None = None
    relative_path: Path | None = None
    drop_root: Path | None = None
    preserve_structure: bool = True
