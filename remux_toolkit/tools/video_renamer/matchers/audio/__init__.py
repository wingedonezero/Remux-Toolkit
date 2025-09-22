# ===========================================
# matchers/audio/__init__.py
# ===========================================
"""Audio matching algorithms"""

from .correlation import CorrelationMatcher
from .chromaprint import ChromaprintMatcher
from .mfcc import MFCCMatcher
from .panako import PanakoMatcher

__all__ = ['CorrelationMatcher', 'ChromaprintMatcher', 'MFCCMatcher', 'PanakoMatcher']
