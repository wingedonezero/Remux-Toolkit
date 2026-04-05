"""Data models for the Video Source Analyzer."""

from __future__ import annotations

import json
import time
import os
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ═══════════════════════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════════════════════

class ContentType(str, Enum):
    SOFT_TELECINE = "soft_telecine"
    SOFT_TELECINE_MIXED = "soft_telecine_mixed"
    HARD_TELECINE = "hard_telecine"
    INTERLACED = "interlaced"
    PROGRESSIVE = "progressive"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SegmentType(str, Enum):
    FILM = "FILM"
    VIDEO = "VIDEO"
    MIXED = "MIXED"


# ═══════════════════════════════════════════════════════════════════════════
# Constants / Thresholds
# ═══════════════════════════════════════════════════════════════════════════

# DGIndex trf cycling
TRF_CYCLE_MASK = 3

# Segment detection
SEGMENT_WINDOW = 500
FILM_SEGMENT_PCT = 20.0
VIDEO_SEGMENT_PCT = 5.0

# Classification thresholds
FILM_PCT_HIGH = 80.0
FILM_PCT_MED = 50.0
FILM_PCT_LOW = 30.0

# Layer 2: Naranjo chi-square + autocorrelation
COMBING_RATIO_THRESHOLD = 1.5
CHI_SQUARE_ALPHA = 0.02
MAD_SCALE = 1.4826

# Layer 2: Duplicate field detection
DUP_FIELD_SAD_THRESHOLD = 0.5
DUP_FIELD_TELECINE_PCT = 15.0
DUP_FIELD_INTERLACED_PCT = 3.0
DUP_FIELD_PERIOD5_RATIO = 2.0

# Layer 3: Field-swap validation
FIXED_THRESHOLD = 0.85
MIN_COMBED_FOR_FIELDSWAP = 30
FIELDSWAP_TELECINE_PCT = 80.0
FIELDSWAP_INTERLACED_PCT = 20.0


# ═══════════════════════════════════════════════════════════════════════════
# Data Structures
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class StreamInfo:
    """Container/stream metadata from MediaInfo (instant, no decode)."""
    codec: str = ""
    codec_id: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    fps_mode: str = ""
    fps_original: str = ""
    scan_type: str = ""
    scan_order: str = ""
    duration_sec: float = 0.0
    frame_count: int = 0
    bit_depth: int = 8

    @property
    def is_mpeg2(self) -> bool:
        return "MPEG" in self.codec.upper() or "V_MPEG2" in self.codec_id.upper()

    @property
    def is_vfr(self) -> bool:
        return self.fps_mode.upper() == "VFR"

    @property
    def is_interlaced(self) -> bool:
        return self.scan_type.lower() == "interlaced"

    @property
    def has_pulldown(self) -> bool:
        return "pulldown" in self.scan_order.lower()


@dataclass
class FrameFlags:
    """Per-frame MPEG-2 bitstream flags from ffprobe."""
    index: int
    pict_type: str = ""
    interlaced_frame: int = 0
    top_field_first: int = 0
    repeat_pict: int = 0
    key_frame: int = 0
    pts_time: float = 0.0
    pkt_size: int = 0

    @property
    def trf(self) -> int:
        rff = 1 if self.repeat_pict > 0 else 0
        return (self.top_field_first << 1) | rff

    @property
    def is_rff(self) -> bool:
        return self.repeat_pict > 0

    @property
    def is_progressive(self) -> bool:
        return self.interlaced_frame == 0

    @property
    def is_tff(self) -> bool:
        return self.top_field_first == 1


@dataclass
class Segment:
    """A contiguous region of frames with consistent content type."""
    start_frame: int
    end_frame: int
    seg_type: str
    cycling_pct: float
    interlaced_pct: float
    frame_count: int = 0
    duration_sec: float = 0.0

    def __post_init__(self):
        if self.frame_count == 0:
            self.frame_count = self.end_frame - self.start_frame


