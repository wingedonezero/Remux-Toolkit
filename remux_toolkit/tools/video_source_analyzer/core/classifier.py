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

    Key insight: cadence-5 is the FINGERPRINT of NTSC 3:2 telecine.
    Combing % varies with motion content but cadence is structural.

    Decision priority:
    1. Cadence-5 strong → hard telecine (regardless of combing %)
    2. High combing + no cadence → interlaced
    3. Low combing → progressive
    4. Otherwise → mixed/uncertain
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

    # ── Hard telecine: cadence-5 IS the signature ─────────────────────
    # Cadence is structural — if it's there, it's telecine regardless of
    # combing %. Combing % varies with motion content.

    # Strong cadence: definitive hard telecine
    if cadence5 >= 30.0:
        return result_fn("hard_telecine", "high",
                         f"strong period-5 cadence ({detail})",
                         "progressive")

    # Good cadence with reasonable combing: hard telecine
    if cadence5 >= 20.0 and combed_pct >= 15.0:
        return result_fn("hard_telecine", "high",
                         f"period-5 cadence detected ({detail})",
                         "progressive")

    # Moderate cadence with combing: likely telecine (weak signal)
    if cadence5 >= 15.0 and combed_pct >= 15.0:
        return result_fn("hard_telecine", "medium",
                         f"moderate period-5 cadence ({detail})",
                         "progressive")

    # Field-swap can confirm telecine in cadence-weak cases
    if fix_pct >= 70 and combed_pct >= 15.0:
        return result_fn("hard_telecine", "medium",
                         f"field-swap supports telecine ({detail})",
                         "progressive")

    # ── Weak telecine: high combing + some cadence ────────────────────
    # Catches cases like Aozora R2J ep1 (30% combed, 13% cad5)
    if combed_pct >= 25.0 and 12.0 <= cadence5 < 15.0:
        return result_fn("hard_telecine", "low",
                         f"weak period-5 cadence + combing ({detail})",
                         "progressive")

    # ── Interlaced: lots of combing, no telecine cadence ──────────────
    # True interlaced has combing on most motion frames, no period-5
    if combed_pct >= 35.0 and cadence5 < 15.0:
        return result_fn("interlaced", "high",
                         f"high combing, no telecine pattern ({detail})",
                         "", "interlaced")

    if combed_pct >= 25.0 and cadence5 < 12.0:
        return result_fn("interlaced", "medium",
                         f"moderate combing, no cadence ({detail})",
                         "", "interlaced")

    # ── Progressive: very low combing ─────────────────────────────────
    if combed_pct < 10.0 and cadence5 < 10.0:
        return result_fn("progressive", "high",
                         f"low combing, no cadence ({detail})")

    # Some compression noise but still low
    if combed_pct < 15.0:
        return result_fn("progressive", "medium",
                         f"low combing ({detail})")

    # ── Borderline: moderate combing + weak cadence ───────────────────
    # Likely interlaced with low motion content
    if 15.0 <= combed_pct < 25.0 and cadence5 < 15.0:
        return result_fn("interlaced", "low",
                         f"moderate combing, weak cadence ({detail})",
                         "", "interlaced")

    return result_fn("mixed", "low",
                     f"unclear ({detail})",
                     "", "interlaced")
