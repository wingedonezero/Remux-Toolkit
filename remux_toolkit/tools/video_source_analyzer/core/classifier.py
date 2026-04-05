"""Classification logic combining all layer results."""

from __future__ import annotations

from typing import Optional

from .models import (
    ClassificationResult, StreamInfo, BitstreamResult, PixelResult,
    FieldSwapResult,
    FILM_PCT_HIGH, FILM_PCT_MED, FILM_PCT_LOW,
    DUP_FIELD_TELECINE_PCT, DUP_FIELD_INTERLACED_PCT,
    FIELDSWAP_TELECINE_PCT, FIELDSWAP_INTERLACED_PCT,
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
    6. Interlaced → use Layer 2 duplicate fields + Layer 3 field-swap
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

    # ── Interlaced → Layer 2 + Layer 3 ─────────────────────────────────
    if intl_pct > 80 and film_pct < 5 and px is not None:
        chi_detected = px.chi_square_detected
        energy_ratio = px.energy_ratio
        comb_l5l1 = px.comb_lag5_lag1_ratio
        has_var = px.has_variance

        # Duplicate field metrics (primary discriminator)
        dup_pct = px.dup_field_pct
        dup_l5l1 = px.dup_field_lag5_lag1_ratio
        dup_period5 = px.dup_field_has_period5

        # Layer 3 metrics
        fix_pct = fs.fix_pct if fs else -1.0
        insufficient = fs.insufficient_data if fs else True
        has_fieldswap = fix_pct >= 0 and not insufficient

        detail = (f"dup={dup_pct:.1f}% L5/L1={dup_l5l1:.2f}, "
                  f"comb_L5/L1={comb_l5l1:.2f}")
        if has_fieldswap:
            detail += f", fix={fix_pct:.1f}%"

        # 1. No variance → interlaced
        if not has_var:
            return result("interlaced", "high",
                          f"no signal variance ({detail})",
                          "", "interlaced")

        # 2. High dup fields + period-5 → hard telecine (definitive)
        if dup_pct >= DUP_FIELD_TELECINE_PCT and dup_period5:
            return result("hard_telecine", "high",
                          f"duplicate fields with period-5 ({detail})",
                          "progressive")

        # 3. High dup fields, no clear period-5
        if dup_pct >= DUP_FIELD_TELECINE_PCT:
            if has_fieldswap and fix_pct >= FIELDSWAP_TELECINE_PCT:
                return result("hard_telecine", "high",
                              f"high dup fields + field-swap ({detail})",
                              "progressive")
            return result("hard_telecine", "medium",
                          f"high dup fields, no period-5 ({detail})",
                          "progressive")

        # 4. No duplicate fields → true interlaced
        if dup_pct <= DUP_FIELD_INTERLACED_PCT:
            return result("interlaced", "high",
                          f"no duplicate fields ({detail})",
                          "", "interlaced")

        # ── Gray zone (3-15% dup fields) ───────────────────────────
        # 5. Field-swap confirms telecine
        if has_fieldswap and fix_pct >= FIELDSWAP_TELECINE_PCT:
            return result("hard_telecine", "high",
                          f"field-swap confirmed + moderate dup ({detail})",
                          "progressive")

        # 6. Field-swap negative → interlaced
        if has_fieldswap and fix_pct < FIELDSWAP_INTERLACED_PCT:
            return result("interlaced", "high",
                          f"field-swap negative + low dup ({detail})",
                          "", "interlaced")

        # 7. Strong autocorrelation signals
        if comb_l5l1 > 2.0 and dup_pct > 5:
            return result("hard_telecine", "medium",
                          f"period-5 combing + dup fields ({detail})",
                          "progressive")

        # 8. Moderate mixed signals
        if has_fieldswap and fix_pct > 50:
            return result("hard_telecine", "medium",
                          f"moderate field-swap + dup ({detail})",
                          "progressive")

        # 9. Fallback → interlaced
        return result("interlaced", "medium",
                      f"uncertain gray zone ({detail})",
                      "", "interlaced")

    # ── Interlaced without Layer 2 ─────────────────────────────────────
    if intl_pct > 80 and film_pct < 5:
        return result("interlaced", "low",
                      f"{intl_pct:.1f}% interlaced, no pixel analysis",
                      "", "interlaced")

    # ── Mixed / fallback ────────────────────────────────────────��──────
    if has_film_seg and has_video_seg:
        return result("mixed", "low",
                      f"mixed segments: film={film_pct:.1f}%, intl={intl_pct:.1f}%",
                      "progressive", "interlaced")

    return result("unknown", "low",
                  f"unclassified: film={film_pct:.1f}%, "
                  f"prog={prog_pct:.1f}%, intl={intl_pct:.1f}%")
