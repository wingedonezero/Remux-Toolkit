# remux_toolkit/tools/ffmpeg_d_remuxer/models/job.py
from dataclasses import dataclass
from pathlib import Path

@dataclass
class Job:
    # --- CORRECTED ORDER ---
    # Required fields (no default value) come first.
    input_path: Path
    base_name: str
    titles_to_process: list[int]

    # Optional field (has a default value) comes last.
    group_name: str | None = None
