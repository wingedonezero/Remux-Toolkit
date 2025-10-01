# remux_toolkit/tools/video_ab_comparator/detectors/base_detector.py

from abc import ABC, abstractmethod
from ..core.models import SourceInfo
from typing import List
import numpy as np

class BaseDetector(ABC):
    """Abstract base class for all issue detectors."""

    @property
    @abstractmethod
    def issue_name(self) -> str:
        """The name of the issue this detector looks for (e.g., 'Blocking')."""
        pass

    @abstractmethod
    def run(self, source: 'VideoSource', frame_list: List[np.ndarray]) -> dict:
        """
        Runs the detection logic on a given video source using a list of frames.

        Returns: A dictionary with 'score' and 'summary'.
        """
        pass
