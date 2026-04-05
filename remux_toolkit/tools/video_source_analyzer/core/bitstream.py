"""Layer 1: MPEG-2 Bitstream Flag Analysis via ffprobe (no decode, ~30s)."""

from __future__ import annotations

import subprocess
import time
from collections import Counter
from typing import Callable, Optional

from .models import (
    BitstreamResult, FrameFlags, Segment, StreamInfo,
    TRF_CYCLE_MASK, SEGMENT_WINDOW, FILM_SEGMENT_PCT, VIDEO_SEGMENT_PCT,
)


def run_layer1(
    filepath: str,
    stream_info: StreamInfo,
    include_per_frame: bool = True,
    on_progress: Optional[Callable[[str], None]] = None,
) -> BitstreamResult:
    """
    DGIndex-style MPEG-2 bitstream flag analysis via ffprobe.

    Reads picture_coding_extension headers from the bitstream without
    decoding video. Computes Film% via trf cycling, builds segment map.
    """
    bs = BitstreamResult()
    t0 = time.time()

    if on_progress:
        on_progress("Reading bitstream flags via ffprobe...")

    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_frames",
        "-show_entries",
        "frame=pict_type,interlaced_frame,top_field_first,"
        "repeat_pict,key_frame,pts_time,pkt_size",
        "-of", "csv=p=0",
        filepath,
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        bs.error = "timeout (>10min)"
        bs.elapsed_sec = time.time() - t0
        return bs
    except Exception as e:
        bs.error = str(e)
        bs.elapsed_sec = time.time() - t0
        return bs

    if proc.returncode != 0:
        bs.error = f"ffprobe exit {proc.returncode}"
        bs.elapsed_sec = time.time() - t0
        return bs

    # ── Parse CSV lines ────────────────────────────────────────────────
    # ffprobe CSV column order (verified empirically):
    #   0: key_frame, 1: pts_time, 2: pkt_size, 3: pict_type,
    #   4: interlaced_frame, 5: top_field_first, 6: repeat_pict
    raw_lines = proc.stdout.strip().split("\n")
    frames: list[FrameFlags] = []
    parse_errors = 0

    for idx, line in enumerate(raw_lines):
        if not line.strip():
            continue
        parts = line.split(",")
        if len(parts) < 7:
            parse_errors += 1
            continue
        try:
            ff = FrameFlags(
                index=idx,
                key_frame=int(parts[0].strip()) if parts[0].strip() else 0,
                pts_time=float(parts[1].strip()) if parts[1].strip() else 0.0,
                pkt_size=int(parts[2].strip()) if parts[2].strip() else 0,
                pict_type=parts[3].strip(),
                interlaced_frame=int(parts[4].strip()) if parts[4].strip() else 0,
                top_field_first=int(parts[5].strip()) if parts[5].strip() else 0,
                repeat_pict=int(parts[6].strip()) if parts[6].strip() else 0,
            )
            frames.append(ff)
        except (ValueError, IndexError):
            parse_errors += 1
            continue

    bs.coded_frames = len(frames)
    if bs.coded_frames == 0:
        bs.error = "no frames parsed"
        bs.elapsed_sec = time.time() - t0
        return bs

    if on_progress:
        on_progress(f"Parsed {bs.coded_frames:,} frames, computing metrics...")

    # ── DGIndex trf cycling algorithm ──────────────────────────────────
    old_trf = -1
    cycling = 0
    not_cycling = 0

    # Counters
    prog_count = 0
    intl_count = 0
    tff_count = 0
    bff_count = 0
    field_rpts = 0
    frame_rpts = 0
    i_count = 0
    p_count = 0
    b_count = 0
    flag_combos: Counter[str] = Counter()
    trf_dist: Counter[int] = Counter()

    per_frame_cycling: list[Optional[bool]] = []

    for ff in frames:
        trf = ff.trf
        trf_dist[trf] += 1

        # trf cycling check
        if old_trf >= 0:
            expected = (old_trf + 1) & TRF_CYCLE_MASK
            is_cycling = (trf & TRF_CYCLE_MASK) == expected
            if is_cycling:
                cycling += 1
            else:
                not_cycling += 1
            per_frame_cycling.append(is_cycling)
        else:
            per_frame_cycling.append(None)
        old_trf = trf

        # Count frame types
        if ff.is_progressive:
            prog_count += 1
        else:
            intl_count += 1

        if ff.is_tff:
            tff_count += 1
        else:
            bff_count += 1

        # Field repeats
        if ff.repeat_pict > 0:
            field_rpts += ff.repeat_pict
        if ff.repeat_pict >= 2:
            frame_rpts += 1

        # Picture type
        pt = ff.pict_type.upper()
        if pt == "I":
            i_count += 1
        elif pt == "P":
            p_count += 1
        elif pt == "B":
            b_count += 1

        # Flag combo tracking
        frame_type = "progressive" if ff.is_progressive else "interlaced"
        combo = f"{frame_type}_TFF{ff.top_field_first}_RFF{1 if ff.is_rff else 0}"
        flag_combos[combo] += 1

    # ── Compute metrics ────────────────────────────────────────────────
    total = bs.coded_frames
    total_classified = cycling + not_cycling

    bs.film_pct = round((cycling / total_classified * 100) if total_classified > 0 else 0.0, 2)
    bs.cycling_count = cycling
    bs.not_cycling_count = not_cycling
    bs.field_rpts = field_rpts
    bs.frame_rpts = frame_rpts
    bs.playback_frames = total + field_rpts // 2
    bs.progressive_frames = prog_count
    bs.interlaced_frames = intl_count
    bs.progressive_pct = round(prog_count / total * 100, 2) if total else 0.0
    bs.interlaced_pct = round(intl_count / total * 100, 2) if total else 0.0
    bs.tff_frames = tff_count
    bs.bff_frames = bff_count
    bs.dominant_field_order = "TFF" if tff_count >= bff_count else "BFF"
    bs.i_frames = i_count
    bs.p_frames = p_count
    bs.b_frames = b_count
    bs.flag_combos = dict(flag_combos.most_common())
    bs.trf_distribution = {str(k): v for k, v in sorted(trf_dist.items())}

    # ── Segment detection (sliding window) ─────────────────────────────
    fps = stream_info.fps if stream_info.fps > 0 else 29.97
    segments: list[Segment] = []
    current_type: Optional[str] = None
    seg_start = 0

    for win_start in range(0, total, SEGMENT_WINDOW):
        win_end = min(win_start + SEGMENT_WINDOW, total)
        chunk_size = win_end - win_start

        cycling_in_chunk = sum(
            1 for i in range(win_start, win_end)
            if per_frame_cycling[i] is True
        )
        intl_in_chunk = sum(
            1 for i in range(win_start, win_end)
            if frames[i].interlaced_frame == 1
        )

        cycling_pct = cycling_in_chunk / chunk_size * 100
        intl_pct = intl_in_chunk / chunk_size * 100

        if cycling_pct > FILM_SEGMENT_PCT:
            seg_type = "FILM"
        elif cycling_pct > VIDEO_SEGMENT_PCT:
            seg_type = "MIXED"
        else:
            seg_type = "VIDEO"

        if seg_type != current_type:
            if current_type is not None:
                seg_size = win_start - seg_start
                if seg_size > 0:
                    seg_cyc = sum(
                        1 for i in range(seg_start, win_start)
                        if per_frame_cycling[i] is True
                    )
                    seg_intl = sum(
                        1 for i in range(seg_start, win_start)
                        if frames[i].interlaced_frame == 1
                    )
                    segments.append(Segment(
                        start_frame=seg_start,
                        end_frame=win_start,
                        seg_type=current_type,
                        cycling_pct=round(seg_cyc / seg_size * 100, 1),
                        interlaced_pct=round(seg_intl / seg_size * 100, 1),
                        duration_sec=round(seg_size / fps, 1),
                    ))
            seg_start = win_start
            current_type = seg_type

    # Close final segment
    if current_type is not None:
        seg_size = total - seg_start
        if seg_size > 0:
            seg_cyc = sum(
                1 for i in range(seg_start, total)
                if per_frame_cycling[i] is True
            )
            seg_intl = sum(
                1 for i in range(seg_start, total)
                if frames[i].interlaced_frame == 1
            )
            segments.append(Segment(
                start_frame=seg_start,
                end_frame=total,
                seg_type=current_type,
                cycling_pct=round(seg_cyc / seg_size * 100, 1),
                interlaced_pct=round(seg_intl / seg_size * 100, 1),
                duration_sec=round(seg_size / fps, 1),
            ))

    bs.segments = segments

    # ── Per-frame data for export ──────────────────────────────────────
    if include_per_frame:
        for i, ff in enumerate(frames):
            bs.per_frame.append({
                "idx": ff.index,
                "pict_type": ff.pict_type,
                "interlaced_frame": ff.interlaced_frame,
                "top_field_first": ff.top_field_first,
                "repeat_pict": ff.repeat_pict,
                "key_frame": ff.key_frame,
                "pts_time": round(ff.pts_time, 6),
                "pkt_size": ff.pkt_size,
                "trf": ff.trf,
                "cycling": per_frame_cycling[i],
            })

    bs.elapsed_sec = round(time.time() - t0, 2)

    if on_progress:
        on_progress(f"Layer 1 complete: Film% = {bs.film_pct:.2f}% ({bs.elapsed_sec:.1f}s)")

    return bs
