# remux_toolkit/tools/ffmpeg_dvd_remuxer/utils/paths.py
import re
from pathlib import Path

def get_base_name(path: Path) -> str:
    if path.is_dir() and path.name.lower() in ("video_ts", "bmdv"):
        return path.parent.name
    return path.stem

def find_dvd_sources(path: Path) -> list[Path]:
    """Finds all valid DVD sources (ISO, VIDEO_TS folders) in a given path."""
    sources = []
    if not path.exists():
        return sources

    if path.is_file() and path.suffix.lower() == '.iso':
        sources.append(path)
    elif path.is_dir():
        # Check if the directory itself is a VIDEO_TS parent
        if (path / "VIDEO_TS").is_dir():
            sources.append(path)
        else: # Recursively search for ISOs and VIDEO_TS folders
            for item in path.rglob('*'):
                if item.is_file() and item.suffix.lower() == '.iso':
                    sources.append(item)
                elif item.is_dir() and item.name.lower() == 'video_ts':
                    sources.append(item.parent) # Add the parent folder of VIDEO_TS

    # Return unique paths
    return sorted(list(set(sources)))

def create_output_folder(output_root: Path, base_name: str, group_name: str | None) -> Path:
    """Creates a unique output folder, optionally inside a group folder."""
    safe_base = re.sub(r'[\\/:*?"<>|]+', " ", base_name).strip()

    target_root = output_root
    if group_name:
        safe_group = re.sub(r'[\\/:*?"<>|]+', " ", group_name).strip()
        target_root = output_root / safe_group

    output_folder = target_root / safe_base

    # Ensure uniqueness
    n = 1
    unique_folder = output_folder
    while unique_folder.exists():
        unique_folder = output_folder.parent / f"{output_folder.name}_{n:02d}"
        n += 1

    unique_folder.mkdir(parents=True, exist_ok=True)
    return unique_folder
