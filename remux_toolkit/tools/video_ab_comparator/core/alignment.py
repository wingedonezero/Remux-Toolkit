# remux_toolkit/tools/video_ab_comparator/core/alignment.py
from __future__ import annotations
from typing import List, Tuple, Optional
import numpy as np
import imagehash
import cv2

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
    C1 = (0.01 * 255) ** 2; C2 = (0.03 * 255) ** 2
    a = a.astype(np.float32); b = b.astype(np.float32)
    mu_a = cv2.GaussianBlur(a, (7, 7), 1.5); mu_b = cv2.GaussianBlur(b, (7, 7), 1.5)
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

def _grab_gray_at(source, ts: float, size=(512, 288), *, accurate: bool) -> Optional[np.ndarray]:
    frame = source.get_frame(ts, accurate=accurate)
    if frame is None:
        return None
    g = _to_gray(frame)
    if size:
        g = cv2.resize(g, size, interpolation=cv2.INTER_AREA)
    return g

def align_sources(fingerprints_a: List, fingerprints_b: List) -> int:
    if not fingerprints_a or not fingerprints_b:
        return 0
    n_a = len(fingerprints_a); n_b = len(fingerprints_b)
    max_shift = max(1, min(n_a, n_b) // 2)
    best_offset, best_mean = 0, float("inf")
    for offset in range(-max_shift, max_shift + 1):
        if offset < 0:
            a_slice = fingerprints_a[: n_a + offset]
            b_slice = fingerprints_b[-offset : -offset + len(a_slice)]
        else:
            a_slice = fingerprints_a[offset:]; b_slice = fingerprints_b[: len(a_slice)]
        if not a_slice or not b_slice:
            continue
        dists = [_hamming(ha, hb) for ha, hb in zip(a_slice, b_slice)]
        mean_dist = float(np.mean(dists))
        if mean_dist < best_mean:
            best_mean = mean_dist; best_offset = offset
    return best_offset

def _best_offset_by_ssim(source_a, source_b, ts_list, base_offset_frames, fps, radius=2):
    candidates = [base_offset_frames + k for k in range(-radius, radius + 1)]
    scores = []
    for cand in candidates:
        total = 0.0; used = 0
        for ts in ts_list:
            ga = _grab_gray_at(source_a, ts, accurate=True)          # accurate only here
            if ga is None: continue
            ts_b = ts - (cand / max(fps, 1e-6))
            gb = _grab_gray_at(source_b, ts_b, accurate=True)        # accurate only here
            if gb is None or ga.shape != gb.shape: continue
            total += _ssim(ga, gb); used += 1
        scores.append(total / used if used else -1.0)
    best_idx = int(np.argmax(scores))
    best_offset = candidates[best_idx]
    best_mean = scores[best_idx]

    local = []
    for ts in ts_list:
        ga = _grab_gray_at(source_a, ts, accurate=True)
        if ga is None: continue
        best_local = base_offset_frames; best_local_score = -1.0
        for cand in candidates:
            ts_b = ts - (cand / max(fps, 1e-6))
            gb = _grab_gray_at(source_b, ts_b, accurate=True)
            if gb is None or ga.shape != gb.shape: continue
            s = _ssim(ga, gb)
            if s > best_local_score:
                best_local_score = s; best_local = cand
        local.append(best_local)

    return best_offset, best_mean, local

def robust_align(source_a, source_b, fingerprints_a: List, fingerprints_b: List, *, anchors: int = 14):
    coarse = align_sources(fingerprints_a, fingerprints_b)
    fps = _safe_fraction_to_fps(next((s.frame_rate for s in source_a.info.streams if s.codec_type == "video"), "24"))
    ts_list = _sample_anchors(source_a.info.duration, max(8, anchors))
    refined, mean_ssim, local = _best_offset_by_ssim(source_a, source_b, ts_list, coarse, fps, radius=2)
    conf = float(np.clip((mean_ssim - 0.4) / (0.98 - 0.4), 0.0, 1.0))
    drift = float(max(local) - min(local)) if local else 0.0
    return refined, conf, drift
