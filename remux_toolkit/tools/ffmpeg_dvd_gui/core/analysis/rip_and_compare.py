"""
Rip-and-compare driver — byte-level validation against MakeMKV.

Drives both rip pipelines (MakeMKV via ``makemkvcon mkv`` + ours via the
native ``rip_title`` path), then hashes the resulting MKVs via
``compare_streams.fingerprint_mkv`` and reports whether the video + audio
elementary-stream SHA-256s match.

The purpose: verify byte-for-byte equivalence with MakeMKV on real discs
after each demux / mux change. Run this manually per disc; not wired into
pytest because it depends on a real disc + makemkvcon.

Usage:

    python -m remux_toolkit.tools.ffmpeg_dvd_gui.core.analysis.rip_and_compare \\
        --disc /path/to/disc \\
        --our-title 7 \\
        --mkv-title 6 \\
        --output-dir /tmp/rip_cmp

    # If both MKVs already exist (skip rips):
    python -m ... rip_and_compare \\
        --ours /path/to/ours.mkv --makemkv /path/to/makemkv.mkv

The MakeMKV-side title id is generally the *kept-titles* 0-based index, not
the libdvdread title number. Use ``makemkvcon -r info file:<disc>`` (or our
captured ``mmcon_titles.json``) to look it up.

Exit code: 0 if video + audio streams all hash-match between the two rips,
non-zero otherwise.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .compare_streams import compare_fingerprints, fingerprint_mkv


# ---------------------------------------------------------------------------
# Rip drivers
# ---------------------------------------------------------------------------

def rip_with_makemkv(disc: Path, mkv_title: int, output_dir: Path,
                     *, makemkvcon: str = "/usr/bin/makemkvcon",
                     minlength: int = 120,
                     stdout=sys.stderr) -> Path:
    """Drive ``makemkvcon mkv`` to rip one title. Returns the produced MKV.

    MakeMKV writes its output filename as ``title_t<NN>.mkv`` (or similar);
    we glob for the newest .mkv in ``output_dir`` after the call.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    # Snapshot existing mkvs so we can identify the new one
    pre_existing = set(output_dir.glob("*.mkv"))

    source = f"iso:{disc}" if disc.suffix.lower() == ".iso" else f"file:{disc}"
    cmd = [
        makemkvcon, "-r", f"--minlength={minlength}",
        "mkv", source, str(mkv_title), str(output_dir),
    ]
    print(f"[mkv] {' '.join(cmd)}", file=stdout)
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.monotonic() - t0
    print(f"[mkv] makemkvcon rc={proc.returncode} in {elapsed:.1f}s", file=stdout)
    if proc.returncode != 0:
        print(proc.stdout, file=stdout)
        print(proc.stderr, file=stdout)
        raise RuntimeError(f"makemkvcon mkv failed (rc={proc.returncode})")

    new_mkvs = sorted(set(output_dir.glob("*.mkv")) - pre_existing,
                      key=lambda p: p.stat().st_mtime)
    if not new_mkvs:
        raise RuntimeError(
            f"makemkvcon mkv reported success but no new .mkv in {output_dir}"
        )
    # Rename to canonical "makemkv.mkv" so the comparison report is consistent
    src_mkv = new_mkvs[-1]
    dst_mkv = output_dir / "makemkv.mkv"
    if dst_mkv.exists():
        dst_mkv.unlink()
    src_mkv.rename(dst_mkv)
    return dst_mkv


def rip_with_ours(disc: Path, our_title: int, output_mkv: Path,
                  *, include_subs: bool = False,
                  stdout=sys.stderr) -> Path:
    """Drive our native ``rip_title_native`` to rip one title.

    Uses the MakeMKV-equivalent native pipeline (CellReader → ps_walker →
    DvdFrameSource → MkvWriter → libmkv_shim). No ffmpeg subprocess.
    """
    from .inspector import _resolve_disc_path
    from ..native_rip import NativeRipOptions, rip_title_native

    src = _resolve_disc_path(disc)
    if src is None or isinstance(src, list):
        raise RuntimeError(f"could not resolve disc path: {disc}")

    def log(sev: str, line: str) -> None:
        print(f"[ours:{sev}] {line}", file=stdout)

    opts = NativeRipOptions(
        include_subpictures=include_subs,
        # CC merging happens post-rip via ccextractor + mkvmerge; turn off
        # for the byte-compare harness so the post-pass doesn't alter the
        # MKV (CC merging rewrites it).
        include_closed_captions=False,
        write_chapters=True,
        log_callback=log,
    )
    output_mkv.parent.mkdir(parents=True, exist_ok=True)
    print(f"[ours] ripping title {our_title} → {output_mkv}", file=stdout)
    t0 = time.monotonic()
    result = rip_title_native(
        str(src), our_title, output_mkv, options=opts,
        title_name=f"Title {our_title}",
    )
    elapsed = time.monotonic() - t0
    print(f"[ours] success={result.success} in {elapsed:.1f}s", file=stdout)
    if not result.success:
        raise RuntimeError(f"native rip failed: {result.error_message}")
    return output_mkv


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def compare_rips(ours: Path, makemkv: Path,
                 *, report_path: Optional[Path] = None,
                 stdout=sys.stderr) -> dict:
    """Fingerprint both MKVs and produce the comparison report."""
    print(f"[cmp] fingerprinting {ours.name}...", file=stdout)
    ours_fp = fingerprint_mkv(ours)
    print(f"[cmp] fingerprinting {makemkv.name}...", file=stdout)
    mkv_fp = fingerprint_mkv(makemkv)
    fps = {"ours": ours_fp, "makemkv": mkv_fp}
    report = {
        "fingerprints": fps,
        "comparison": compare_fingerprints(fps),
    }
    if report_path is not None:
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(f"[cmp] wrote report → {report_path}", file=stdout)
    return report


