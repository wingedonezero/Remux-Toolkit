"""
Audio + subpicture stream-level dedup — Python port of MakeMKV's
FUN_007786d0 (``stream_dedup_classifier``).

Some DVDs declare duplicate streams in their PGC: the same audio track
attached twice, or a sub track that contains identical bitmaps to
another sub. MakeMKV samples bytes from each stream's first N PES
payloads and compares them; streams whose sampled content matches an
earlier stream get flagged and emit MSG:3029 (audio dup) or MSG:3030
(sub dup) — the rip pipeline then skips the duplicate.

Our port mirrors the algorithm shape:
    1. Walk the first N sectors of the title (default 512 = 1 MB).
    2. Build a per-stream sample buffer of the first SAMPLE_LEN bytes
       (after PES/substream-header stripping).
    3. Compare samples pairwise; later streams with identical content
       to an earlier one are flagged as duplicates.

This isn't bit-exact to FUN_007786d0 (we don't replicate MakeMKV's
sorted-container comparison or the 240-byte record layout), but the
SEMANTICS — "two streams with the same early bytes are duplicates" —
match. Validates against MakeMKV's MSG:3029/3030 once cross-validation
runs on the corpus.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from ..demux.cell_reader import CellReader
from ..demux.ps_walker import iter_es_payloads, stream_key


_logger = logging.getLogger(__name__)


# DVD audio stream-id range (private_stream_1 substream_id 0x40-0xBF in
# MakeMKV's coordinates; libdvdread / our ps_walker normalises to
# substream_id 0x80-0x87 (AC3), 0x88-0x8F (DTS), 0xA0-0xA7 (LPCM), and
# 0xC0+slot for MPEG audio). For dedup purposes we group all audio.
def _is_audio_key(key) -> bool:
    stream_id, sub = key
    # private_stream_1 audio
    if stream_id == 0xBD and sub is not None and 0x80 <= sub <= 0xBF:
        return True
    # MPEG audio
    if 0xC0 <= stream_id <= 0xCF:
        return True
    return False


def _is_subpicture_key(key) -> bool:
    stream_id, sub = key
    return (stream_id == 0xBD and sub is not None and 0x20 <= sub <= 0x3F)


#: Bytes per stream to compare. MakeMKV uses 240-byte records (with ~16
#: bytes of metadata + 220 bytes of payload). We compare 256 payload
#: bytes — enough to distinguish meaningfully different streams without
#: requiring deep disc reads.
SAMPLE_LEN = 256

#: Sectors to scan when collecting per-stream samples. 512 sectors =
#: ~1 MB of disc reads — enough to see the first N bytes of every active
#: stream on a typical DVD title.
SCAN_SECTORS = 512


@dataclass(slots=True)
class StreamDedupReport:
    #: Set of stream keys flagged as duplicates of an earlier stream.
    duplicate_keys: set = field(default_factory=set)
    #: Map from duplicate-key → key-of-the-stream-it-duplicates.
    duplicates_of: Dict = field(default_factory=dict)
    #: Per-stream sample byte counts (for diagnostics).
    sample_sizes: Dict = field(default_factory=dict)


def _compare_samples(a: bytes, b: bytes) -> bool:
    """True if the two sample buffers are content-equal up to the shorter
    one's length. We require a minimum overlap of 64 bytes — fewer and
    the comparison is too unreliable.
    """
    overlap = min(len(a), len(b))
    if overlap < 64:
        return False
    return a[:overlap] == b[:overlap]


def detect_duplicate_streams(
    disc, title_num: int, *,
    vts_no: Optional[int] = None,
    pgc_no: Optional[int] = None,
    candidate_keys: Optional[Iterable] = None,
    sample_len: int = SAMPLE_LEN,
    scan_sectors: int = SCAN_SECTORS,
) -> StreamDedupReport:
    """Scan up to ``scan_sectors`` of the title and identify duplicate
    audio + sub streams.

    ``candidate_keys`` optionally restricts which stream keys are
    considered — the caller may supply this from
    ``_enumerate_streams`` so dedup ignores excluded codecs.

    Returns a ``StreamDedupReport`` describing which keys are dups.
    """
    cand_set = set(candidate_keys) if candidate_keys else None
    samples: Dict[tuple, bytearray] = {}

    with CellReader(disc, title_num,
                    vts_no=vts_no, pgc_no=pgc_no) as cr:
        scanned = 0
        for payload in iter_es_payloads(cr.iter_sectors()):
            if payload.is_nav:
                continue
            scanned += 1
            if scanned > scan_sectors * 8:   # ~8 PES per sector worst-case
                break
            key = stream_key(payload.stream_id, payload.substream_id)
            if cand_set is not None and key not in cand_set:
                continue
            if not (_is_audio_key(key) or _is_subpicture_key(key)):
                continue
            buf = samples.setdefault(key, bytearray())
            if len(buf) >= sample_len:
                continue
            need = sample_len - len(buf)
            buf.extend(payload.es_bytes[:need])
            # Early-exit if every candidate is full.
            if (cand_set is not None
                    and all(len(samples.get(k, b"")) >= sample_len
                            for k in cand_set
                            if (_is_audio_key(k) or _is_subpicture_key(k)))):
                break

    # Pair-up compare: for each stream, check if it matches an earlier
    # one (in some stable order — we sort by key for deterministic output).
    report = StreamDedupReport()
    report.sample_sizes = {k: len(v) for k, v in samples.items()}

    keys_ordered = sorted(samples.keys(), key=lambda k: (k[0], k[1] if k[1] is not None else -1))
    earlier: List[Tuple[tuple, bytes]] = []
    for k in keys_ordered:
        b = bytes(samples[k])
        match = None
        for prev_k, prev_b in earlier:
            # Same kind only — audio dedups against audio, sub against sub.
            same_kind = (
                (_is_audio_key(k) and _is_audio_key(prev_k))
                or (_is_subpicture_key(k) and _is_subpicture_key(prev_k))
            )
            if same_kind and _compare_samples(b, prev_b):
                match = prev_k
                break
        if match is not None:
            report.duplicate_keys.add(k)
            report.duplicates_of[k] = match
        else:
            earlier.append((k, b))

    return report


def filter_duplicate_plans(plans, report: StreamDedupReport):
    """Yield only plans whose key is not in ``report.duplicate_keys``.
    Use to drop dup streams from a ``_StreamPlan`` list before opening
    a rip."""
    for p in plans:
        if p.key not in report.duplicate_keys:
            yield p


__all__ = [
    "SAMPLE_LEN",
    "SCAN_SECTORS",
    "StreamDedupReport",
    "detect_duplicate_streams",
    "filter_duplicate_plans",
]
