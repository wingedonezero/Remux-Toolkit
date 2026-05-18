"""
Walk a title's cells, parse MPEG-PS, and emit per-stream statistics. Built
as the *diagnostic foundation* for the demuxer/muxer work in Phase 4 — every
later decision about correctness has to be verifiable against the numbers
this tool produces.

CLI:
    python -m remux_toolkit.tools.ffmpeg_dvd_gui.core.analysis.inspect_title_streams \\
        /path/to/disc <title_num> [--include-nav] [--output report.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..demux.cell_reader import CellReader
from ..demux.ps_walker import (
    ESPayload, iter_es_payloads, stream_key, stream_kind, STREAM_PRIVATE_2,
)
from ...bindings import libdvdread as dr
from .inspector import _resolve_disc_path


SCHEMA = "remux-toolkit/dvd-stream-stats/v1"


# ---------------------------------------------------------------------------
# Per-stream stats accumulator
# ---------------------------------------------------------------------------

@dataclass
class StreamStats:
    stream_id: int
    substream_id: Optional[int]
    kind: str
    packet_count: int = 0
    byte_total: int = 0
    pts_first: Optional[int] = None
    pts_last: Optional[int] = None
    pts_gaps: list[tuple[int, int, int]] = field(default_factory=list)
    # (cell_index, prev_pts, new_pts) where new_pts <= prev_pts
    cell_indices: set = field(default_factory=set)
    nav_packets: int = 0  # only used for the NAV pseudo-stream

    def observe(self, pkt: ESPayload) -> None:
        self.packet_count += 1
        self.byte_total += len(pkt.es_bytes)
        self.cell_indices.add(pkt.cell_index)
        if pkt.pts is not None:
            if self.pts_first is None:
                self.pts_first = pkt.pts
            elif self.pts_last is not None and pkt.pts < self.pts_last:
                # Possible PTS rollover (33-bit wrap = 26.5 hours; or a real
                # discontinuity from a cell boundary). Record both.
                self.pts_gaps.append((pkt.cell_index, self.pts_last, pkt.pts))
            self.pts_last = pkt.pts

    def to_dict(self) -> dict:
        out = {
            "stream_id":    self.stream_id,
            "stream_id_hex": f"{self.stream_id:#04x}",
            "substream_id": self.substream_id,
            "substream_id_hex": f"{self.substream_id:#04x}" if self.substream_id is not None else None,
            "kind":         self.kind,
            "packet_count": self.packet_count,
            "byte_total":   self.byte_total,
            "cells_present": sorted(self.cell_indices),
        }
        if self.pts_first is not None:
            duration_ticks = (self.pts_last - self.pts_first) if self.pts_last else 0
            out["pts_first_ticks"] = self.pts_first
            out["pts_last_ticks"]  = self.pts_last
            out["pts_span_seconds"] = round(duration_ticks / 90000.0, 3)
        if self.pts_gaps:
            out["pts_anomalies"] = [
                {"at_cell": c, "prev_pts_ticks": p, "new_pts_ticks": n,
                 "delta_seconds": round((n - p) / 90000.0, 3)}
                for c, p, n in self.pts_gaps[:50]  # cap to keep JSON small
            ]
            out["pts_anomaly_count"] = len(self.pts_gaps)
        return out


# ---------------------------------------------------------------------------
# Cell-boundary tracker
# ---------------------------------------------------------------------------

@dataclass
class CellBoundary:
    """One observed cell transition. Records what each stream's last PTS was
    going into the new cell."""
    from_cell: int
    to_cell: int
    seamless: bool
    stc_discontinuity: bool
    streams_pts_into: dict  # stream_key -> last pts seen in from_cell
    streams_pts_out: dict   # stream_key -> first pts seen in to_cell


def walk_title(disc, title_num: int) -> dict:
    """Walk the title's full PS, accumulate stats, return a report dict."""
    streams: dict[tuple, StreamStats] = {}
    cell_boundaries: list[CellBoundary] = []
    last_pts_per_stream: dict[tuple, int] = {}
    current_cell: Optional[int] = None
    pending_boundary: Optional[CellBoundary] = None

    nav_count = 0
    sector_count = 0
    bytes_read = 0

    with CellReader(disc, title_num) as reader:
        for cell, sector in reader.iter_sectors():
            sector_count += 1
            bytes_read += len(sector)
            if current_cell is None:
                current_cell = cell.index
            elif cell.index != current_cell:
                # Cell transition. The first PTS we see in the new cell on
                # each stream pairs with the last we saw in the old cell.
                pending_boundary = CellBoundary(
                    from_cell=current_cell,
                    to_cell=cell.index,
                    seamless=cell.seamless_play,
                    stc_discontinuity=cell.stc_discontinuity,
                    streams_pts_into=dict(last_pts_per_stream),
                    streams_pts_out={},
                )
                cell_boundaries.append(pending_boundary)
                current_cell = cell.index

            for pkt in iter_es_payloads(iter([(cell, sector)])):
                if pkt.is_nav:
                    nav_count += 1
                    continue
                key = stream_key(pkt.stream_id, pkt.substream_id)
                if key not in streams:
                    streams[key] = StreamStats(
                        stream_id=pkt.stream_id,
                        substream_id=pkt.substream_id,
                        kind=stream_kind(pkt.stream_id, pkt.substream_id),
                    )
                streams[key].observe(pkt)
                if pkt.pts is not None:
                    last_pts_per_stream[key] = pkt.pts
                    if pending_boundary is not None and key not in pending_boundary.streams_pts_out:
                        pending_boundary.streams_pts_out[key] = pkt.pts

    return {
        "schema":     SCHEMA,
        "title_num":  title_num,
        "vts":        reader.vts_no,
        "pgc":        reader.pgc_no,
        "num_cells":  len(reader.cells),
        "cells": [
            {
                "index": c.index,
                "first_sector": c.first_sector,
                "last_sector":  c.last_sector,
                "num_sectors":  c.num_sectors,
                "block_type":   c.block_type,
                "block_mode":   c.block_mode,
                "seamless":     c.seamless_play,
                "stc_disc":     c.stc_discontinuity,
                "still_time":   c.still_time,
            }
            for c in reader.cells
        ],
        "totals": {
            "sectors":   sector_count,
            "bytes":     bytes_read,
            "nav_packs": nav_count,
        },
        "streams": [s.to_dict() for s in sorted(streams.values(),
                                                key=lambda s: (s.stream_id, s.substream_id or 0))],
        "cell_boundaries": [
            {
                "from_cell": b.from_cell,
                "to_cell":   b.to_cell,
                "seamless":  b.seamless,
                "stc_discontinuity": b.stc_discontinuity,
                "streams": [
                    {
                        "kind": stream_kind(k[0], None if k[1] == -1 else k[1]),
                        "stream_id_hex": f"{k[0]:#04x}",
                        "substream_id_hex": (f"{k[1]:#04x}" if k[1] != -1 else None),
                        "pts_into": b.streams_pts_into.get(k),
                        "pts_out":  b.streams_pts_out.get(k),
                        "delta_seconds": (round((b.streams_pts_out.get(k) - b.streams_pts_into.get(k)) / 90000.0, 3)
                                          if b.streams_pts_into.get(k) is not None
                                          and b.streams_pts_out.get(k) is not None
                                          else None),
                    }
                    for k in sorted(set(b.streams_pts_into.keys()) | set(b.streams_pts_out.keys()))
                ],
            }
            for b in cell_boundaries
        ],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="inspect-title-streams",
        description="Walk a DVD title's MPEG-PS and emit per-stream statistics.",
    )
    ap.add_argument("path", help="Disc path (folder, ISO, or VIDEO_TS)")
    ap.add_argument("title", type=int, help="libdvdread title number (1-based)")
    ap.add_argument("--output", "-o", help="Write JSON to file (default: stdout)")
    args = ap.parse_args(argv)

    src = _resolve_disc_path(Path(args.path))
    if src is None or isinstance(src, list):
        print(f"error: could not resolve disc path: {args.path}", file=sys.stderr)
        return 2

    with dr.open_disc(src) as disc:
        report = walk_title(disc, args.title)

    out_json = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(out_json + "\n")
    else:
        print(out_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