@dataclass
class BitstreamResult:
    """Results from Layer 1: MPEG-2 bitstream flag analysis."""
    film_pct: float = 0.0
    field_rpts: int = 0
    frame_rpts: int = 0
    coded_frames: int = 0
    playback_frames: int = 0
    cycling_count: int = 0
    not_cycling_count: int = 0
    progressive_frames: int = 0
    interlaced_frames: int = 0
    progressive_pct: float = 0.0
    interlaced_pct: float = 0.0
    tff_frames: int = 0
    bff_frames: int = 0
    dominant_field_order: str = ""
    i_frames: int = 0
    p_frames: int = 0
    b_frames: int = 0
    flag_combos: dict[str, int] = field(default_factory=dict)
    trf_distribution: dict[str, int] = field(default_factory=dict)
    segments: list[Segment] = field(default_factory=list)
    per_frame: list[dict] = field(default_factory=list)
    elapsed_sec: float = 0.0
    error: str = ""


@dataclass
class PixelResult:
    """Results from Layer 2: Naranjo chi-square + autocorrelation + dup fields."""
    # Chi-square energy test
    chi_square_detected: bool = False
    energy_ratio: float = 0.0
    std_ratio: float = 0.0
    chi_square_detail: dict = field(default_factory=dict)

    # Field difference x[n] statistics
    xn_mean: float = 0.0
    xn_std: float = 0.0
    xn_median: float = 0.0

    # Autocorrelation — field difference x[n]
    xn_autocorrelation: dict[str, float] = field(default_factory=dict)
    xn_lag5: float = 0.0
    xn_lag1: float = 0.0
    xn_lag5_lag1_ratio: float = 0.0

    # Autocorrelation — combing ratio
    comb_autocorrelation: dict[str, float] = field(default_factory=dict)
    comb_lag5: float = 0.0
    comb_lag1: float = 0.0
    comb_lag5_lag1_ratio: float = 0.0
    has_variance: bool = False

    # Duplicate field detection
    dup_field_pct: float = 0.0
    dup_field_pct_02: float = 0.0
    dup_field_pct_10: float = 0.0
    dup_field_autocorrelation: dict[str, float] = field(default_factory=dict)
    dup_field_lag5: float = 0.0
    dup_field_lag1: float = 0.0
    dup_field_lag5_lag1_ratio: float = 0.0
    dup_field_has_period5: bool = False
    dup_field_top_median_sad: float = 0.0
    dup_field_bot_median_sad: float = 0.0
    dup_field_top_p5_sad: float = 0.0
    dup_field_bot_p5_sad: float = 0.0

    # Combing stats (for Layer 3 handoff)
    combed_frames: int = 0
    combed_pct: float = 0.0
    median_ratio: float = 0.0
    combed_indices: list[int] = field(default_factory=list)

    # Per-frame data
    per_frame: list[dict] = field(default_factory=list)

    # Timing
    total_frames: int = 0
    elapsed_sec: float = 0.0
    frames_per_sec: float = 0.0
    error: str = ""


@dataclass
class FieldSwapResult:
    """Results from Layer 3: Field-swap validation."""
    total_combed: int = 0
    degenerate_skipped: int = 0
    eligible_combed: int = 0
    sampled: int = 0
    tested: int = 0
    fixed_count: int = 0
    unfixable_count: int = 0
    fix_pct: float = -1.0
    insufficient_data: bool = True
    swap_results: list[dict] = field(default_factory=list)
    elapsed_sec: float = 0.0
    error: str = ""


@dataclass
class ClassificationResult:
    """Classification output from the classifier."""
    classification: str = ContentType.UNKNOWN.value
    confidence: str = Confidence.LOW.value
    reason: str = ""
    film_source: str = ""
    video_source: str = ""
    film_pct: float = 0.0
    video_pct: float = 0.0
    film_frames: int = 0
    video_frames: int = 0
    mixed_frames: int = 0


@dataclass
class AnalysisResult:
    """Top-level container for all analysis results."""
    file_path: str = ""
    file_name: str = ""
    stream_info: StreamInfo = field(default_factory=StreamInfo)
    bitstream: Optional[BitstreamResult] = None
    pixel: Optional[PixelResult] = None
    field_swap: Optional[FieldSwapResult] = None
    classification: ClassificationResult = field(default_factory=ClassificationResult)
    layer1_ran: bool = False
    layer2_ran: bool = False
    layer3_ran: bool = False
    total_elapsed_sec: float = 0.0

    def to_dict(self, include_per_frame: bool = False) -> dict:
        """Serialize to a plain dict for JSON export."""
        d = _dataclass_to_dict(self)
        if not include_per_frame:
            if d.get("bitstream"):
                d["bitstream"].pop("per_frame", None)
            if d.get("pixel"):
                d["pixel"].pop("per_frame", None)
        return d

    def to_json(self, include_per_frame: bool = False, indent: int = 2) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(include_per_frame), indent=indent, default=str)

    def summary_text(self) -> str:
        """Build a human-readable summary log."""
        return _build_summary(self)


