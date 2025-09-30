# remux_toolkit/tools/video_ab_comparator/core/alignment.py

import imagehash
import numpy as np
from typing import List

def align_sources(fingerprints_a: List, fingerprints_b: List) -> int:
    """
    Finds the best frame offset of sequence B relative to sequence A.
    A positive offset means B starts later than A.
    """
    if not fingerprints_a or not fingerprints_b:
        return 0

    a = np.array([int(str(h), 16) for h in fingerprints_a])
    b = np.array([int(str(h), 16) for h in fingerprints_b])

    best_offset = 0
    min_diff = float('inf')

    # Search for the best alignment in a reasonable window
    for offset in range(-len(a) // 2, len(a) // 2):
        if offset < 0:
            shifted_a = a[:len(a) + offset]
            shifted_b = b[-offset:]
        else:
            shifted_a = a[offset:]
            shifted_b = b[:len(b) - offset]

        min_len = min(len(shifted_a), len(shifted_b))
        if min_len == 0:
            continue

        diff = np.sum(np.abs(shifted_a[:min_len] - shifted_b[:min_len]))

        if diff < min_diff:
            min_diff = diff
            best_offset = offset

    return best_offset