def _summarize(report: dict, *, stdout=sys.stderr) -> tuple[int, int]:
    """Print human summary. Returns (matched_streams, total_streams)."""
    comparisons = report["comparison"]["comparisons"]
    matched = 0
    total = 0
    print(file=stdout)
    print("=== Stream-by-stream hash diff ===", file=stdout)
    for row in comparisons:
        pos = row["stream_position"]
        per_label = row["per_label"]
        ours = per_label.get("ours")
        mkv = per_label.get("makemkv")
        if ours is None or mkv is None:
            print(f"  [{pos}] (missing on one side: ours={'Y' if ours else 'N'} "
                  f"makemkv={'Y' if mkv else 'N'})", file=stdout)
            total += 1
            continue
        total += 1
        if ours["codec_type"] not in ("video", "audio", "subtitle"):
            continue
        identical = row["all_identical"]
        marker = "OK" if identical else "DIFF"
        print(f"  [{pos}] {ours['codec_type']:8} {ours['codec_name']:>10} "
              f"lang={ours['language'] or '-':3} "
              f"{marker} ours={ours['sha256'][:12]} mkv={mkv['sha256'][:12]} "
              f"Δb={mkv['es_bytes']-ours['es_bytes']:+,} "
              f"Δf={mkv['decoded_frames']-ours['decoded_frames']:+}",
              file=stdout)
        if identical:
            matched += 1
    print(file=stdout)
    if matched == total:
        print(f"=== PASS — {matched}/{total} streams hash-match ===",
              file=stdout)
    else:
        print(f"=== FAIL — {matched}/{total} streams hash-match ===",
              file=stdout)
    return matched, total


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="rip-and-compare",
        description="Rip a DVD title with MakeMKV + ours and compare ES hashes.",
    )
    # End-to-end mode
    ap.add_argument("--disc", type=Path, help="Disc path for both rips")
    ap.add_argument("--our-title", type=int,
                    help="libdvdread title number (1-based) for our path")
    ap.add_argument("--mkv-title", type=int,
                    help="MakeMKV kept-titles id (0-based) for makemkvcon mkv")
    ap.add_argument("--output-dir", type=Path,
                    help="Where to drop ours.mkv + makemkv.mkv + report.json")

    # Compare-only mode
    ap.add_argument("--ours", type=Path,
                    help="Pre-existing ours.mkv (skip our-side rip)")
    ap.add_argument("--makemkv", type=Path,
                    help="Pre-existing makemkv.mkv (skip mkv-side rip)")

    # Options
    ap.add_argument("--include-subs", action="store_true",
                    help="Include subpicture streams in our rip (default: off)")
    ap.add_argument("--makemkvcon", default="/usr/bin/makemkvcon",
                    help="Path to makemkvcon binary")
    ap.add_argument("--minlength", type=int, default=120,
                    help="--minlength passed to makemkvcon (default: 120s)")
    ap.add_argument("--report", type=Path,
                    help="Write full JSON report to this path")
    args = ap.parse_args(argv)

    if args.ours and args.makemkv:
        # Compare-only mode
        ours_mkv = args.ours
        mkv_mkv = args.makemkv
    else:
        # End-to-end mode — need disc + titles + output_dir
        if not all([args.disc, args.our_title, args.mkv_title is not None,
                    args.output_dir]):
            ap.error(
                "End-to-end mode requires --disc, --our-title, --mkv-title, "
                "--output-dir (or supply --ours + --makemkv for compare-only)"
            )
        args.output_dir.mkdir(parents=True, exist_ok=True)
        mkv_mkv = rip_with_makemkv(
            args.disc, args.mkv_title, args.output_dir,
            makemkvcon=args.makemkvcon, minlength=args.minlength,
        )
        ours_mkv = rip_with_ours(
            args.disc, args.our_title, args.output_dir / "ours.mkv",
            include_subs=args.include_subs,
        )

    report_path = args.report or (
        args.output_dir / "report.json" if args.output_dir else None
    )
    report = compare_rips(ours_mkv, mkv_mkv, report_path=report_path)
    matched, total = _summarize(report)
    return 0 if matched == total and total > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
