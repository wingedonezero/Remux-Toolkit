"""Classification logic combining all layer results."""

from __future__ import annotations

from typing import Optional

from .models import (
    ClassificationResult, StreamInfo, BitstreamResult, PixelResult,
    FieldSwapResult,
    FILM_PCT_HIGH, FILM_PCT_MED, FILM_PCT_LOW,
    TELECINE_COMBED_MIN, TELECINE_COMBED_MAX, TELECINE_CADENCE5_MIN,
    INTERLACED_COMBED_MIN, PROGRESSIVE_COMBED_MAX,
)


def classify(
    stream_info: StreamInfo,
    bitstream: Optional[BitstreamResult],
    pixel: Optional[PixelResult] = None,
    field_swap: Optional[FieldSwapResult] = None,
) -> ClassificationResult:
    """
    Classify content type from combined layer results.

    Decision tree:
    1. Non-MPEG-2 shortcuts (container flags only)
    2. Pulldown flag + high Film% → soft telecine
    3. VFR + high Film% → soft telecine
    4. Film% alone (CFR with cycling) → soft telecine
    5. Pure progressive (>95% progressive frames)
    6. Interlaced → use Layer 2 Laplacian combing + cadence-5
    7. Mixed / fallback
    """
    bs = bitstream or BitstreamResult()
    px = pixel
    fs = field_swap

    film_pct = bs.film_pct
    prog_pct = bs.progressive_pct
    intl_pct = bs.interlaced_pct
    total = bs.coded_frames or 1

    segments = bs.segments
    has_video_seg = any(s.seg_type == "VIDEO" for s in segments)
    has_film_seg = any(s.seg_type == "FILM" for s in segments)

    film_frames = sum(s.frame_count for s in segments if s.seg_type == "FILM")
    video_frames = sum(s.frame_count for s in segments if s.seg_type == "VIDEO")
    mixed_frames = sum(s.frame_count for s in segments if s.seg_type == "MIXED")

    def result(cls, conf, reason, film_src="", video_src=""):
        return ClassificationResult(
            classification=cls,
            confidence=conf,
            reason=reason,
            film_source=film_src,
            video_source=video_src,
            film_pct=round(film_pct, 2),
            video_pct=round(100 - film_pct, 2),
            film_frames=film_frames,
            video_frames=video_frames,
            mixed_frames=mixed_frames,
        )

    # ── Non-MPEG-2 shortcut ────────────────────────────────────────────
    if not stream_info.is_mpeg2:
        # For non-MPEG-2, use Layer 2 if available
        if px is not None and not px.error:
            return _classify_from_pixel(px, fs, result)
        if stream_info.scan_type.lower() == "progressive":
            return result("progressive", "high",
                          "non-MPEG-2 codec, container says progressive")
        elif stream_info.is_interlaced:
            return result("interlaced", "medium",
                          "non-MPEG-2 codec, container says interlaced")
        return result("unknown", "low", "non-MPEG-2 codec, unknown scan type")

    # ── Pulldown flag ──────────────────────────────────────────────────
    if stream_info.has_pulldown:
        if film_pct > FILM_PCT_HIGH:
            if has_video_seg and intl_pct > 5:
                return result("soft_telecine_mixed", "high",
                              f"pulldown flag + {film_pct:.1f}% film + video segments",
                              "progressive", "interlaced")
            return result("soft_telecine", "high",
                          f"pulldown flag + {film_pct:.1f}% film",
                          "progressive")
        elif film_pct > FILM_PCT_MED:
            return result("soft_telecine_mixed", "high",
                          f"pulldown flag + {film_pct:.1f}% film (mixed)",
                          "progressive", "interlaced")

    # ── VFR container signal ───────────────────────────────────────────
    if stream_info.is_vfr:
        if film_pct > FILM_PCT_HIGH:
            if has_video_seg and intl_pct > 5:
                return result("soft_telecine_mixed", "high",
                              f"VFR + {film_pct:.1f}% film + video segments",
                              "progressive", "interlaced")
            return result("soft_telecine", "high",
                          f"VFR + {film_pct:.1f}% film",
                          "progressive")
        elif film_pct > FILM_PCT_MED:
            return result("soft_telecine_mixed", "medium",
                          f"VFR + {film_pct:.1f}% film",
                          "progressive", "interlaced")

    # ── Film% from bitstream ───────────────────────────────────────────
    if film_pct > FILM_PCT_HIGH:
        if has_video_seg and intl_pct > 5:
            return result("soft_telecine_mixed", "high",
                          f"CFR but {film_pct:.1f}% film cycling + video segments",
                          "progressive", "interlaced")
        return result("soft_telecine", "high",
                      f"CFR but {film_pct:.1f}% film cycling in bitstream",
                      "progressive")
    elif film_pct > FILM_PCT_MED:
        return result("soft_telecine_mixed", "medium",
                      f"{film_pct:.1f}% film cycling",
                      "progressive", "interlaced")
    elif film_pct > FILM_PCT_LOW:
        return result("soft_telecine_mixed", "low",
                      f"{film_pct:.1f}% film cycling (low)",
                      "progressive", "interlaced")

    # ── Pure progressive ───────────────────────────────────────────────
    if prog_pct > 95 and film_pct < 5:
        return result("progressive", "high",
                      f"{prog_pct:.1f}% progressive frames, {film_pct:.1f}% film")

    # ── Interlaced / hard telecine → Layer 2 ──────────────────────────
    if intl_pct > 80 and film_pct < 5 and px is not None and not px.error:
        return _classify_from_pixel(px, fs, result)

    # ── Interlaced without Layer 2 ────────────────────────────────────
    if intl_pct > 80 and film_pct < 5:
        return result("interlaced", "low",
                      f"{intl_pct:.1f}% interlaced, no pixel analysis",
                      "", "interlaced")

    # ── Mixed / fallback ──────────────────────────────────────────────
    if has_film_seg and has_video_seg:
        return result("mixed", "low",
                      f"mixed segments: film={film_pct:.1f}%, intl={intl_pct:.1f}%",
                      "progressive", "interlaced")

    return result("unknown", "low",
                  f"unclassified: film={film_pct:.1f}%, "
                  f"prog={prog_pct:.1f}%, intl={intl_pct:.1f}%")


