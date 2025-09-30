# remux_toolkit/tools/video_ab_comparator/core/alignment.py
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional, Tuple
import numpy as np
import imagehash
import cv2

# ... (small helpers like _safe_fraction_to_fps, _hamming, etc. are unchanged) ...
def _safe_fraction_to_fps(r_frame_rate: Optional[str]) -> float:
    if not r_frame_rate: return 24.0
    s = str(r_frame_rate)
    if "/" in s:
        n, d = s.split("/", 1)
        try:
            n, d = float(n.strip()), float(d.strip())
            return n / d if d != 0 else 24.0
        except Exception: pass
    try: return float(s)
    except Exception: return 24.0

def _hamming(a, b) -> int:
    try: return int(a - b)
    except Exception:
        try:
            ha, hb = imagehash.hex_to_hash(str(a)), imagehash.hex_to_hash(str(b))
            return int(ha - hb)
        except Exception: return 64

def _to_gray(img_bgr) -> np.ndarray:
    return cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

def _ssim(a: np.ndarray, b: np.ndarray) -> float:
    C1, C2 = (0.01 * 255)**2, (0.03 * 255)**2
    a, b = a.astype(np.float32), b.astype(np.float32)
    mu_a, mu_b = cv2.GaussianBlur(a, (7, 7), 1.5), cv2.GaussianBlur(b, (7, 7), 1.5)
    mu_a2, mu_b2, mu_ab = mu_a*mu_a, mu_b*mu_b, mu_a*mu_b
    sigma_a2 = cv2.GaussianBlur(a*a, (7, 7), 1.5) - mu_a2
    sigma_b2 = cv2.GaussianBlur(b*b, (7, 7), 1.5) - mu_b2
    sigma_ab = cv2.GaussianBlur(a*b, (7, 7), 1.5) - mu_ab
    num = (2*mu_ab + C1) * (2*sigma_ab + C2)
    den = (mu_a2 + mu_b2 + C1) * (sigma_a2 + sigma_b2 + C2)
    return float((num / (den + 1e-9)).mean())

def _sample_anchors(duration_sec: float, count: int) -> List[float]:
    if duration_sec <= 0: return []
    return list(np.linspace(duration_sec * 0.1, duration_sec * 0.9, num=count))

@dataclass
class AlignResult:
    offset_sec: float
    drift_ppm: float
    confidence: float

def _coarse_offset_from_hashes(fpa: List, fpb: List) -> int:
    if not fpa or not fpb: return 0
    n_a, n_b = len(fpa), len(fpb)
    max_shift = max(1, min(n_a, n_b) // 3)
    best = (float("inf"), 0)
    for shift in range(-max_shift, max_shift + 1):
        a, b = (fpa[:n_a + shift], fpb[-shift:]) if shift < 0 else (fpa[shift:], fpb[:n_b - shift])
        if not a or not b: continue
        m = min(len(a), len(b))
        if m == 0: continue
        mean_dist = sum(_hamming(a[i], b[i]) for i in range(m)) / m
        if mean_dist < best[0]:
            best = (mean_dist, shift)
    return int(best[1])

def robust_align(source_a, source_b, *, fps_a: float, fps_b: float, duration: float, progress_callback=None) -> AlignResult:
    # 1. Coarse alignment with fingerprints (fast)
    fpa = source_a.generate_fingerprints(120)
    fpb = source_b.generate_fingerprints(120)
    coarse_frames = _coarse_offset_from_hashes(fpa, fpb)
    coarse_offset_sec = float(coarse_frames) / max(fps_a, 1.0)

    # 2. Refine with "Seek then Scan" method (accurate and much faster)
    anchors_a = _sample_anchors(duration, 8)
    matched_pairs: List[Tuple[float, float]] = []

    scan_radius_sec = 1.0 # Scan 2 seconds total (-1s to +1s)

    for i, t_a in enumerate(anchors_a):
        # One slow, accurate seek for source A
        frm_a = source_a.get_frame(t_a, accurate=True)
        if frm_a is None: continue
        g_a = _to_gray(frm_a)
        g_a = cv2.resize(g_a, (512, 288), interpolation=cv2.INTER_AREA)

        # Estimate where the frame should be in B and define a scan window
        t_b_est = t_a - coarse_offset_sec
        scan_start_time = max(0.0, t_b_est - scan_radius_sec)

        best_ssim, best_ts_b = -1.0, 0.0

        # Use the fast iterator to scan the small window in source B
        for frame_idx, frm_b in enumerate(source_b.get_frame_iterator(scan_start_time, scan_radius_sec * 2)):
            g_b = _to_gray(frm_b)
            g_b = cv2.resize(g_b, (512, 288), interpolation=cv2.INTER_AREA)

            ssim = _ssim(g_a, g_b)
            if ssim > best_ssim:
                best_ssim = ssim
                best_ts_b = scan_start_time + (frame_idx / max(fps_b, 1.0))

        if best_ssim > -1.0:
            matched_pairs.append((t_a, best_ts_b))

        if progress_callback:
            progress_callback(i + 1, len(anchors_a))

    if len(matched_pairs) < 2:
        return AlignResult(offset_sec=coarse_offset_sec, drift_ppm=0.0, confidence=0.3)

    # 3. Linear fit to find final offset and drift (unchanged)
    ta = np.array([p[0] for p in matched_pairs], dtype=np.float64)
    tb = np.array([p[1] for p in matched_pairs], dtype=np.float64)
    y = ta - tb
    X = np.vstack([np.ones_like(ta), ta]).T
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    offset, drift = float(beta[0]), float(beta[1])

    return AlignResult(offset_sec=offset, drift_ppm=drift, confidence=0.8)