def _dataclass_to_dict(obj) -> dict:
    """Recursively convert dataclasses to dicts."""
    if hasattr(obj, "__dataclass_fields__"):
        result = {}
        for k in obj.__dataclass_fields__:
            val = getattr(obj, k)
            result[k] = _dataclass_to_dict(val)
        return result
    elif isinstance(obj, list):
        return [_dataclass_to_dict(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: _dataclass_to_dict(v) for k, v in obj.items()}
    elif isinstance(obj, Enum):
        return obj.value
    else:
        return obj


def _build_summary(result: AnalysisResult) -> str:
    """Build DGIndex-style summary log text from an AnalysisResult."""
    lines = []
    W = 70
    si = result.stream_info
    cls = result.classification

    lines.append("=" * W)
    lines.append("  VIDEO CONTENT CLASSIFICATION — RESULTS")
    lines.append("=" * W)
    lines.append(f"  Date:           {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  File:           {result.file_name}")
    lines.append(f"  Path:           {result.file_path}")
    lines.append("")

    # Stream Info
    lines.append("  -- Stream Info (MediaInfo) --")
    lines.append(f"  Codec:          {si.codec} ({si.codec_id})")
    lines.append(f"  Resolution:     {si.width}x{si.height}")
    lines.append(f"  Frame Rate:     {si.fps} fps ({si.fps_mode})")
    if si.fps_original:
        lines.append(f"  Original FPS:   {si.fps_original}")
    lines.append(f"  Scan Type:      {si.scan_type}")
    lines.append(f"  Scan Order:     {si.scan_order}")
    dur = si.duration_sec
    lines.append(f"  Duration:       {dur:.1f}s ({dur / 60:.1f} min)")
    lines.append(f"  Frame Count:    {si.frame_count:,}")
    lines.append(f"  MPEG-2:         {si.is_mpeg2}")
    lines.append(f"  VFR:            {si.is_vfr}")
    lines.append(f"  Pulldown:       {si.has_pulldown}")
    lines.append("")

    # Layer 1
    bs = result.bitstream
    if bs and not bs.error:
        lines.append("  -- Layer 1: Bitstream Flags (DGIndex-equivalent) --")
        lines.append(f"  Film%:          {bs.film_pct:.2f}%")
        lines.append(f"  Field Rpts:     {bs.field_rpts:,}")
        lines.append(f"  Frame Rpts:     {bs.frame_rpts:,}")
        lines.append(f"  Coded Frames:   {bs.coded_frames:,}")
        lines.append(f"  Playback:       {bs.playback_frames:,}")
        lines.append(f"  Field Order:    {bs.dominant_field_order}")
        lines.append(f"  Time:           {bs.elapsed_sec:.1f}s")
        lines.append("")

        total = bs.coded_frames or 1
        lines.append("  -- Frame Type Distribution --")
        lines.append(f"  Progressive:    {bs.progressive_frames:,} ({bs.progressive_pct:.1f}%)")
        lines.append(f"  Interlaced:     {bs.interlaced_frames:,} ({bs.interlaced_pct:.1f}%)")
        lines.append(f"  TFF:            {bs.tff_frames:,}")
        lines.append(f"  BFF:            {bs.bff_frames:,}")
        lines.append("")
        lines.append(f"  I-frames:       {bs.i_frames:,}")
        lines.append(f"  P-frames:       {bs.p_frames:,}")
        lines.append(f"  B-frames:       {bs.b_frames:,}")
        lines.append("")

        lines.append("  -- trf Cycling (DGIndex Algorithm) --")
        lines.append(f"  Cycling:        {bs.cycling_count:,} (FILM)")
        lines.append(f"  Not cycling:    {bs.not_cycling_count:,} (VIDEO)")
        lines.append("")

        if bs.flag_combos:
            lines.append("  -- Flag Combo Distribution --")
            for combo, count in sorted(bs.flag_combos.items(), key=lambda x: -x[1]):
                pct = count / total * 100
                lines.append(f"    {combo:<35} {count:>8} ({pct:>5.1f}%)")
            lines.append("")

        if bs.trf_distribution:
            lines.append("  -- TRF Value Distribution --")
            trf_names = {
                "0": "TFF=0 RFF=0", "1": "TFF=0 RFF=1",
                "2": "TFF=1 RFF=0", "3": "TFF=1 RFF=1",
            }
            for trf_val, count in sorted(bs.trf_distribution.items()):
                name = trf_names.get(trf_val, f"trf={trf_val}")
                pct = count / total * 100
                lines.append(f"    trf={trf_val} ({name:<15}) {count:>8} ({pct:>5.1f}%)")
            lines.append("")

        if bs.segments:
            fps = si.fps if si.fps > 0 else 29.97
            lines.append("  -- Segment Map --")
            for s in bs.segments:
                t_sec = s.start_frame / fps
                t_min = int(t_sec // 60)
                t_s = t_sec % 60
                lines.append(
                    f"    {s.start_frame:>7}-{s.end_frame:>7} "
                    f"[{t_min:02d}:{t_s:04.1f}] {s.seg_type:>5}  "
                    f"({s.cycling_pct:.0f}% film, {s.interlaced_pct:.0f}% intl, "
                    f"{s.frame_count} frames, {s.duration_sec:.1f}s)"
                )
            lines.append("")
    elif bs and bs.error:
        lines.append(f"  Layer 1: ERROR - {bs.error}")
        lines.append("")

    # Layer 2
    px = result.pixel
    if px and not px.error:
        lines.append("  -- Layer 2: Naranjo Detection (chi-square + autocorrelation) --")
        chi_str = "DETECTED" if px.chi_square_detected else "not detected"
        lines.append(f"  Chi-square:     {chi_str}")
        lines.append(f"  Energy ratio:   {px.energy_ratio:.4f}  (>1 = telecine)")
        lines.append(f"  Std/Robust s:   {px.std_ratio:.4f}  (>1.3 suggests periodic)")
        lines.append(f"  x[n] mean:      {px.xn_mean:.4f}")
        lines.append(f"  x[n] std:       {px.xn_std:.4f}")
        lines.append(f"  x[n] median:    {px.xn_median:.4f}")
        lines.append("")

        lines.append(f"  Combed frames:  {px.combed_frames:,} ({px.combed_pct:.1f}%)"
                      f" [threshold={COMBING_RATIO_THRESHOLD}]")
        lines.append(f"  Median ratio:   {px.median_ratio:.4f}")
        lines.append(f"  Time:           {px.elapsed_sec:.1f}s ({px.frames_per_sec:.0f} f/s)")
        lines.append("")

        # Autocorrelation table
        xn_ac = px.xn_autocorrelation
        comb_ac = px.comb_autocorrelation
        if xn_ac or comb_ac:
            lines.append("  -- Autocorrelation (lags 1-10) --")
            lines.append(f"  {'Lag':>5}   {'x[n] field diff':>15}   {'combing ratio':>15}")
            lines.append(f"  {'---':>5}   {'---------------':>15}   {'---------------':>15}")
            for lag in range(1, 11):
                xn_val = xn_ac.get(str(lag), 0)
                cb_val = comb_ac.get(str(lag), 0)
                marker = "  < period-5" if lag == 5 else ""
                lines.append(f"  {lag:>5}   {xn_val:>+15.4f}   {cb_val:>+15.4f}{marker}")
            lines.append(f"  {'L5/L1':>5}   {px.xn_lag5_lag1_ratio:>15.3f}"
                          f"   {px.comb_lag5_lag1_ratio:>15.3f}")
        lines.append("")

        # Duplicate field detection
        lines.append("  -- Duplicate Field Detection --")
        lines.append(f"  Dup% (SAD<0.2): {px.dup_field_pct_02:.1f}%")
        lines.append(f"  Dup% (SAD<0.5): {px.dup_field_pct:.1f}%  < primary threshold")
        lines.append(f"  Dup% (SAD<1.0): {px.dup_field_pct_10:.1f}%")
        lines.append(f"  Top median SAD: {px.dup_field_top_median_sad:.4f}")
        lines.append(f"  Bot median SAD: {px.dup_field_bot_median_sad:.4f}")
        lines.append(f"  AC lag1:        {px.dup_field_lag1:+.4f}")
        lines.append(f"  AC lag5:        {px.dup_field_lag5:+.4f}")
        lines.append(f"  AC lag5/lag1:   {px.dup_field_lag5_lag1_ratio:.3f}")
        if px.dup_field_has_period5:
            lines.append(f"  Period-5:       DETECTED (telecine 3:2)")
        else:
            lines.append(f"  Period-5:       not detected")
        if px.dup_field_pct >= DUP_FIELD_TELECINE_PCT:
            lines.append(f"  Verdict:        TELECINE (>={DUP_FIELD_TELECINE_PCT}% dup fields)")
        elif px.dup_field_pct <= DUP_FIELD_INTERLACED_PCT:
            lines.append(f"  Verdict:        TRUE INTERLACED (<={DUP_FIELD_INTERLACED_PCT}% dup)")
        else:
            lines.append(f"  Verdict:        GRAY ZONE ({DUP_FIELD_INTERLACED_PCT}-{DUP_FIELD_TELECINE_PCT}%)")
        lines.append("")
    elif px and px.error:
        lines.append(f"  Layer 2: ERROR - {px.error}")
        lines.append("")

    # Layer 3
    fs = result.field_swap
    if fs and not fs.error:
        if fs.insufficient_data:
            lines.append("  -- Layer 3: Field-Swap Validation --")
            lines.append(f"  Insufficient data: only {fs.eligible_combed}"
                          f" combed frames (need {MIN_COMBED_FOR_FIELDSWAP}+)")
            lines.append("")
        else:
            lines.append("  -- Layer 3: Field-Swap Validation (physical proof) --")
            lines.append(f"  Total combed:   {fs.total_combed:,}")
            lines.append(f"  Degenerate:     {fs.degenerate_skipped:,} (skipped)")
            lines.append(f"  Eligible:       {fs.eligible_combed:,}")
            lines.append(f"  Tested:         {fs.tested:,}")
            lines.append(f"  Fixable:        {fs.fixed_count:,} ({fs.fix_pct:.1f}%)")
            lines.append(f"  Unfixable:      {fs.unfixable_count:,} ({100 - fs.fix_pct:.1f}%)")
            if fs.fix_pct >= FIELDSWAP_TELECINE_PCT:
                lines.append(f"  Verdict:        TELECINE (>={FIELDSWAP_TELECINE_PCT}% fixable)")
            elif fs.fix_pct < FIELDSWAP_INTERLACED_PCT:
                lines.append(f"  Verdict:        INTERLACED (<{FIELDSWAP_INTERLACED_PCT}% fixable)")
            else:
                lines.append(f"  Verdict:        MIXED ({FIELDSWAP_INTERLACED_PCT}-{FIELDSWAP_TELECINE_PCT}%)")
            lines.append(f"  Time:           {fs.elapsed_sec:.1f}s")
            lines.append("")
    elif fs and fs.error:
        lines.append(f"  Layer 3: ERROR - {fs.error}")
        lines.append("")

    # Classification
    lines.append("  == CLASSIFICATION ==")
    lines.append(f"  Result:         {cls.classification}")
    lines.append(f"  Confidence:     {cls.confidence}")
    lines.append(f"  Reason:         {cls.reason}")
    if cls.film_source:
        lines.append(f"  Film source:    {cls.film_source}"
                      f" ({cls.film_pct:.1f}% of frames, {cls.film_frames:,} frames)")
    if cls.video_source:
        lines.append(f"  Video source:   {cls.video_source}"
                      f" ({cls.video_pct:.1f}% of frames, {cls.video_frames:,} frames)")
    layers_str = (f"{'1' if result.layer1_ran else '-'}"
                  f"{'2' if result.layer2_ran else '-'}"
                  f"{'3' if result.layer3_ran else '-'}")
    lines.append(f"  Layers run:     {layers_str}")
    lines.append(f"  Total time:     {result.total_elapsed_sec:.1f}s")
    lines.append("=" * W)

    return "\n".join(lines)
