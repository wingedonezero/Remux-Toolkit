"""
Cross-rip comparison tool.

Takes one or more MKV files (from different ripping pipelines — ours,
ffmpeg-dvdvideo, patched-ffmpeg-dvdvideo, MakeMKV, ...) and produces a
stream-by-stream comparison:

  * Per-stream SHA-256 of the extracted elementary bytes
  * Per-stream frame count (decoded)
  * Per-stream byte total
  * Per-stream computed duration (frames × 1/fps for video,
    AC3 frames × 1536/sample_rate for audio)
  * Diff matrix: which rips have identical streams; if they differ, where

Hash comparison means: if MD5(our video ES) == MD5(makemkv video ES), our
ripping is bit-for-bit correct on that stream. That's the strongest proof
we can have that we're not dropping or corrupting data on the wire.

For audio it's even better — a single different byte cascades through AC3
frame parsing, so any mismatch shows up.

CLI:
    python -m remux_toolkit.tools.ffmpeg_dvd_gui.core.analysis.compare_streams \\
        ours.mkv ref.mkv [more.mkv ...] [--output report.json]

Or, rip-and-compare in one go:
    python -m ... compare_streams \\
        --rip-our /path/to/disc TITLE \\
        --rip-ffmpeg-dvdvideo /path/to/disc TITLE \\
        [--ffmpeg /path/to/patched-ffmpeg] \\
        --output report.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Probing a single MKV
# ---------------------------------------------------------------------------

@dataclass
class StreamFingerprint:
    index: int                  # stream index in the MKV
    codec_type: str             # "video", "audio", "subtitle"
    codec_name: str             # "mpeg2video", "ac3", ...
    language: str
    sha256: str
    es_bytes: int
    decoded_frames: int
    duration_inferred_seconds: float

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "codec_type": self.codec_type,
            "codec_name": self.codec_name,
            "language": self.language,
            "sha256": self.sha256,
            "es_bytes": self.es_bytes,
            "decoded_frames": self.decoded_frames,
            "duration_inferred_seconds": round(self.duration_inferred_seconds, 3),
        }


def _ffprobe_json(mkv_path: Path, *, ffprobe_bin: str = "ffprobe") -> dict:
    out = subprocess.check_output(
        [ffprobe_bin, "-hide_banner", "-v", "error", "-show_format",
         "-show_streams", "-print_format", "json", str(mkv_path)],
        text=True,
    )
    return json.loads(out)


def _ffmpeg_raw_format(codec_type: str, codec_name: str) -> Optional[str]:
    """ffmpeg `-f` value for extracting this stream as raw ES."""
    if codec_type == "video":
        # Most DVD video is mpeg2video; map to raw demuxers
        return {"mpeg2video": "mpeg2video", "mpegvideo": "mpegvideo",
                "h264": "h264", "hevc": "hevc"}.get(codec_name, codec_name)
    if codec_type == "audio":
        return codec_name  # "ac3", "dts", "mp2", "pcm_s16be" all work as -f
    if codec_type == "subtitle":
        return None  # subpictures are messy; skip for now
    return None


def _extract_and_hash(mkv_path: Path, stream_index: int, raw_format: str,
                       *, ffmpeg_bin: str = "ffmpeg") -> tuple[str, int]:
    """Stream-extract the elementary bytes, return (sha256, byte_count)."""
    h = hashlib.sha256()
    total = 0
    proc = subprocess.Popen(
        [ffmpeg_bin, "-hide_banner", "-v", "error",
         "-i", str(mkv_path), "-map", f"0:{stream_index}",
         "-c", "copy", "-f", raw_format, "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read(65536)
        if not chunk:
            break
        h.update(chunk)
        total += len(chunk)
    proc.wait()
    return (h.hexdigest(), total)


def _count_frames(mkv_path: Path, stream_index: int,
                   *, ffprobe_bin: str = "ffprobe") -> int:
    """Use ffprobe -count_frames; works for video and audio."""
    try:
        out = subprocess.check_output(
            [ffprobe_bin, "-hide_banner", "-v", "error",
             "-select_streams", str(stream_index), "-count_frames",
             "-show_entries", "stream=nb_read_frames", "-of", "json",
             str(mkv_path)],
            text=True,
        )
        d = json.loads(out)
        if d.get("streams"):
            n = d["streams"][0].get("nb_read_frames")
            return int(n) if n and n != "N/A" else 0
    except (subprocess.CalledProcessError, json.JSONDecodeError, ValueError):
        pass
    return 0


def _infer_duration(stream: dict, frame_count: int) -> float:
    codec_type = stream.get("codec_type", "")
    if codec_type == "video":
        try:
            num, den = stream["r_frame_rate"].split("/")
            return frame_count * int(den) / int(num)
        except (KeyError, ValueError, ZeroDivisionError):
            return 0.0
    if codec_type == "audio":
        sr = int(stream.get("sample_rate", 0) or 0)
        if sr == 0:
            return 0.0
        codec = stream.get("codec_name", "")
        # AC3: 1536 samples/frame; DTS: 512 or 1024; MP2: 1152
        samples_per_frame = {
            "ac3": 1536, "eac3": 1536, "dts": 512,
            "mp2": 1152, "mp3": 1152, "pcm_s16be": 1,
        }.get(codec, 1)
        return frame_count * samples_per_frame / sr
    return 0.0


def fingerprint_mkv(mkv_path: Path,
                     *, ffmpeg_bin: str = "ffmpeg",
                     ffprobe_bin: str = "ffprobe") -> dict:
    """Fingerprint every video/audio stream of an MKV."""
    info = _ffprobe_json(mkv_path, ffprobe_bin=ffprobe_bin)
    streams: list[StreamFingerprint] = []
    for s in info.get("streams", []):
        codec_type = s.get("codec_type", "")
        if codec_type not in ("video", "audio"):
            continue
        codec_name = s.get("codec_name", "")
        raw_fmt = _ffmpeg_raw_format(codec_type, codec_name)
        if raw_fmt is None:
            continue
        idx = int(s.get("index", 0))
        sha, byte_count = _extract_and_hash(mkv_path, idx, raw_fmt,
                                              ffmpeg_bin=ffmpeg_bin)
        frame_count = _count_frames(mkv_path, idx, ffprobe_bin=ffprobe_bin)
        duration = _infer_duration(s, frame_count)
        streams.append(StreamFingerprint(
            index=idx, codec_type=codec_type, codec_name=codec_name,
            language=s.get("tags", {}).get("language", ""),
            sha256=sha, es_bytes=byte_count,
            decoded_frames=frame_count,
            duration_inferred_seconds=duration,
        ))
    return {
        "path": str(mkv_path),
        "size_bytes": int(info.get("format", {}).get("size", 0) or 0),
        "duration_seconds": float(info.get("format", {}).get("duration", 0) or 0),
        "streams": [s.to_dict() for s in streams],
    }


# ---------------------------------------------------------------------------
# Cross-comparison
# ---------------------------------------------------------------------------

def compare_fingerprints(fps: dict[str, dict]) -> dict:
    """Given {label: fingerprint_dict}, produce a comparison report."""
    labels = list(fps.keys())
    if len(labels) < 2:
        return {"labels": labels, "comparisons": []}

    # Stream lineup per fingerprint
    lineups = {
        lbl: [(s["codec_type"], s["codec_name"], s["language"]) for s in fp["streams"]]
        for lbl, fp in fps.items()
    }

    # Side-by-side per-stream (matched by order — both should agree on layout)
    max_streams = max(len(l) for l in lineups.values())
    comparisons = []
    for i in range(max_streams):
        row: dict = {"stream_position": i}
        # Pull each label's stream at position i (if exists)
        per_label = {}
        for lbl, fp in fps.items():
            if i < len(fp["streams"]):
                per_label[lbl] = fp["streams"][i]
        row["per_label"] = per_label

        # Group labels by sha256
        hash_groups: dict[str, list[str]] = {}
        for lbl, s in per_label.items():
            hash_groups.setdefault(s["sha256"], []).append(lbl)
        row["hash_groups"] = hash_groups
        row["all_identical"] = len(hash_groups) == 1
        # Compute deltas vs first label
        if labels[0] in per_label:
            ref = per_label[labels[0]]
            deltas = {}
            for lbl, s in per_label.items():
                if lbl == labels[0]:
                    continue
                deltas[lbl] = {
                    "bytes_delta": s["es_bytes"] - ref["es_bytes"],
                    "frames_delta": s["decoded_frames"] - ref["decoded_frames"],
                    "duration_delta_seconds": round(
                        s["duration_inferred_seconds"] - ref["duration_inferred_seconds"], 3
                    ),
                }
            row["vs_ref_deltas"] = deltas
        comparisons.append(row)
    return {"labels": labels, "comparisons": comparisons}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="compare-streams",
        description="Fingerprint and cross-compare elementary streams from MKV rips.",
    )
    ap.add_argument("mkvs", nargs="+", metavar="LABEL=PATH",
                    help="One or more `label=mkv_path` arguments")
    ap.add_argument("--ffmpeg-bin", default="ffmpeg",
                    help="ffmpeg binary for ES extraction (default: PATH)")
    ap.add_argument("--ffprobe-bin", default="ffprobe",
                    help="ffprobe binary (default: PATH)")
    ap.add_argument("--output", "-o", help="Write JSON report to this path")
    args = ap.parse_args(argv)

    rips: dict[str, Path] = {}
    for spec in args.mkvs:
        if "=" not in spec:
            print(f"error: expected LABEL=PATH, got {spec!r}", file=sys.stderr)
            return 2
        lbl, path_s = spec.split("=", 1)
        p = Path(path_s)
        if not p.exists():
            print(f"error: file not found: {p}", file=sys.stderr)
            return 2
        rips[lbl] = p

    print(f"Fingerprinting {len(rips)} MKV(s)...", file=sys.stderr)
    fps: dict[str, dict] = {}
    for lbl, p in rips.items():
        t0 = time.monotonic()
        fps[lbl] = fingerprint_mkv(p, ffmpeg_bin=args.ffmpeg_bin,
                                     ffprobe_bin=args.ffprobe_bin)
        elapsed = time.monotonic() - t0
        print(f"  [{lbl}] {p.name} ({len(fps[lbl]['streams'])} streams) "
              f"in {elapsed:.1f}s", file=sys.stderr)

    report = {
        "fingerprints": fps,
        "comparison": compare_fingerprints(fps),
    }

    out_json = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(out_json + "\n")
    else:
        print(out_json)

    _print_summary(report)
    return 0


def _print_summary(report: dict) -> None:
    """Tabular human summary on stderr."""
    cmp = report["comparison"]
    labels = cmp["labels"]
    print(file=sys.stderr)
    print("=== Per-stream summary ===", file=sys.stderr)
    print(f"{'pos':>4} {'codec':>10} {'lang':>5}  ", end="", file=sys.stderr)
    for lbl in labels:
        print(f"{lbl[:14]:>16}  ", end="", file=sys.stderr)
    print(file=sys.stderr)
    print("-" * (12 + 18 * len(labels)), file=sys.stderr)
    for row in cmp["comparisons"]:
        any_s = next(iter(row["per_label"].values()), {})
        kind = any_s.get("codec_name", "-")
        lang = any_s.get("language", "-")
        print(f"{row['stream_position']:>4} {kind:>10} {lang:>5}  ", end="", file=sys.stderr)
        for lbl in labels:
            s = row["per_label"].get(lbl)
            if s is None:
                print(f"{'(missing)':>16}  ", end="", file=sys.stderr)
            else:
                tag = "✓" if row["all_identical"] else "✗"
                print(f"{tag} {s['sha256'][:8]} {s['es_bytes']/1e6:>4.0f}MB  ", end="",
                      file=sys.stderr)
        print(file=sys.stderr)
        if not row["all_identical"]:
            for lbl, delta in row.get("vs_ref_deltas", {}).items():
                if delta["bytes_delta"] or delta["frames_delta"]:
                    print(f"      → {lbl} vs {labels[0]}: "
                          f"{delta['bytes_delta']:+,d}B "
                          f"{delta['frames_delta']:+,d}f "
                          f"{delta['duration_delta_seconds']:+.3f}s",
                          file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
