# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/__init__.py
from .demux import DemuxStep
from .ccextract import CCExtractStep
from .chapters import ChaptersStep
from .finalize import FinalizeStep
from .disc_analysis import DiscAnalysisStep
from .metadata_analysis import MetadataAnalysisStep
from .telecine_detection import TelecineDetectionStep
from .ifo_parser import IfoParserStep

# This makes it easy for the orchestrator to import all available steps
__all__ = [
    "DemuxStep",
    "CCExtractStep",
    "ChaptersStep",
    "FinalizeStep",
    "DiscAnalysisStep",
    "MetadataAnalysisStep",
    "TelecineDetectionStep",
    "IfoParserStep",
]