def _classify_from_pixel(
    px: PixelResult,
    fs: Optional[FieldSwapResult],
    result_fn,
) -> ClassificationResult:
    """
    Classify based on Layer 2 Laplacian combing + cadence-5 analysis.

    Key metrics:
    - combed_pct: % of frames with block-level combing
    - cadence5_pct: how strongly combed frames follow period-5 (3:2 pulldown)

    Decision matrix:
    - combed ~30-40% + cadence5 >15% → hard telecine
    - combed >35% + cadence5 <15%    → interlaced
    - combed <15%                    → progressive
    """
    combed_pct = px.combed_pct
    cadence5 = px.cadence5_pct
    telecine_gap = px.telecine_gap_pct

    detail = (f"combed={combed_pct:.1f}%, cadence5={cadence5:.1f}%, "
              f"tc_gaps={telecine_gap:.1f}%")

    # Layer 3 field-swap if available
    fix_pct = -1.0
    if fs and not fs.insufficient_data and fs.fix_pct >= 0:
        fix_pct = fs.fix_pct
        detail += f", fix={fix_pct:.1f}%"

    # ── Progressive: very low combing ─────────────────────────────────
    if combed_pct < PROGRESSIVE_COMBED_MAX:
        return result_fn("progressive", "high",
                         f"low combing ({detail})")

    # ── Hard telecine: moderate combing with period-5 cadence ─────────
    if combed_pct >= TELECINE_COMBED_MIN and cadence5 >= TELECINE_CADENCE5_MIN:
        # Strong telecine signal
        if combed_pct <= TELECINE_COMBED_MAX and cadence5 >= 30:
            return result_fn("hard_telecine", "high",
                             f"telecine cadence detected ({detail})",
                             "progressive")
        # Good cadence but combing % is high (might have interlaced segments)
        if combed_pct > TELECINE_COMBED_MAX:
            return result_fn("hard_telecine", "medium",
                             f"telecine cadence + high combing ({detail})",
                             "progressive")
        # Moderate cadence
        return result_fn("hard_telecine", "medium",
                         f"moderate telecine cadence ({detail})",
                         "progressive")

    # ── Reinforce with field-swap if available ────────────────────────
    if fix_pct >= 80 and combed_pct >= TELECINE_COMBED_MIN:
        return result_fn("hard_telecine", "high",
                         f"field-swap confirmed telecine ({detail})",
                         "progressive")

    # ── Interlaced: high combing, no telecine cadence ─────────────────
    if combed_pct >= INTERLACED_COMBED_MIN and cadence5 < TELECINE_CADENCE5_MIN:
        return result_fn("interlaced", "high",
                         f"high combing, no telecine pattern ({detail})",
                         "", "interlaced")

    # ── Gray zone ─────────────────────────────────────────────────────
    # Moderate combing but weak cadence — could be mixed or noisy telecine
    if combed_pct >= TELECINE_COMBED_MIN:
        if fix_pct >= 50:
            return result_fn("hard_telecine", "medium",
                             f"field-swap supports telecine ({detail})",
                             "progressive")
        if cadence5 >= 10:
            return result_fn("hard_telecine", "low",
                             f"weak telecine signal ({detail})",
                             "progressive")
        return result_fn("mixed", "low",
                         f"ambiguous: moderate combing, weak cadence ({detail})",
                         "", "interlaced")

    # ── Low-moderate combing, not clearly anything ────────────────────
    return result_fn("mixed", "low",
                     f"unclear ({detail})")
