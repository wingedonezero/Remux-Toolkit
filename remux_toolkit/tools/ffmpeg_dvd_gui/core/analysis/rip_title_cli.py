"""
Standalone CLI for the native title ripper. Useful for smoke testing the
demux + mux pipeline without involving the GUI.

    python -m remux_toolkit.tools.ffmpeg_dvd_gui.core.analysis.rip_title_cli \\
        /path/to/disc <title_num> -o out.mkv [--include-subs]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Optional

from ...bindings import libdvdread as dr
from ..analysis.inspector import _resolve_disc_path
from ..orchestrator import RipOptions, rip_title


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="rip-title",
        description="Native DVD title ripper (libdvdread + MPEG-PS walker + ffmpeg pipe mux).",
    )
    ap.add_argument("path", help="Disc path (folder, ISO, or VIDEO_TS)")
    ap.add_argument("title", type=int, help="libdvdread title number (1-based)")
    ap.add_argument("-o", "--output", required=True, help="Output MKV path")
    ap.add_argument("--include-subs", action=argparse.BooleanOptionalAction, default=True,
                    help="Include DVD subpicture streams via ffmpeg-dvdvideo "
                         "side-channel (default: include; use --no-include-subs to skip)")
    ap.add_argument("--no-chapters", action="store_true",
                    help="Skip the FFmetadata chapter input")
    ap.add_argument("--quiet", "-q", action="store_true",
                    help="Suppress per-line ffmpeg log; show only final summary")
    args = ap.parse_args(argv)

    src = _resolve_disc_path(Path(args.path))
    if src is None or isinstance(src, list):
        print(f"error: could not resolve disc path: {args.path}", file=sys.stderr)
        return 2

    last_progress_print = [0.0]

    def log(sev: str, line: str) -> None:
        if args.quiet:
            return
        print(f"[{sev}] {line}", file=sys.stderr)

    def progress(done: int, total: int) -> None:
        now = time.monotonic()
        if now - last_progress_print[0] < 0.5 and done < total:
            return
        last_progress_print[0] = now
        pct = (done / total * 100) if total else 0
        print(f"\r  read {done:,} / {total:,} sectors ({pct:5.1f}%)",
              end="", file=sys.stderr, flush=True)
        if done >= total:
            print(file=sys.stderr)

    opts = RipOptions(
        include_subpictures=args.include_subs,
        write_chapters=not args.no_chapters,
        log_callback=log,
        progress_callback=progress,
    )

    started = time.monotonic()
    with dr.open_disc(str(src)) as disc:
        result = rip_title(disc, args.title, Path(args.output),
                           options=opts, disc_source_path=str(src))
    elapsed = time.monotonic() - started

    print(file=sys.stderr)
    print(f"=== Rip summary ===", file=sys.stderr)
    print(f"  output:        {result.output_path}", file=sys.stderr)
    print(f"  ffmpeg rc:     {result.ffmpeg_returncode}", file=sys.stderr)
    print(f"  sectors read:  {result.sectors_read:,}", file=sys.stderr)
    print(f"  bytes read:    {result.bytes_read:,}  ({result.bytes_read/1e6:.1f} MB)", file=sys.stderr)
    print(f"  elapsed:       {elapsed:.1f}s", file=sys.stderr)
    if result.cancelled:
        print(f"  CANCELLED", file=sys.stderr)
    if result.error:
        print(f"  error: {result.error}", file=sys.stderr)
    if result.audio_dedup_drops:
        print(f"  audio dedup drops: {result.audio_dedup_drops}", file=sys.stderr)
    if result.streams:
        print("  per-stream stats:", file=sys.stderr)
        for s in result.streams:
            sub = s.key[1] if s.key[1] != -1 else None
            sub_s = f"0x{sub:02X}" if sub is not None else "  - "
            print(f"    sid=0x{s.key[0]:02X} sub={sub_s} {s.codec_name:>8} "
                  f"lang={s.language or '-':<3} "
                  f"delay={s.delay_seconds*1000:+7.1f}ms "
                  f"pkts={s.packets_written:>10,} bytes={s.bytes_written/1e6:>8.1f}MB",
                  file=sys.stderr)
    if result.ffmpeg_returncode != 0 and result.ffmpeg_stderr_tail:
        print(f"  ffmpeg stderr tail:\n{result.ffmpeg_stderr_tail}", file=sys.stderr)

    return 0 if result.ffmpeg_returncode == 0 and not result.error else 1


if __name__ == "__main__":
    raise SystemExit(main())
