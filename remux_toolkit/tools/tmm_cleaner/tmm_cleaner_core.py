# remux_toolkit/tools/tmm_cleaner/tmm_cleaner_core.py
"""
TMM Cleaner core — scan media folders for TinyMediaManager / Kodi artefacts.

Removes:
    * artwork images  (poster / fanart / banner / thumb / .tbn ...)
    * .nfo metadata files
    * subtitles (optional, off by default)

Always keeps:
    * the media itself (mkv, mp4, avi ... any non-listed extension)
    * .txt files           <- disc-info notes
    * extension-less files <- disc-info notes

Deletion is PERMANENT (os.remove). Files are NOT sent to the trash.
"""

import os

# --------------------------------------------------------------------------- #
# Deletion policy.
#
# This is a DELETE-LIST, not a keep-list: a file is only ever marked for
# deletion if its (lower-cased) extension appears in one of the sets below.
# Everything else is kept by default -- so an unknown file type is ALWAYS
# safe.  Media containers, subtitles (off by default), .txt notes and
# extension-less files are never touched unless you explicitly opt in.
# --------------------------------------------------------------------------- #
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tbn", ".webp", ".gif", ".bmp"}
NFO_EXTS = {".nfo"}
SUB_EXTS = {".srt", ".sub", ".ass", ".ssa", ".idx", ".vtt", ".smi"}

# Never deleted, regardless of any option -- the disc-info safety net.
ALWAYS_KEEP_EXTS = {"", ".txt"}

REASON_LABELS = {"artwork": "artwork / poster", "nfo": ".nfo metadata",
                 "subtitle": "subtitle"}


def human_size(num):
    """Bytes -> human readable string."""
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024.0 or unit == "TB":
            return f"{int(num)} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024.0


def classify(filename, del_images, del_nfo, del_subs):
    """Return a reason string if the file should be deleted, else None."""
    ext = os.path.splitext(filename)[1].lower()
    if ext in ALWAYS_KEEP_EXTS:          # .txt / no-extension -> never
        return None
    if del_images and ext in IMAGE_EXTS:
        return "artwork"
    if del_nfo and ext in NFO_EXTS:
        return "nfo"
    if del_subs and ext in SUB_EXTS:
        return "subtitle"
    return None


def scan_folder(folder, recurse, del_images, del_nfo, del_subs):
    """Walk a folder and return the list of files that would be deleted."""
    entries = []
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):      # never follow / delete symlinks
                continue
            reason = classify(name, del_images, del_nfo, del_subs)
            if not reason:
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            entries.append({
                "path": full,
                "rel": os.path.relpath(full, folder),
                "size": size,
                "reason": reason,
                "checked": True,          # checked == will be deleted
            })
        if not recurse:                   # stay in the top folder only
            dirnames[:] = []
    entries.sort(key=lambda e: e["rel"].lower())
    return entries


def remove_empty_dirs(root):
    """Remove empty sub-directories of root (never root itself). Returns count."""
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if os.path.realpath(dirpath) == os.path.realpath(root):
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
                removed += 1
        except OSError:
            pass
    return removed
