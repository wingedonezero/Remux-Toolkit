# remux_toolkit/tools/ffmpeg_dvd_gui/utils/paths.py
import re
from pathlib import Path
from typing import NamedTuple


def safe_name(s: str) -> str:
    """Sanitize a string for use as a filename/directory name."""
    s = re.sub(r'[\\/:*?"<>|]+', " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s or "Unnamed"


def unique_dir(base_dir: Path) -> Path:
    """Return base_dir if it doesn't exist, otherwise base_dir_001, _002, etc."""
    if not base_dir.exists():
        return base_dir
    n = 1
    while True:
        candidate = base_dir.parent / f"{base_dir.name}_{n:03d}"
        if not candidate.exists():
            return candidate
        n += 1


def is_iso(path: Path) -> bool:
    """Check if a path is an ISO/disc image file."""
    return path.is_file() and path.suffix.lower() in {".iso", ".img", ".bin", ".nrg"}


class DiscInfo(NamedTuple):
    disc_path: Path
    display_name: str
    relative_path: Path
    drop_root: Path


def find_dvd_roots_with_structure(path: Path, max_depth: int = 5) -> list[DiscInfo]:
    """
    Find all DVD roots (VIDEO_TS directories or ISOs) within the given path.
    Returns DiscInfo with structure information for preserving folder hierarchy.
    Note: This only handles DVDs, not Blu-rays.
    """
    discs: list[DiscInfo] = []
    drop_root = path.resolve()

    def _find_discs_recursive(current_path: Path, depth: int = 0) -> None:
        if depth > max_depth:
            return

        if not current_path.is_dir():
            return

        try:
            video_ts = current_path / "VIDEO_TS"

            if video_ts.is_dir():
                rel_path = current_path.relative_to(drop_root)
                discs.append(DiscInfo(
                    disc_path=video_ts,
                    display_name=current_path.name,
                    relative_path=rel_path,
                    drop_root=drop_root
                ))
                return  # Don't recurse into disc structures

            # Check for ISO files
            iso_files = []
            subdirs = []
            for item in sorted(current_path.iterdir()):
                if item.is_file() and is_iso(item):
                    iso_files.append(item)
                elif item.is_dir():
                    subdirs.append(item)

            for iso_file in iso_files:
                rel_path = iso_file.relative_to(drop_root)
                discs.append(DiscInfo(
                    disc_path=iso_file,
                    display_name=iso_file.stem,
                    relative_path=rel_path,
                    drop_root=drop_root
                ))

            # If we found ISOs, don't recurse further in this dir
            if iso_files:
                return

            # Recurse into subdirectories
            for subdir in subdirs:
                _find_discs_recursive(subdir, depth + 1)

        except (PermissionError, OSError):
            pass

    # Handle single ISO file
    if path.is_file() and is_iso(path):
        return [DiscInfo(
            disc_path=path,
            display_name=path.stem,
            relative_path=Path(".") / path.name,
            drop_root=path.parent
        )]

    # Handle directories
    if path.is_dir():
        # Check if it's a VIDEO_TS directory directly
        if (path / "VIDEO_TS").is_dir():
            return [DiscInfo(
                disc_path=path / "VIDEO_TS",
                display_name=path.name,
                relative_path=Path("."),
                drop_root=drop_root
            )]
        _find_discs_recursive(path)

    return discs


def create_output_structure(disc_info: DiscInfo, output_root: Path, preserve_structure: bool = True) -> Path:
    """Create output directory structure, preserving folder hierarchy if requested."""
    output_root.mkdir(parents=True, exist_ok=True)

    if not preserve_structure or disc_info.relative_path == Path("."):
        dest_dir = unique_dir(output_root / safe_name(disc_info.display_name))
    else:
        drop_root_name = safe_name(disc_info.drop_root.name)
        safe_parts = [safe_name(part) for part in disc_info.relative_path.parts]
        nested_path = Path(*safe_parts) if safe_parts else Path(".")

        if nested_path == Path("."):
            dest_dir = unique_dir(output_root / drop_root_name)
        else:
            base_structure = output_root / drop_root_name / nested_path
            dest_dir = unique_dir(base_structure)

    dest_dir.mkdir(parents=True, exist_ok=True)
    return dest_dir


def get_dvd_input_path(disc_path: Path) -> str:
    """
    Get the proper input path for FFmpeg's dvdvideo demuxer.
    For VIDEO_TS directories, we need the parent directory.
    For ISOs, we use the ISO path directly.
    """
    if disc_path.name == "VIDEO_TS":
        return str(disc_path.parent)
    return str(disc_path)
