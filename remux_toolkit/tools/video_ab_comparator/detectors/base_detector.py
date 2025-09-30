# remux_toolkit/tools/video_ab_comparator/detectors/base_detector.py

from abc import ABC, abstractmethod
from ..core.models import SourceInfo

class BaseDetector(ABC):
    """Abstract base class for all issue detectors."""

    @property
    @abstractmethod
    def issue_name(self) -> str:
        """The name of the issue this detector looks for (e.g., 'Blocking')."""
        pass

    @abstractmethod
    def run(self, source: 'VideoSource') -> dict:
        """
        Runs the detection logic on a given video source.

        Returns: A dictionary with 'score' and 'summary'.
        """
        pass
