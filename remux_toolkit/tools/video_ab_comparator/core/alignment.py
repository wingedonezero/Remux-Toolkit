from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional, Tuple
import numpy as np
import imagehash
import cv2

# ---------- small helpers ----------

def _safe_fraction_to_fps(r_frame_rate: Optional[str]) -> float:
    if not r_frame_rate:
        return 24.0
    s = str(r_frame_rate)
    if "/" in s:
        n, d = s.split("/", 1)
        try:
            n = float(n.strip()); d = float(d.strip())
            return n / d if d != 0 else 24.0
        except Exception:
            pass
    try:
        return float(s)
    except Exception:
        return 24.0

def _hamming(a, b) -> int:
    try:
        return int(a - b)
    except Exception:
        try:
            ha = imagehash.hex_to_hash(str(a)); hb = imagehash.hex_to_hash(str(b))
            return int(ha - hb)
        except Exception:
            return 64

def _to_gray(img_bgr) -> np.ndarray:
    return cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

def _ssim(a: np.ndarray, b: np.ndarray) -> float:
    C1 = (0.01 * 255) ** 2
    C2 = (0.03 * 255) ** 2
    a = a.astype(np.float32); b = b.astype(np.float32)
    mu_a = cv2.GaussianBlur(a, (7, 7), 1.5)
    mu_b = cv2.GaussianBlur(b, (7, 7), 1.5)
    mu_a2 = mu_a * mu_a; mu_b2 = mu_b * mu_b; mu_ab = mu_a * mu_b
    sigma_a2 = cv2.GaussianBlur(a * a, (7, 7), 1.5) - mu_a2
    sigma_b2 = cv2.GaussianBlur(b * b, (7, 7), 1.5) - mu_b2
    sigma_ab = cv2.GaussianBlur(a * b, (7, 7), 1.5) - mu_ab
    num = (2 * mu_ab + C1) * (2 * sigma_ab + C2)
    den = (mu_a2 + mu_b2 + C1) * (sigma_a2 + sigma_b2 + C2)
    return float((num / (den + 1e-9)).mean())

def _sample_anchors(duration_sec: float, count: int) -> List[float]:
    if duration_sec <= 0:
        return []
    return list(np.linspace(duration_sec * 0.1, duration_sec * 0.9, num=count))

# ---------- public API ----------

@dataclass
class AlignResult:
    # B relative to A
    # map ts in A -> ts in B with: ts_b = ts_a - (offset_sec + drift_ppm * ts_a)
    offset_sec: float
    drift_ppm: float     # seconds drift per second (e.g., 0.0001 == 100 ppm)
    confidence: float    # 0..1

# ---------- coarse alignment from hashes ----------

def _coarse_offset_from_hashes(fpa: List, fpb: List) -> int:
    """Return frame offset of B relative to A (positive => B starts later)."""
    if not fpa or not fpb:
        return 0
    n_a = len(fpa); n_b = len(fpb)
    max_shift = max(1, min(n_a, n_b) // 3)   # search window
    best = (float("inf"), 0)
    for shift in range(-max_shift, max_shift + 1):
        if shift < 0:
            a = fpa[:(n_a + shift)]; b = fpb[-shift:]
        else:
            a = fpa[shift:];         b = fpb[:(n_b - shift)]
        if not a or not b:
            continue
        m = min(len(a), len(b))
        if m == 0:
            continue
        mean_dist = sum(_hamming(a[i], b[i]) for i in range(m)) / m
        if mean_dist < best[0]:
            best = (mean_dist, shift)
    return int(best[1])

# ---------- robust align (hash -> SSIM refine -> drift fit) ----------

def robust_align(source_a, source_b, *, fps_a: float, fps_b: float, duration: float) -> AlignResult:
    """
    1) coarse: perceptual-hash sweep -> frame offset (fast, tolerant)
    2) refine: SSIM snap on ~8 anchors around midpoints (accurate seek)
    3) drift: linear fit on (t_a, t_b) pairs
    """
    # --- 1. coarse (fast fingerprints) ---
    fpa = source_a.generate_fingerprints(120)
    fpb = source_b.generate_fingerprints(120)
    coarse_frames = _coarse_offset_from_hashes(fpa, fpb)
    coarse_offset_sec = float(coarse_frames) / max(fps_a, 1.0)

    # --- 2. SSIM refinement on accurate seeks ---
    anchors_a = _sample_anchors(duration, 8)
    matched_pairs: List[Tuple[float, float]] = []

    for t_a in anchors_a:
        # search around coarse mapping ±0.75s
        search = np.linspace(-0.75, 0.75, 13)
        best_s, best_ssim = 0.0, -1.0

        frm_a = source_a.get_frame(t_a, accurate=True)
        if frm_a is None:
            continue
        g_a = _to_gray(frm_a)
        g_a = cv2.resize(g_a, (512, 288), interpolation=cv2.INTER_AREA)

        for s in search:
            t_b = max(0.0, t_a - (coarse_offset_sec + s))
            frm_b = source_b.get_frame(t_b, accurate=True)
            if frm_b is None:
                continue
            g_b = _to_gray(frm_b)
            g_b = cv2.resize(g_b, (512, 288), interpolation=cv2.INTER_AREA)
            ssim = _ssim(g_a, g_b)
            if ssim > best_ssim:
                best_ssim, best_s = ssim, s

        matched_pairs.append((t_a, max(0.0, t_a - (coarse_offset_sec + best_s))))

    if len(matched_pairs) < 2:
        # fallback: no refinement, no drift estimate
        return AlignResult(offset_sec=coarse_offset_sec, drift_ppm=0.0, confidence=0.3)

    # --- 3. linear fit: t_b ≈ t_a - (offset + drift * t_a)
    # => y = t_a - t_b ≈ offset + drift * t_a
    ta = np.array([p[0] for p in matched_pairs], dtype=np.float64)
    tb = np.array([p[1] for p in matched_pairs], dtype=np.float64)
    y = ta - tb
    X = np.vstack([np.ones_like(ta), ta]).T      # [1, t_a]
    beta, *_ = np.linalg.lstsq(X, y, rcond=None) # beta[0]=offset, beta[1]=drift
    offset, drift = float(beta[0]), float(beta[1])

    # rough confidence (you can refine later by keeping SSIMs)
    conf = 0.8
    return AlignResult(offset_sec=offset, drift_ppm=drift, confidence=conf)
