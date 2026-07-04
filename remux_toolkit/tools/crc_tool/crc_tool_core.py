# remux_toolkit/tools/crc_tool/crc_tool_core.py
"""
CRC/MD5 File Integrity core.
FileBot-style CRC appending + recursive MD5 manifest creation/verification.
"""

import os
import re
import hashlib
import zlib

from PyQt6.QtCore import QThread, pyqtSignal

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS & HELPERS
# ─────────────────────────────────────────────────────────────────────────────

CRC32_PATTERN = re.compile(r'\(([0-9A-Fa-f]{8})\)(?:\.[^.]+)?$')
MD5_LINE_PATTERN = re.compile(r'^([0-9a-fA-F]{32})\s+\*?(.+)$')

CHUNK_SIZE = 1024 * 1024  # 1 MB


def compute_crc32(filepath: str, progress_cb=None) -> str:
    crc = 0
    size = os.path.getsize(filepath)
    done = 0
    with open(filepath, 'rb') as f:
        while chunk := f.read(CHUNK_SIZE):
            crc = zlib.crc32(chunk, crc)
            done += len(chunk)
            if progress_cb and size:
                progress_cb(int(done * 100 / size))
    return format(crc & 0xFFFFFFFF, '08X')


def compute_md5(filepath: str, progress_cb=None) -> str:
    h = hashlib.md5()
    size = os.path.getsize(filepath)
    done = 0
    with open(filepath, 'rb') as f:
        while chunk := f.read(CHUNK_SIZE):
            h.update(chunk)
            done += len(chunk)
            if progress_cb and size:
                progress_cb(int(done * 100 / size))
    return h.hexdigest()


def strip_crc_from_name(name: str) -> str:
    """Remove trailing CRC from stem, e.g.  'File (ABCD1234)' -> 'File'"""
    stem, ext = os.path.splitext(name)
    stem = re.sub(r'\s*\([0-9A-Fa-f]{8}\)\s*$', '', stem).rstrip()
    return stem + ext


def append_crc_to_name(name: str, crc: str) -> str:
    """Append CRC: 'File.mkv' -> 'File (ABCD1234).mkv'"""
    clean = strip_crc_from_name(name)
    stem, ext = os.path.splitext(clean)
    return f"{stem} ({crc}){ext}"


