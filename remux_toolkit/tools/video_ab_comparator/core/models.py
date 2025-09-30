# remux_toolkit/tools/video_ab_comparator/core/models.py

from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any

@dataclass
class StreamInfo:
    """Holds metadata for a single stream."""
    index: int
    codec_type: str
    codec_name: str
    resolution: Optional[str] = None
    dar: Optional[str] = None
    colorspace: Optional[str] = None
    frame_rate: Optional[str] = None
    fps: float = 0.0  # NEW: For calculations
    frame_count: int = 0  # NEW: For looping
    bitrate: Optional[str] = None

@dataclass
class SourceInfo:
    """Holds all probed metadata for a source file."""
    path: str
    format_name: str
    duration: float
    bitrate: str
    streams: List[StreamInfo] = field(default_factory=list)
    video_stream: Optional[StreamInfo] = None # NEW: Quick access to main video stream

@dataclass
class DetectedIssue:
    """Holds the result from a single detector for one source."""
    issue_name: str
    score: float  # 0-100, lower is better
    summary: str
    worst_frame_timestamp: Optional[float] = None

@dataclass
class ComparisonResult:
    """Top-level object containing the full A/B comparison results."""
    source_a: SourceInfo
    source_b: SourceInfo
    alignment_offset_secs: float = 0.0
    verdict: str = "Analysis not yet complete."
    issues: Dict[str, Dict[str, Any]] = field(default_factory=dict)
