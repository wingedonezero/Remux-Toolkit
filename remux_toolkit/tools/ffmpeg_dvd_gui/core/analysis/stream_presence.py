"""
IFO-declared vs VOB-present stream filter.

MakeMKV's closed binary (``FUN_00718010`` per Ghidra, see
``research/ghidra_output/unmapped_stream_setup.md``) takes the audio + sub
streams declared by the IFO and removes any whose stream IDs aren't
actually observed in the VOB's PS packs. Without this filter we emit
"phantom" tracks — IFO-declared streams that aren't physically present —
which then show up as empty / silent MKV tracks.

Approach here mirrors MakeMKV's semantics rather than the exact byte
layout:

    1. Walk up to ``max_sectors`` sectors from the title's VOBs.
    2. Collect the (stream_id, substream_id) of every non-NAV PES.
    3. Map each IFO-declared audio / subpicture stream to the
       (stream_id, substream_id) we'd expect for its slot + codec.
    4. Return which declared slots are *missing* from the observed set.

The expected-key mapping uses the spec-canonical substream base (AC3 ->
0x80, DTS -> 0x88, LPCM -> 0xA0, MPEG audio -> stream_id 0xC0+slot,
subpicture -> 0x20+slot). This matches our orchestrator's
``_AUDIO_FORMAT_TABLE``.

Caller decides what to do with a non-empty ``missing_audio_indices``:

    * Inspector: surface for diagnosis.
    * Rip pipeline: filter the per-slot stream plans before opening
      tracks. Off by default (gated on ``--filter-phantoms``) until
      cross-validation against MakeMKV confirms the filter's accuracy.

The scan is read-only: no IFO state is mutated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..demux.cell_reader import CellReader
from ..demux.ps_walker import (
    STREAM_MPEG_VIDEO, STREAM_PRIVATE_1, iter_es_payloads,
)


# Map ``audio_attr_t.audio_format`` (libdvdread) to the substream_id base
# used in the VOB. ``0xC0`` is special — MPEG audio uses stream_id 0xC0+slot
# directly, not private_stream_1 + substream.
_AUDIO_BASE = {
    0: 0x80,   # AC-3
    2: 0xC0,   # MPEG-1 audio (uses stream_id 0xC0+slot, substream_id=None)
    3: 0xC0,   # MPEG-2 ext (same as above)
    4: 0xA0,   # LPCM
    6: 0x88,   # DTS
    # 1, 5, 7 are reserved.
}

_SUBP_BASE = 0x20

#: Default sector budget for the scan. 16384 sectors = 32 MiB ≈ 30s of
#: 8.5 Mbps DVD-Video. Subtitle streams on long titles (TV-show discs
#: with sparse signage / commentary tracks) often don't surface their
#: first PES until 20-30s in. Smaller budgets gave false phantom flags
#: on ANGEL_S1D1's main features (44 min titles). The scan exits early
#: once all expected keys have been observed, so healthy commercial
#: discs still finish in <2 MB of reads.
DEFAULT_SCAN_SECTORS = 16384


@dataclass(slots=True)
class StreamPresenceReport:
    """Result of a stream-presence scan for one PGC."""
    vts_no: int
    pgc_no: int
    declared_audio_count: int
    declared_subp_count: int
    sectors_scanned: int
    observed_keys: set = field(default_factory=set)
    expected_audio_keys: List[Optional[Tuple[int, Optional[int]]]] = field(default_factory=list)
    expected_subp_keys: List[Optional[Tuple[int, Optional[int]]]] = field(default_factory=list)
    #: 0-based declared-audio slot indices whose expected key was NOT seen.
    missing_audio_indices: List[int] = field(default_factory=list)
    #: 0-based declared-sub slot indices whose expected key was NOT seen.
    missing_subp_indices: List[int] = field(default_factory=list)
    #: True when at least one MPEG-2 video PES was observed.
    video_present: bool = False

    @property
    def has_phantom_streams(self) -> bool:
        return bool(self.missing_audio_indices or self.missing_subp_indices)


def expected_audio_key(slot: int, audio_format: int) -> Optional[Tuple[int, Optional[int]]]:
    """Return the (stream_id, substream_id) the IFO declares for the given
    audio slot + codec. None when the codec is reserved/unknown."""
    base = _AUDIO_BASE.get(int(audio_format))
    if base is None:
        return None
    if base == 0xC0:
        # MPEG audio: stream_id encodes the slot directly.
        return (0xC0 + slot, None)
    return (STREAM_PRIVATE_1, base + slot)


def expected_subp_key(slot: int) -> Tuple[int, Optional[int]]:
    return (STREAM_PRIVATE_1, _SUBP_BASE + slot)


def _expected_active_audio(audio_attrs, audio_control) -> List[Tuple[int, Optional[Tuple[int, Optional[int]]]]]:
    """For each PGC-active audio slot, return (slot_index, expected_key_or_None).
    Slots that are inactive per PGC.audio_control are skipped entirely."""
    out: List[Tuple[int, Optional[Tuple[int, Optional[int]]]]] = []
    n_slots = min(len(audio_attrs), 8)
    for slot in range(n_slots):
        if not (audio_control[slot] & 0x8000):
            continue
        out.append((slot, expected_audio_key(slot, int(audio_attrs[slot].audio_format))))
    return out


def _expected_active_subp(subp_attrs, subp_control) -> List[Tuple[int, Tuple[int, Optional[int]]]]:
    out: List[Tuple[int, Tuple[int, Optional[int]]]] = []
    n_slots = min(len(subp_attrs), 32)
    for slot in range(n_slots):
        if not (subp_control[slot] & 0x80000000):
            continue
        out.append((slot, expected_subp_key(slot)))
    return out


def scan_observed_streams(disc, vts_no: int, pgc_no: int, *,
                          max_sectors: int = DEFAULT_SCAN_SECTORS,
                          stop_when_all_present: Optional[set] = None,
                          ) -> Tuple[set, int]:
    """Read sectors from the title VOBs and return the set of observed
    ``(stream_id, substream_id)`` tuples plus the number of sectors actually
    read.

    ``stop_when_all_present``: if given, the scan exits early once every
    key in this set has been observed. Pass the union of expected keys to
    minimise IO on healthy discs.
    """
    observed: set = set()
    sectors_read = 0
    target_remaining = set(stop_when_all_present) if stop_when_all_present else None

    # CellReader needs *some* title_num parameter; vts_no + pgc_no together
    # are enough to identify the PGC. Use 0 — the constructor skips the
    # title-resolution path when both are provided.
    with CellReader(disc, title_num=0,
                    vts_no=vts_no, pgc_no=pgc_no) as cr:
        sector_count = [0]

        def sector_stream():
            for cell, sect in cr.iter_sectors():
                sector_count[0] += 1
                yield cell, sect
                if sector_count[0] >= max_sectors:
                    return

        for payload in iter_es_payloads(sector_stream()):
            if payload.is_nav:
                continue
            key = (payload.stream_id, payload.substream_id)
            observed.add(key)
            if target_remaining is not None and key in target_remaining:
                target_remaining.discard(key)
                if not target_remaining:
                    break

        sectors_read = sector_count[0]
    return observed, sectors_read


def detect_phantom_streams(disc, vts_no: int, pgc_no: int, *,
                           max_sectors: int = DEFAULT_SCAN_SECTORS,
                           ) -> StreamPresenceReport:
    """Open the VTS IFO, enumerate the PGC's active audio + sub slots,
    scan the title VOBs, and report which declared streams are missing
    from the observed set.
    """
    from ...bindings import libdvdread as dr

    with dr.open_ifo(disc, vts_no) as vts:
        m = vts.contents.vtsi_mat.contents
        pgcit = vts.contents.vts_pgcit.contents
        if pgc_no < 1 or pgc_no > pgcit.nr_of_pgci_srp:
            raise ValueError(
                f"pgc_no {pgc_no} out of range (1..{pgcit.nr_of_pgci_srp})")
        pgc = pgcit.pgci_srp[pgc_no - 1].pgc.contents

        audio_active = _expected_active_audio(
            m.vts_audio_attr, pgc.audio_control)
        subp_active = _expected_active_subp(
            m.vts_subp_attr, pgc.subp_control)
        declared_audio_count = int(m.nr_of_vts_audio_streams)
        declared_subp_count = int(m.nr_of_vts_subp_streams)

    expected_keys = {k for _, k in audio_active if k is not None}
    expected_keys.update(k for _, k in subp_active)
    # Always include video so we can early-exit even on disc with no
    # audio/sub.
    expected_keys.add((STREAM_MPEG_VIDEO, None))

    observed, sectors_read = scan_observed_streams(
        disc, vts_no, pgc_no,
        max_sectors=max_sectors,
        stop_when_all_present=expected_keys,
    )

    # Build per-slot expected lists (None for reserved/unmappable codecs).
    expected_audio_keys: List[Optional[Tuple[int, Optional[int]]]] = []
    missing_audio_indices: List[int] = []
    for slot, key in audio_active:
        expected_audio_keys.append(key)
        if key is None:
            # Reserved codec — can't assert presence either way.
            continue
        if key not in observed:
            missing_audio_indices.append(slot)

    expected_subp_keys: List[Optional[Tuple[int, Optional[int]]]] = []
    missing_subp_indices: List[int] = []
    for slot, key in subp_active:
        expected_subp_keys.append(key)
        if key not in observed:
            missing_subp_indices.append(slot)

    return StreamPresenceReport(
        vts_no=vts_no,
        pgc_no=pgc_no,
        declared_audio_count=declared_audio_count,
        declared_subp_count=declared_subp_count,
        sectors_scanned=sectors_read,
        observed_keys=observed,
        expected_audio_keys=expected_audio_keys,
        expected_subp_keys=expected_subp_keys,
        missing_audio_indices=missing_audio_indices,
        missing_subp_indices=missing_subp_indices,
        video_present=((STREAM_MPEG_VIDEO, None) in observed),
    )


def report_to_dict(r: StreamPresenceReport) -> dict:
    return {
        "vts_no": r.vts_no,
        "pgc_no": r.pgc_no,
        "declared_audio_count": r.declared_audio_count,
        "declared_subp_count": r.declared_subp_count,
        "sectors_scanned": r.sectors_scanned,
        "observed_keys": sorted(
            [(int(s), None if sub is None else int(sub))
             for s, sub in r.observed_keys],
            key=lambda t: (t[0], t[1] if t[1] is not None else -1),
        ),
        "missing_audio_indices": r.missing_audio_indices,
        "missing_subp_indices": r.missing_subp_indices,
        "video_present": r.video_present,
        "has_phantom_streams": r.has_phantom_streams,
    }


__all__ = [
    "DEFAULT_SCAN_SECTORS",
    "StreamPresenceReport",
    "detect_phantom_streams",
    "expected_audio_key",
    "expected_subp_key",
    "report_to_dict",
    "scan_observed_streams",
]