def format_size(b: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def collect_files(paths: list) -> list:
    """Expand dirs recursively, return flat list of file paths."""
    result = []
    for p in paths:
        if os.path.isfile(p):
            result.append(p)
        elif os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs.sort()
                for f in sorted(files):
                    full = os.path.join(root, f)
                    result.append(full)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# WORKER THREADS
# ─────────────────────────────────────────────────────────────────────────────

class CRCWorker(QThread):
    row_progress = pyqtSignal(int, int)          # row, pct
    row_done     = pyqtSignal(int, str, str)     # row, old_name, new_name
    row_error    = pyqtSignal(int, str)
    all_done     = pyqtSignal()

    def __init__(self, rows: list, rename: bool):
        super().__init__()
        # rows = list of (row_index, filepath)
        self.rows = rows
        self.rename = rename
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        for row, filepath in self.rows:
            if self._cancel:
                break
            try:
                crc = compute_crc32(filepath, lambda p: self.row_progress.emit(row, p))
                old_name = os.path.basename(filepath)
                new_name = append_crc_to_name(old_name, crc)
                if self.rename and old_name != new_name:
                    new_path = os.path.join(os.path.dirname(filepath), new_name)
                    os.rename(filepath, new_path)
                self.row_done.emit(row, old_name, new_name)
            except Exception as e:
                self.row_error.emit(row, str(e))
        self.all_done.emit()


class MD5Worker(QThread):
    file_progress = pyqtSignal(int, int, str)   # current_file_idx, pct, filename
    all_done      = pyqtSignal(str, int, int)   # manifest_path, total_files, total_bytes
    error         = pyqtSignal(str)

    def __init__(self, folder: str):
        super().__init__()
        self.folder = folder
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        folder = self.folder
        files = []
        for root, dirs, fnames in os.walk(folder):
            dirs.sort()
            for f in sorted(fnames):
                fp = os.path.join(root, f)
                # skip existing md5 manifests
                if fp.lower().endswith('.md5'):
                    continue
                files.append(fp)

        folder_name = os.path.basename(folder.rstrip('/\\'))
        manifest_path = os.path.join(folder, f"{folder_name}.md5")

        total_bytes = 0
        lines = []
        try:
            for idx, fp in enumerate(files):
                if self._cancel:
                    return
                rel = os.path.relpath(fp, folder)
                self.file_progress.emit(idx, 0, rel)
                h = compute_md5(fp, lambda p: self.file_progress.emit(idx, p, rel))
                lines.append(f"{h} *{rel}")
                total_bytes += os.path.getsize(fp)

            with open(manifest_path, 'w', encoding='utf-8') as mf:
                mf.write('\n'.join(lines) + '\n')

            self.all_done.emit(manifest_path, len(files), total_bytes)
        except Exception as e:
            self.error.emit(str(e))


class VerifyWorker(QThread):
    item_result  = pyqtSignal(str, bool, str)   # filepath, ok, detail
    progress     = pyqtSignal(int, int)          # done, total
    all_done     = pyqtSignal(int, int)          # passed, failed

    def __init__(self, tasks: list):
        """
        tasks = list of dicts:
          {'type': 'crc',  'path': filepath}
          {'type': 'md5',  'manifest': manifest_path, 'base_dir': dir}
        """
        super().__init__()
        self.tasks = tasks
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        passed = failed = 0
        total = self._count_total()
        done = 0

        for task in self.tasks:
            if self._cancel:
                break
            if task['type'] == 'crc':
                fp = task['path']
                name = os.path.basename(fp)
                m = CRC32_PATTERN.search(name)
                if not m:
                    self.item_result.emit(fp, False, "No CRC in filename")
                    failed += 1
                else:
                    expected = m.group(1).upper()
                    try:
                        actual = compute_crc32(fp)
                        ok = actual == expected
                        detail = f"Expected {expected} | Got {actual}"
                        self.item_result.emit(fp, ok, detail)
                        if ok:
                            passed += 1
                        else:
                            failed += 1
                    except Exception as e:
                        self.item_result.emit(fp, False, str(e))
                        failed += 1
                done += 1
                self.progress.emit(done, total)

            elif task['type'] == 'md5':
                manifest = task['manifest']
                base_dir = task['base_dir']
                try:
                    with open(manifest, 'r', encoding='utf-8') as f:
                        for line in f:
                            if self._cancel:
                                break
                            line = line.strip()
                            if not line:
                                continue
                            mm = MD5_LINE_PATTERN.match(line)
                            if not mm:
                                continue
                            expected_hash = mm.group(1).lower()
                            rel_path = mm.group(2)
                            fp = os.path.join(base_dir, rel_path)
                            if not os.path.exists(fp):
                                self.item_result.emit(fp, False, "File missing")
                                failed += 1
                            else:
                                actual = compute_md5(fp)
                                ok = actual == expected_hash
                                detail = f"Expected {expected_hash[:8]}… | Got {actual[:8]}…"
                                self.item_result.emit(fp, ok, detail)
                                if ok:
                                    passed += 1
                                else:
                                    failed += 1
                            done += 1
                            self.progress.emit(done, total)
                except Exception as e:
                    self.item_result.emit(manifest, False, f"Manifest error: {e}")
                    failed += 1

        self.all_done.emit(passed, failed)

    def _count_total(self):
        total = 0
        for task in self.tasks:
            if task['type'] == 'crc':
                total += 1
            elif task['type'] == 'md5':
                try:
                    with open(task['manifest'], 'r', encoding='utf-8') as f:
                        total += sum(1 for l in f if MD5_LINE_PATTERN.match(l.strip()))
                except Exception:
                    total += 1
        return max(total, 1)
