# remux_toolkit/tools/audio_authenticity/audio_authenticity_core.py
"""
Audio Authenticity & Provenance.

Two questions this answers, which the Audio Comparison tool deliberately does
not (that one ranks quality between sources):

  1. Is this track really what it claims to be?  A lossless container can hold
     audio that was lossy earlier in its life, "stereo" can be mono, "5.1" can
     be a matrix upmix of a stereo master, "24-bit" can be 16-bit padded.
  2. Do two tracks come from the same master?

CALIBRATION (measured, not assumed). Known-lossless 48 kHz/16-bit PCM was
re-encoded and the spectral wall measured on the decoded result:

    true lossless (PCM / FLAC)  23918 Hz, no wall
    AC3 224k / 448k / 640k      20414 Hz, steep wall
    MP3 320k                    20367 Hz, steep wall
    AAC 256k                    21762 Hz, steep wall
    DTS 1536k                   23684 Hz, no usable wall

So the spectral test catches the fake that actually occurs in the wild - a
lossless wrapper around formerly-AC3/MP3/AAC audio - but cannot catch a
high-bitrate DTS source. Frame-boundary periodicity and digital-silence
integrity were both tried as a second opinion and neither separated DTS 1536k
from lossless, so a track with no wall is reported INCONCLUSIVE, never clean.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

NFFT = 8192
CHUNK_FRAMES = 1 << 18


# ---------------------------------------------------------------------------
# settings / results
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    work_dir: str = ""
    keep_decodes: bool = False
    cutoff_floor_db: float = 50.0
    lossy_cutoff_hz: float = 22500.0
    wall_steepness_db: float = 8.0
    hf_energy_frac: float = 1.0e-6
    dual_mono_corr: float = 0.99999
    fake_stereo_corr: float = 0.98
    matrix_corr: float = 0.90
    dead_channel_db: float = -90.0
    lfe_dead_db: float = -90.0
    clip_threshold: float = 0.9995
    clip_warn_frac: float = 0.0001
    dc_offset_warn: float = 0.002
    envelope_lock: float = 0.5
    envelope_skip: float = 0.12
    spectrogram: bool = True
    spectrogram_cols: int = 900
    spectrogram_rows: int = 480
    subsample_align: bool = True
    edit_min_ms: float = 1.0
    null_retimed_db: float = -10.0
    retimed_corr: float = 0.85
    correct_speed: bool = True
    speed_min_ratio: float = 2.0e-5
    align_search_s: float = 45.0
    window_s: float = 20.0
    window_step_s: float = 60.0
    window_corr_lock: float = 0.30
    null_same_master_db: float = -20.0
    null_reworked_db: float = -6.0
    drift_warn_ms: float = 5.0

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class TrackReport:
    source: str = ""
    source_label: str = ""
    stream_index: int = 0
    audio_index: int = 0
    language: str = "und"
    title: str = ""
    codec: str = ""
    profile: str = ""
    channels: int = 0
    channel_layout: str = ""
    sample_rate: int = 0
    declared_bits: int | None = None
    declared_lossless: bool | None = None
    duration_s: float = 0.0

    # spectral
    cutoff_hz: float = 0.0
    wall_db_per_khz: float = 0.0
    energy_above_wall: float = 0.0
    nyquist_hz: float = 0.0

    # integrity / level
    effective_bits: int | None = None
    channel_rms_db: list[float] = field(default_factory=list)
    channel_peak_db: list[float] = field(default_factory=list)
    channel_dc: list[float] = field(default_factory=list)
    clipped_frac: float = 0.0
    lr_correlation: float | None = None
    identical_lr: bool = False
    pair_correlations: dict = field(default_factory=dict)

    # verdicts
    content_verdict: str = ""
    content_reasons: list[str] = field(default_factory=list)
    channel_verdict: str = ""
    channel_reasons: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    error: str = ""

    # Spek-style view: dB magnitudes, shape (freq_rows, time_cols), low freq first
    spectrogram: object = None

    @property
    def name(self) -> str:
        bits = f" {self.declared_bits}bit" if self.declared_bits else ""
        return (f"{self.source_label} #{self.stream_index} "
                f"{self.profile or self.codec}{bits} {self.language}")


@dataclass
class PairReport:
    a: str = ""
    b: str = ""
    offset_samples: int = 0
    offset_ms: float = 0.0
    locked_windows: int = 0
    total_windows: int = 0
    best_corr: float = 0.0
    median_corr: float = 0.0
    drift_ms: float = 0.0
    null_db: float = 0.0
    segment_null_db: float = 0.0
    gain_db: float = 0.0
    edit_points: list = field(default_factory=list)   # (time_s, shift_ms)
    bit_exact: bool = False
    polarity_inverted: bool = False
    channels_swapped: bool = False
    speed_ratio: float = 1.0
    speed_corrected: bool = False
    speed_label: str = ""
    _retimed: object = None          # internal: decode to clean up
    tier: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class Report:
    tracks: list[TrackReport] = field(default_factory=list)
    pairs: list[PairReport] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# probing / decoding
# ---------------------------------------------------------------------------

_LOSSLESS_CODECS = {
    "pcm_s16le", "pcm_s16be", "pcm_s24le", "pcm_s24be", "pcm_s32le", "pcm_s32be",
    "pcm_f32le", "pcm_f64le", "pcm_bluray", "pcm_dvd", "flac", "alac",
    "truehd", "mlp", "wavpack", "tta", "ape",
}
_LOSSY_CODECS = {
    "ac3", "eac3", "aac", "mp3", "mp2", "vorbis", "opus", "wmav2", "cook", "sipr",
}


def check_dependencies() -> tuple[bool, str]:
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        return False, "Missing required tool(s): " + ", ".join(missing)
    return True, ""


def _declared_lossless(codec: str, profile: str) -> bool | None:
    """True/False where the container is unambiguous, None where it is not."""
    codec = (codec or "").lower()
    prof = (profile or "").lower()
    if codec == "dts":
        if "ma" in prof or "lossless" in prof:
            return True
        if prof:                       # plain "DTS", "DTS-ES", "DTS-HD HRA"
            return False
        return None
    if codec in _LOSSLESS_CODECS:
        return True
    if codec in _LOSSY_CODECS:
        return False
    return None


def probe_audio_streams(path: str) -> list[dict]:
    """Every audio stream in a file, in ffmpeg -map 0:a:N order."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index,codec_name,profile,channels,channel_layout,sample_rate,"
         "sample_fmt,bits_per_raw_sample,duration:stream_tags=language,title",
         "-of", "json", path],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {out.stderr.strip()[:300]}")
    streams = json.loads(out.stdout or "{}").get("streams", [])
    result = []
    for a_i, st in enumerate(streams):
        tags = st.get("tags", {}) or {}
        bits = st.get("bits_per_raw_sample")
        fmt = (st.get("sample_fmt") or "")
        if bits in (None, "N/A", "0"):
            bits = {"s16": 16, "s16p": 16, "s32": 32, "s32p": 32,
                    "flt": 32, "fltp": 32}.get(fmt)
            bits = None if bits in (32,) and fmt.startswith(("flt",)) else bits
        result.append({
            "audio_index": a_i,
            "index": int(st.get("index", a_i)),
            "codec": st.get("codec_name", ""),
            "profile": "" if st.get("profile") in (None, "unknown") else st.get("profile"),
            "channels": int(st.get("channels") or 0),
            "channel_layout": st.get("channel_layout", "") or "",
            "sample_rate": int(st.get("sample_rate") or 0),
            "sample_fmt": fmt,
            "declared_bits": int(bits) if bits not in (None, "N/A") else None,
            "language": tags.get("language", "und"),
            "title": tags.get("title", ""),
        })
    return result


@dataclass
class Decoded:
    raw_path: str
    dtype: str
    channels: int
    sample_rate: int
    frames: int
    full_scale: float
    env: object = None          # cached 100 Hz envelope, see _envelope()

    def memmap(self) -> np.ndarray:
        a = np.memmap(self.raw_path, dtype=self.dtype, mode="r")
        usable = len(a) // self.channels * self.channels
        return a[:usable].reshape(-1, self.channels)


def decode_track(path: str, stream: dict, settings: Settings, speed: float = 1.0) -> Decoded:
    """
    Decode one track in full to a raw file we can memmap.

    16-bit sources stay 16-bit (half the bytes); anything deeper goes to s32 so
    the effective-bit-depth check can see the real low bits.
    """
    deep = (stream.get("declared_bits") or 16) > 16 or stream.get("sample_fmt", "").startswith("flt")
    dtype, fmt, acodec = ("int32", "s32le", "pcm_s32le") if deep else ("int16", "s16le", "pcm_s16le")

    work = work_dir_for(settings)
    work.mkdir(parents=True, exist_ok=True)
    raw_path = work / f"aa_{uuid.uuid4().hex[:12]}.{fmt}"

    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", path,
           "-map", f"0:a:{stream['audio_index']}"]
    if abs(speed - 1.0) > 1e-9:
        # Re-time the way a film-speed change does: reinterpret the sample rate
        # (speed and pitch together), then land back on the original rate.
        sr = stream["sample_rate"] or 48000
        cmd += ["-af", f"asetrate={sr}*{speed:.12f},aresample={sr}:resampler=soxr"]
    cmd += ["-f", fmt, "-acodec", acodec, str(raw_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not raw_path.exists():
        raise RuntimeError(f"decode failed: {proc.stderr.strip()[:300]}")

    ch = stream["channels"] or 1
    itemsize = 4 if dtype == "int32" else 2
    frames = raw_path.stat().st_size // (itemsize * ch)
    return Decoded(str(raw_path), dtype, ch, stream["sample_rate"] or 48000,
                   frames, 2.0 ** (31 if dtype == "int32" else 15))


def work_dir_for(settings: Settings) -> Path:
    return Path(settings.work_dir or
                (Path.home() / ".cache" / "remux_toolkit" / "audio_authenticity"))


def sweep_work_dir(settings: Settings, max_age_s: float = 0.0) -> int:
    """
    Delete staged decodes left behind by a previous run.

    Normal runs clean up after themselves; this catches the case where the app
    was killed mid-analysis, so closing and reopening the tool does not leak
    gigabytes of raw audio into the cache directory.
    """
    work = work_dir_for(settings)
    if not work.is_dir():
        return 0
    now = time.time()
    removed = 0
    for f in work.glob("aa_*"):
        try:
            if max_age_s and (now - f.stat().st_mtime) < max_age_s:
                continue
            f.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def release(dec: Decoded, settings: Settings) -> None:
    if dec and not settings.keep_decodes:
        try:
            os.remove(dec.raw_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# per-track analysis
# ---------------------------------------------------------------------------

_CANON = {"FL": "FL", "FR": "FR", "FC": "FC", "LFE": "LFE", "BL": "BL", "BR": "BR",
          "SL": "SL", "SR": "SR", "BC": "BC"}


def _channel_names(layout: str, channels: int) -> list[str]:
    if layout:
        parts = [p.strip().upper() for p in layout.split("(")[0].split("+")]
        if len(parts) == channels:
            return [_CANON.get(p, p) for p in parts]
    return {1: ["FC"], 2: ["FL", "FR"], 6: ["FL", "FR", "FC", "LFE", "BL", "BR"],
            8: ["FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"]}.get(
        channels, [f"CH{i}" for i in range(channels)])


def _stream_stats(dec: Decoded, settings: Settings, progress=None, cancel=None) -> dict:
    """One pass over the whole track: spectrum, levels, correlations, bit depth."""
    data = dec.memmap()
    ch, fs = dec.channels, dec.full_scale
    win = np.hanning(NFFT).astype(np.float64)
    psd = np.zeros((ch, NFFT // 2 + 1))
    windows = 0

    cols = max(1, settings.spectrogram_cols) if settings.spectrogram else 0
    rows = max(1, settings.spectrogram_rows)
    gram_img = np.zeros((rows, cols)) if cols else None
    gram_hits = np.zeros(cols) if cols else None
    expected_windows = max(1, dec.frames // NFFT)
    row_group = max(1, (NFFT // 2 + 1) // rows)

    total = np.zeros(ch)
    sq = np.zeros(ch)
    peak = np.zeros(ch)
    clipped = np.zeros(ch)
    gram = np.zeros((ch, ch))          # cross-products for correlations
    bit_or = 0
    identical_lr = ch >= 2
    n = 0
    carry = np.zeros((0, ch))

    for start in range(0, dec.frames, CHUNK_FRAMES):
        if cancel is not None and cancel():
            raise InterruptedError("cancelled")
        block_i = np.asarray(data[start:start + CHUNK_FRAMES])
        if block_i.size == 0:
            break
        bit_or |= int(np.bitwise_or.reduce(np.abs(block_i.astype(np.int64)).ravel()))
        if identical_lr and not np.array_equal(block_i[:, 0], block_i[:, 1]):
            identical_lr = False

        block = block_i.astype(np.float64) / fs
        n += len(block)
        total += block.sum(0)
        sq += (block ** 2).sum(0)
        peak = np.maximum(peak, np.abs(block).max(0))
        clipped += (np.abs(block) >= settings.clip_threshold).sum(0)
        gram += block.T @ block

        buf = np.vstack([carry, block]) if carry.size else block
        nwin = len(buf) // NFFT
        for w in range(nwin):
            seg = buf[w * NFFT:(w + 1) * NFFT]
            mono_power = None
            for c in range(ch):
                power = np.abs(np.fft.rfft(seg[:, c] * win)) ** 2
                psd[c] += power
                mono_power = power if mono_power is None else mono_power + power
            if gram_img is not None:
                col = min(cols - 1, int(windows / expected_windows * cols))
                usable = row_group * rows
                binned = (mono_power[:usable] / ch).reshape(rows, row_group).mean(1)
                gram_img[:, col] += binned
                gram_hits[col] += 1
            windows += 1
        carry = buf[nwin * NFFT:]
        if progress:
            progress(min(1.0, (start + CHUNK_FRAMES) / max(dec.frames, 1)))

    n = max(n, 1)
    psd /= max(windows, 1)
    rms = np.sqrt(sq / n)
    corr = np.zeros((ch, ch))
    for i in range(ch):
        for j in range(ch):
            d = rms[i] * rms[j] * n
            corr[i, j] = gram[i, j] / d if d > 0 else 0.0

    trailing = 0
    if bit_or:
        while bit_or and not (bit_or & 1):
            bit_or >>= 1
            trailing += 1
    container_bits = 32 if dec.dtype == "int32" else 16
    effective = None if bit_or == 0 else max(1, container_bits - trailing)

    spectrogram = None
    if gram_img is not None and gram_hits.any():
        live = gram_hits > 0
        gram_img[:, live] /= gram_hits[live]
        # Carry the last live column across any gap so the image has no stripes.
        last = None
        for i in range(cols):
            if live[i]:
                last = gram_img[:, i]
            elif last is not None:
                gram_img[:, i] = last
        spectrogram = (10 * np.log10(np.maximum(gram_img, 1e-30))).astype(np.float32)
        spectrogram -= spectrogram.max()

    return dict(spectrogram=spectrogram, gram=gram, n=n, psd=psd, freqs=np.fft.rfftfreq(NFFT, 1 / dec.sample_rate),
                rms=rms, peak=peak, dc=total / n, clipped=clipped / n,
                corr=corr, effective_bits=effective, identical_lr=identical_lr,
                frames=n)


def measure_wall(freqs: np.ndarray, psd: np.ndarray, settings: Settings) -> tuple[float, float, float]:
    """
    Locate the codec low-pass, if any.

    Returns (cutoff_hz, steepness_db_per_khz, share_of_energy_above). A true
    lossless 48 kHz track runs to ~24 kHz with a shallow natural roll-off; a
    lossy-sourced one stops dead at the encoder's wall.
    """
    db = 10 * np.log10(np.maximum(psd, 1e-30))
    band = (freqs > 300) & (freqs < 3000)
    if not band.any():
        return float(freqs[-1]), 0.0, 0.0
    rel = db - np.median(db[band])
    live = np.where(rel > -settings.cutoff_floor_db)[0]
    if len(live) == 0:
        return 0.0, 0.0, 0.0
    cutoff = float(freqs[live[-1]])

    below = rel[(freqs > cutoff - 1000) & (freqs <= cutoff)]
    above = rel[(freqs > cutoff) & (freqs <= cutoff + 1000)]
    steep = float(np.mean(below) - np.mean(above)) if below.size and above.size else 0.0
    tail = float(np.sum(psd[freqs > cutoff + 200]) / max(np.sum(psd), 1e-30))
    return cutoff, steep, tail


def _judge_content(rep: TrackReport, settings: Settings) -> None:
    """Decide whether the audio content matches what the container claims."""
    nyq = rep.nyquist_hz
    wall_is_low = rep.cutoff_hz < min(settings.lossy_cutoff_hz, nyq - 500)
    wall_is_steep = rep.wall_db_per_khz >= settings.wall_steepness_db
    empty_above = rep.energy_above_wall <= settings.hf_energy_frac

    if wall_is_low and wall_is_steep and empty_above:
        rep.content_reasons.append(
            f"Sharp wall at {rep.cutoff_hz/1000:.2f} kHz "
            f"({rep.wall_db_per_khz:.0f} dB/kHz, {rep.energy_above_wall:.1e} of energy above) - "
            f"an encoder low-pass, not natural roll-off.")
        if rep.declared_lossless:
            rep.content_verdict = "FAKE LOSSLESS"
            rep.content_reasons.append(
                f"Container declares {rep.profile or rep.codec} (lossless) but the "
                f"content was through a lossy encoder earlier.")
        else:
            rep.content_verdict = "LOSSY (as declared)"
        return

    if wall_is_low and not wall_is_steep:
        rep.content_verdict = "BAND-LIMITED"
        rep.content_reasons.append(
            f"Energy stops around {rep.cutoff_hz/1000:.2f} kHz but rolls off gently "
            f"({rep.wall_db_per_khz:.0f} dB/kHz) - consistent with an analogue or "
            f"soft-limited master rather than a codec wall.")
        return

    if rep.declared_lossless is False:
        rep.content_verdict = "LOSSY (as declared)"
        rep.content_reasons.append(
            f"{rep.profile or rep.codec} is a lossy codec; no additional low-pass found "
            f"(high-bitrate lossy often has none).")
        return

    rep.content_verdict = "NO LOSSY FINGERPRINT"
    rep.content_reasons.append(
        f"Full bandwidth to {rep.cutoff_hz/1000:.2f} kHz with no encoder wall.")
    rep.content_reasons.append(
        "Cannot prove lossless from spectrum alone: a high-bitrate source "
        "(e.g. DTS 1536k) leaves no wall. Compare against another source for proof.")


def _judge_channels(rep: TrackReport, stats: dict, settings: Settings) -> None:
    names = _channel_names(rep.channel_layout, rep.channels)
    corr = stats["corr"]
    rms_db = rep.channel_rms_db
    idx = {nm: i for i, nm in enumerate(names)}

    def c(a: str, b: str):
        if a in idx and b in idx:
            return float(corr[idx[a], idx[b]])
        return None

    dead = [names[i] for i, v in enumerate(rms_db) if v <= settings.dead_channel_db]
    if dead:
        rep.issues.append(f"Silent channel(s): {', '.join(dead)}")

    if rep.channels == 2:
        lr = c("FL", "FR")
        rep.lr_correlation = lr
        if rep.identical_lr:
            rep.channel_verdict = "DUAL MONO"
            rep.channel_reasons.append(
                "Left and right are bit-for-bit identical - this is mono in a stereo wrapper.")
        elif lr is not None and lr >= settings.dual_mono_corr:
            rep.channel_verdict = "DUAL MONO (near-exact)"
            rep.channel_reasons.append(f"L/R correlation {lr:.6f} - effectively mono.")
        elif lr is not None and lr >= settings.fake_stereo_corr:
            rep.channel_verdict = "NEAR-MONO"
            rep.channel_reasons.append(
                f"L/R correlation {lr:.4f} - very little stereo separation; likely a mono "
                f"master presented as stereo.")
        else:
            rep.channel_verdict = "TRUE STEREO"
            if lr is not None:
                rep.channel_reasons.append(f"L/R correlation {lr:.4f}.")
        return

    if rep.channels < 2:
        rep.channel_verdict = "MONO"
        return

    # A matrix upmix does NOT have to have mono fronts - the classic one keeps
    # the original stereo up front and derives everything else, so gating on
    # "fronts are mono" misses it entirely. What gives it away is that the other
    # channels are linear combinations of the fronts, which the covariance
    # matrix already contains: corr(surround, FL-FR) and corr(FC, FL+FR).
    gram, n_samples = stats["gram"], stats["n"]

    def combo_corr(target: str, weights: dict[str, float]):
        if target not in idx or any(k not in idx for k in weights):
            return None
        w = np.zeros(gram.shape[0])
        for name, weight in weights.items():
            w[idx[name]] = weight
        var_w = float(w @ gram @ w)
        var_t = float(gram[idx[target], idx[target]])
        if var_w <= 0 or var_t <= 0:
            return None
        return float((w @ gram[idx[target], :]) / np.sqrt(var_t * var_w))

    front = c("FL", "FR")
    back_l, back_r = ("BL", "BR") if "BL" in idx else ("SL", "SR")
    surr = c(back_l, back_r)
    lfe_db = rms_db[idx["LFE"]] if "LFE" in idx else None
    centre_dup = max([v for v in (c("FC", "FL"), c("FC", "FR")) if v is not None], default=None)
    matrix_l = combo_corr(back_l, {"FL": 1.0, "FR": -1.0})
    matrix_r = combo_corr(back_r, {"FL": 1.0, "FR": -1.0})
    centre_sum = combo_corr("FC", {"FL": 1.0, "FR": 1.0})

    strong, supporting = [], []
    matrix_hit = max((abs(v) for v in (matrix_l, matrix_r) if v is not None), default=0.0)
    if matrix_hit >= settings.matrix_corr:
        strong.append(f"surrounds are the front difference: corr(surround, FL-FR) = {matrix_hit:.4f}")
    if centre_sum is not None and abs(centre_sum) >= settings.matrix_corr:
        strong.append(f"centre is the front sum: corr(FC, FL+FR) = {centre_sum:.4f}")
    if front is not None and front >= settings.fake_stereo_corr:
        strong.append(f"front pair correlation {front:.4f} (mono fronts)")

    if surr is not None and abs(surr) >= settings.fake_stereo_corr:
        supporting.append(f"surround pair correlation {surr:+.4f} - the two surrounds are "
                          f"{'copies' if surr > 0 else 'inversions'} of one another")
    if centre_dup is not None and centre_dup >= settings.fake_stereo_corr:
        supporting.append(f"centre duplicates the fronts ({centre_dup:.4f})")
    if lfe_db is not None and lfe_db <= settings.lfe_dead_db:
        supporting.append(f"LFE is dead ({lfe_db:.0f} dBFS)")

    if strong and (supporting or len(strong) >= 2):
        rep.channel_verdict = "FAKE MULTICHANNEL"
        rep.channel_reasons.extend(strong + supporting)
        rep.channel_reasons.append(
            "These channels are linear combinations of the front pair - a matrix upmix of a "
            "stereo (or mono) master, not a discrete multichannel mix.")
    else:
        rep.channel_verdict = "DISCRETE MULTICHANNEL"
        rep.channel_reasons.extend(strong + supporting)
    rep.pair_correlations = {
        "FL/FR": front, "surround pair": surr, "FC vs fronts": centre_dup,
        "surround vs FL-FR": matrix_hit or None, "FC vs FL+FR": centre_sum,
    }


def analyze_track(path: str, stream: dict, source_label: str, settings: Settings,
                  progress=None, cancel=None) -> tuple[TrackReport, Decoded | None]:
    rep = TrackReport(
        source=path, source_label=source_label, stream_index=stream["index"],
        audio_index=stream["audio_index"], language=stream["language"],
        title=stream["title"], codec=stream["codec"], profile=stream["profile"],
        channels=stream["channels"], channel_layout=stream["channel_layout"],
        sample_rate=stream["sample_rate"], declared_bits=stream["declared_bits"],
        declared_lossless=_declared_lossless(stream["codec"], stream["profile"]),
    )
    try:
        dec = decode_track(path, stream, settings)
    except Exception as e:
        rep.error = str(e)
        rep.content_verdict = "DECODE FAILED"
        return rep, None

    try:
        stats = _stream_stats(dec, settings, progress, cancel)
    except InterruptedError:
        release(dec, settings)
        raise
    except Exception as e:
        rep.error = str(e)
        rep.content_verdict = "ANALYSIS FAILED"
        release(dec, settings)
        return rep, None

    rep.duration_s = stats["frames"] / max(dec.sample_rate, 1)
    rep.nyquist_hz = dec.sample_rate / 2.0
    rep.cutoff_hz, rep.wall_db_per_khz, rep.energy_above_wall = measure_wall(
        stats["freqs"], stats["psd"].mean(0), settings)

    to_db = lambda v: float(20 * np.log10(max(v, 1e-12)))
    rep.channel_rms_db = [to_db(v) for v in stats["rms"]]
    rep.channel_peak_db = [to_db(v) for v in stats["peak"]]
    rep.channel_dc = [float(v) for v in stats["dc"]]
    rep.clipped_frac = float(stats["clipped"].max())
    rep.identical_lr = bool(stats["identical_lr"])
    rep.spectrogram = stats["spectrogram"]
    rep.effective_bits = stats["effective_bits"]

    _judge_content(rep, settings)
    _judge_channels(rep, stats, settings)

    if rep.declared_bits and rep.effective_bits and rep.effective_bits < rep.declared_bits:
        rep.issues.append(
            f"Declared {rep.declared_bits}-bit but only {rep.effective_bits} bits are ever "
            f"used - padded, not truly {rep.declared_bits}-bit.")
    if rep.clipped_frac > settings.clip_warn_frac:
        rep.issues.append(f"{rep.clipped_frac*100:.3f}% of samples at full scale (clipping).")
    for nm, dc in zip(_channel_names(rep.channel_layout, rep.channels), rep.channel_dc):
        if abs(dc) > settings.dc_offset_warn:
            rep.issues.append(f"DC offset on {nm}: {dc:+.4f}")
    if rep.cutoff_hz and rep.nyquist_hz and rep.cutoff_hz < rep.nyquist_hz * 0.55:
        rep.issues.append(
            f"Only reaches {rep.cutoff_hz/1000:.1f} kHz at a {rep.sample_rate} Hz sample rate - "
            f"possibly upsampled from a lower rate.")
    return rep, dec


# ---------------------------------------------------------------------------
# cross-source matching
# ---------------------------------------------------------------------------
# A single whole-file cross-correlation is not good enough here: two releases of
# the same episode differ in head/tail padding, level and sometimes speed, and
# the global peak can land on a lobe that nulls at 0 dB. So alignment is done in
# two stages - a coarse pass on a low-rate energy envelope, then a full-rate
# refinement in windows spread across the file, which also exposes drift.

ENV_HZ = 100.0

# Speed relationships that actually occur between releases of the same show.
KNOWN_RATIOS = {
    1000.0 / 1001.0: "NTSC pulldown (24 -> 23.976 fps)",
    1001.0 / 1000.0: "NTSC pullup (23.976 -> 24 fps)",
    24.0 / 25.0: "PAL pullup (25 -> 24 fps)",
    25.0 / 24.0: "PAL speed-up (24 -> 25 fps)",
}


def describe_ratio(ratio: float, tol: float = 3.0e-4) -> str:
    """Name a measured speed ratio when it matches a standard one."""
    for known, name in KNOWN_RATIOS.items():
        if abs(ratio - known) <= tol:
            return name
    return ""


def _envelope(dec: Decoded) -> np.ndarray:
    """
    Low-rate RMS envelope of the mono mix - robust to codec and level.

    Cached on the Decoded: every pair needs it, and recomputing means re-reading
    the whole track from disk once per pair instead of once per track.
    """
    if dec.env is not None:
        return dec.env
    frame = max(1, int(dec.sample_rate / ENV_HZ))
    data = dec.memmap()
    out = []
    for start in range(0, dec.frames, CHUNK_FRAMES):
        block = np.asarray(data[start:start + CHUNK_FRAMES], dtype=np.float64)
        if block.size == 0:
            break
        mono = block.mean(1) / dec.full_scale
        usable = len(mono) // frame * frame
        if usable:
            out.append(np.sqrt((mono[:usable].reshape(-1, frame) ** 2).mean(1)))
    if not out:
        dec.env = np.zeros(0)
        return dec.env
    env = np.concatenate(out)
    dec.env = env - env.mean()
    return dec.env


def _xcorr_lag(x: np.ndarray, y: np.ndarray, max_lag: int) -> tuple[int, float]:
    """
    Lag of x relative to y, searched over +/- max_lag.

    Positive result means x occurs later than y. Correlation is normalised by
    the overlapping energy so the score is comparable between pairs.
    """
    n = max(len(x), len(y))
    N = 1 << int(np.ceil(np.log2(2 * n + 1)))
    X = np.fft.rfft(x, N)
    Y = np.fft.rfft(y, N)
    cc = np.fft.irfft(X * np.conj(Y), N)
    pos = cc[:max_lag + 1]
    neg = cc[-max_lag:] if max_lag > 0 else np.zeros(0)
    both = np.concatenate([neg, pos])
    k = int(np.argmax(np.abs(both)))
    lag = k - len(neg)
    denom = np.linalg.norm(x) * np.linalg.norm(y) + 1e-12
    return lag, float(abs(both[k]) / denom)


def _mono_slice(dec: Decoded, lo: int, hi: int) -> tuple[np.ndarray, int]:
    """Mono mix of [lo, hi), clamped to the data. Returns (samples, actual_lo)."""
    lo = max(0, lo)
    hi = min(dec.frames, hi)
    if hi <= lo:
        return np.zeros(0), lo
    block = np.asarray(dec.memmap()[lo:hi], dtype=np.float64)
    return block.mean(1) / dec.full_scale, lo


def _refine_windows(a: Decoded, b: Decoded, coarse: int, settings: Settings) -> list[tuple[int, int, float]]:
    """
    (time_samples, lag, correlation) for probe windows across the file.

    `lag` is positive when a runs later than b, matching _null_at's convention
    (b_index = a_index - lag). The search range is clamped to b's real data
    rather than zero-padded, so a window can never "match" against padding.
    """
    sr = a.sample_rate
    win = int(settings.window_s * sr)
    win = max(sr, min(win, max(a.frames // 3, sr)))
    if a.frames < win or b.frames < win:
        return []

    step = int(settings.window_step_s * sr)
    if a.frames > win:
        step = min(step, max(1, (a.frames - win) // 7))
    step = max(step, win // 4)
    search = int(0.75 * sr)

    out = []
    for s in range(0, a.frames - win + 1, step):
        got = _probe_full(a, b, s, win, coarse, search)
        if got is not None:
            out.append(got)
    return out


def _probe_full(a: Decoded, b: Decoded, s: int, win: int, coarse: int, search: int):
    """One full-rate probe window: (start, integer lag, correlation, fractional lag)."""
    x, _ = _mono_slice(a, s, s + win)
    if x.size < win or not np.any(x):
        return None
    y, y_lo = _mono_slice(b, s - coarse - search, s - coarse + win + search)
    if y.size < win:
        return None
    N = 1 << int(np.ceil(np.log2(len(y) + len(x))))
    cc = np.fft.irfft(np.fft.rfft(y, N) * np.conj(np.fft.rfft(x, N)), N)[:len(y) - win + 1]
    mag = np.abs(cc)
    k = int(np.argmax(mag))
    frac = 0.0
    if 0 < k < len(mag) - 1:
        y1, y2, y3 = float(mag[k - 1]), float(mag[k]), float(mag[k + 1])
        denom_f = y1 - 2.0 * y2 + y3
        if abs(denom_f) > 1e-12:
            delta = 0.5 * (y1 - y3) / denom_f
            if -1.0 < delta < 1.0:
                frac = delta
    seg = y[k:k + win]
    denom = np.linalg.norm(x) * np.linalg.norm(seg) + 1e-12
    return (s, s - (y_lo + k), float(mag[k] / denom), frac)


def _null_at(a: Decoded, b: Decoded, lag: int, invert: bool = False) -> tuple[float, float, bool]:
    """
    Stream a null test at the given lag.

    Returns (null_dB_relative_to_signal, gain_dB_applied, bit_exact).
    """
    a_start = max(0, lag)
    b_start = max(0, -lag)
    n = min(a.frames - a_start, b.frames - b_start)
    if n <= 0:
        return 0.0, 0.0, False
    ch = min(a.channels, b.channels)
    A, B = a.memmap(), b.memmap()

    sa = sb = 0.0
    for s in range(0, n, CHUNK_FRAMES):
        m = min(CHUNK_FRAMES, n - s)
        xa = np.asarray(A[a_start + s:a_start + s + m, :ch], dtype=np.float64) / a.full_scale
        xb = np.asarray(B[b_start + s:b_start + s + m, :ch], dtype=np.float64) / b.full_scale
        sa += float((xa ** 2).sum())
        sb += float((xb ** 2).sum())
    ra, rb = np.sqrt(sa / (n * ch)), np.sqrt(sb / (n * ch))
    if ra <= 0 or rb <= 0:
        return 0.0, 0.0, False
    gain = ra / rb
    sign = -1.0 if invert else 1.0

    resid = 0.0
    exact = True
    for s in range(0, n, CHUNK_FRAMES):
        m = min(CHUNK_FRAMES, n - s)
        ia = np.asarray(A[a_start + s:a_start + s + m, :ch])
        ib = np.asarray(B[b_start + s:b_start + s + m, :ch])
        if exact:
            same = np.array_equal(ia, -ib.astype(np.int64)) if invert else np.array_equal(ia, ib)
            if a.dtype != b.dtype or not same:
                exact = False
        xa = ia.astype(np.float64) / a.full_scale
        xb = ib.astype(np.float64) / b.full_scale
        resid += float(((xa - sign * gain * xb) ** 2).sum())
    rr = np.sqrt(resid / (n * ch))
    return float(20 * np.log10(max(rr, 1e-12) / ra)), float(20 * np.log10(gain)), exact


def _frac_shift(x: np.ndarray, frac: float) -> np.ndarray:
    """Shift by a fractional number of samples with an FFT phase ramp."""
    if abs(frac) < 1e-6:
        return x
    n = x.shape[0]
    spec = np.fft.rfft(x, axis=0)
    ramp = np.exp(-2j * np.pi * np.fft.rfftfreq(n) * frac)
    return np.fft.irfft(spec * ramp[:, None], n, axis=0)


def _segment_nulls(a: Decoded, b: Decoded, windows: list, settings: Settings) -> list[tuple[int, int, float]]:
    """
    Null every locked window at that window's own lag.

    This is the metric that actually answers "same master?" for real releases.
    A whole-file null needs one constant offset for the entire runtime, which
    fails the moment two releases pad their gaps differently - even though every
    second of the audio is identical. Nulling each window at its own lag sees
    through that, and the lag sequence itself exposes where the edits are.
    """
    out = []
    n = int(settings.window_s * a.sample_rate)
    ch = min(a.channels, b.channels)
    edge = 128                                   # drop the FFT-shift wrap-around
    A, B = a.memmap(), b.memmap()
    for win in windows:
        s, lag = win[0], win[1]
        frac = win[3] if len(win) > 3 else 0.0
        bs = s - lag
        if bs < 0 or s + n > a.frames or bs + n > b.frames:
            continue
        xa = np.asarray(A[s:s + n, :ch], dtype=np.float64) / a.full_scale
        xb = np.asarray(B[bs:bs + n, :ch], dtype=np.float64) / b.full_scale
        if settings.subsample_align and frac:
            xb = _frac_shift(xb, -frac)
        if n > 4 * edge:
            xa, xb = xa[edge:-edge], xb[edge:-edge]
        ra = np.sqrt((xa ** 2).mean())
        rb = np.sqrt((xb ** 2).mean())
        if ra <= 0 or rb <= 0:
            continue
        gain = ra / rb
        best = min(
            float(np.sqrt(((xa - sign * gain * xb) ** 2).mean()))
            for sign in (1.0, -1.0))
        out.append((s, lag, float(20 * np.log10(max(best, 1e-12) / ra))))
    return out


def _edit_points(segments: list, sample_rate: int, min_ms: float = 1.0) -> list[tuple[float, float]]:
    """
    Where the lag genuinely jumps between consecutive matched windows.

    A lag that wobbles by a sample or two between windows is measurement noise,
    not an edit, so only shifts of at least `min_ms` are reported.
    """
    edits = []
    for (s_prev, lag_prev, _), (s_now, lag_now, _) in zip(segments, segments[1:]):
        shift_ms = (lag_prev - lag_now) / sample_rate * 1000.0
        if abs(shift_ms) >= min_ms:
            edits.append(((s_prev + s_now) / 2 / sample_rate, shift_ms))
    return edits


def _channel_orientation(a: Decoded, b: Decoded, lag: int, settings: Settings) -> bool:
    """True when L/R appear swapped between the two tracks."""
    if a.channels < 2 or b.channels < 2:
        return False
    sr = a.sample_rate
    win = int(min(settings.window_s, 20.0) * sr)
    s = max(0, min(a.frames - win, a.frames // 3))
    bs = s - lag
    if bs < 0 or bs + win > b.frames:
        return False
    xa = np.asarray(a.memmap()[s:s + win, :2], dtype=np.float64)
    xb = np.asarray(b.memmap()[bs:bs + win, :2], dtype=np.float64)

    def corr(u, v):
        du, dv = np.linalg.norm(u), np.linalg.norm(v)
        return float(u @ v / (du * dv)) if du and dv else 0.0

    straight = corr(xa[:, 0], xb[:, 0]) + corr(xa[:, 1], xb[:, 1])
    crossed = corr(xa[:, 0], xb[:, 1]) + corr(xa[:, 1], xb[:, 0])
    return crossed > straight + 0.05


def _probe(x: np.ndarray, y: np.ndarray, s: int, win: int, search: int):
    """Locate x[s:s+win] inside y, searching +/- `search` around the same position."""
    seg_x = x[s:s + win]
    if len(seg_x) < win or not np.any(seg_x):
        return None
    lo = max(0, s - search)
    hi = min(len(y), s + win + search)
    y_win = y[lo:hi]
    if len(y_win) < win:
        return None
    N = 1 << int(np.ceil(np.log2(len(y_win) + win)))
    cc = np.fft.irfft(np.fft.rfft(y_win, N) * np.conj(np.fft.rfft(seg_x, N)), N)
    cc = cc[:len(y_win) - win + 1]
    mag = np.abs(cc)
    k = int(np.argmax(mag))
    # Parabolic sub-sample peak fit. Integer alignment is not good enough for a
    # null: half a sample of error caps cancellation at about -4 dB by 10 kHz.
    frac = 0.0
    if 0 < k < len(mag) - 1:
        y1, y2, y3 = float(mag[k - 1]), float(mag[k]), float(mag[k + 1])
        denom_f = y1 - 2.0 * y2 + y3
        if abs(denom_f) > 1e-12:
            delta = 0.5 * (y1 - y3) / denom_f
            if -1.0 < delta < 1.0:
                frac = delta
    seg_y = y_win[k:k + win]
    denom = np.linalg.norm(seg_x) * np.linalg.norm(seg_y) + 1e-12
    return s - (lo + k), float(abs(cc[k]) / denom), frac


def _envelope_align(env_a: np.ndarray, env_b: np.ndarray, settings: Settings) -> list[tuple[int, int, float]]:
    """
    Windowed alignment on the 100 Hz envelopes.

    Drift is measured here rather than at full rate on purpose: a 0.1 % speed
    difference smears a 20 s full-rate correlation badly, but over a 30 s
    envelope window it is only three frames, so the lag trend stays readable.
    """
    win = max(int(5 * ENV_HZ), min(int(30 * ENV_HZ), max(len(env_a) // 4, int(5 * ENV_HZ))))
    if len(env_a) < win or len(env_b) < win:
        return []
    step = max(win // 2, 1)
    search = int(settings.align_search_s * ENV_HZ)
    pts = []
    for s in range(0, len(env_a) - win + 1, step):
        got = _probe(env_a, env_b, s, win, search)
        if got is not None:
            pts.append((s, got[0], got[1]))          # frac unused at 100 Hz
    return pts


def _align(a: Decoded, b: Decoded, settings: Settings, cancel=None) -> dict | None:
    """
    Two-stage alignment: drift and coarse offset from the envelopes, then a
    full-rate refinement for a sample-accurate lag.
    """
    env_a, env_b = _envelope(a), _envelope(b)
    if env_a.size == 0 or env_b.size == 0:
        return None

    scale = a.sample_rate / ENV_HZ
    env_pts = _envelope_align(env_a, env_b, settings)
    if env_pts and max(p[2] for p in env_pts) < settings.envelope_skip:
        # Nothing in the envelopes lines up anywhere; a full-rate search would
        # only confirm that at a hundred times the cost.
        return dict(total=len(env_pts), locked=0, env_corr=max(p[2] for p in env_pts),
                    best_corr=0.0, median_corr=0.0, lag=0, drift_ms=0.0,
                    speed_ratio=1.0, env_locked=0, prev_drift_ms=0.0, windows=[])
    good = [p for p in env_pts if p[2] >= settings.envelope_lock]
    env_corr = max((p[2] for p in env_pts), default=0.0)

    if good:
        lags = np.array([p[1] for p in good], dtype=float)
        times = np.array([p[0] for p in good], dtype=float)
        coarse = int(round(float(np.median(lags)) * scale))
        env_drift_ms = float(lags.max() - lags.min()) / ENV_HZ * 1000.0
        env_speed = 1.0
        if len(good) >= 3 and float(np.ptp(times)) > 0:
            env_speed = 1.0 + float(np.polyfit(times, lags, 1)[0])
    else:
        env_lag, env_corr2 = _xcorr_lag(env_a, env_b, int(settings.align_search_s * ENV_HZ))
        env_corr = max(env_corr, env_corr2)
        coarse = int(round(env_lag * scale))
        env_drift_ms, env_speed = 0.0, 1.0

    windows = _refine_windows(a, b, coarse, settings)
    if cancel is not None and cancel():
        raise InterruptedError("cancelled")
    if not windows:
        return None

    corrs = np.array([w[2] for w in windows]) if windows else np.zeros(1)
    locked = [w for w in windows if w[2] >= settings.window_corr_lock]
    info = dict(total=len(windows), locked=len(locked), env_corr=env_corr,
                best_corr=float(corrs.max()), median_corr=float(np.median(corrs)),
                lag=coarse, drift_ms=env_drift_ms, speed_ratio=env_speed,
                env_locked=len(good), prev_drift_ms=0.0, windows=locked)

    # The envelope stage can see a match that full-rate windows cannot, because
    # drift destroys full-rate correlation long before it troubles the envelope.
    if len(good) >= 2 and not locked:
        info["locked"] = len(good)
        info["total"] = max(len(env_pts), 1)
        info["best_corr"] = env_corr
        info["median_corr"] = float(np.median([p[2] for p in good]))
        return info

    if not locked:
        return info

    lags = np.array([w[1] for w in locked], dtype=float)
    times = np.array([w[0] for w in locked], dtype=float)
    info["windows"] = locked
    info["lag"] = int(round(float(np.median(lags))))
    full_drift = float(lags.max() - lags.min()) / a.sample_rate * 1000.0
    if not good or full_drift <= env_drift_ms:
        info["drift_ms"] = full_drift
        if len(locked) >= 3 and float(np.ptp(times)) > 0:
            info["speed_ratio"] = 1.0 + float(np.polyfit(times, lags, 1)[0])
    return info


def _retime_and_realign(a_rep: TrackReport, a: Decoded, b_rep: TrackReport, b: Decoded,
                        align: dict, settings: Settings, cancel=None):
    """
    Re-decode b at the measured speed and realign.

    Returns (new_decoded_b, new_align, factor) when the correction genuinely
    reduces drift, else None. The sign is verified by measurement rather than
    trusted: the inverse is tried too, and only a real improvement is accepted.
    """
    slope = align["speed_ratio"] - 1.0
    candidates = [1.0 - slope, 1.0 / (1.0 - slope)] if abs(slope) < 0.5 else []
    prev_drift = align["drift_ms"]
    best = None
    try:
        stream = probe_audio_streams(b_rep.source)[b_rep.audio_index]
    except Exception:
        return None

    for factor in candidates:
        snapped = factor
        for known in KNOWN_RATIOS:
            if abs(factor - known) <= 3.0e-4:
                snapped = known
                break
        try:
            b2 = decode_track(b_rep.source, stream, settings, speed=snapped)
        except Exception:
            continue
        try:
            new_align = _align(a, b2, settings, cancel)
        except InterruptedError:
            release(b2, settings)
            raise
        if new_align and new_align["locked"] and new_align["drift_ms"] < prev_drift * 0.5:
            if best is not None:
                release(best[0], settings)
            new_align["prev_drift_ms"] = prev_drift
            best = (b2, new_align, snapped)
            break
        release(b2, settings)
    return best


def compare_tracks(a_rep: TrackReport, a: Decoded, b_rep: TrackReport, b: Decoded,
                   settings: Settings, cancel=None) -> PairReport:
    pair = PairReport(a=a_rep.name, b=b_rep.name)
    retimed: Decoded | None = None
    try:
        return _compare(pair, a_rep, a, b_rep, b, settings, cancel)
    finally:
        if pair.speed_corrected and pair._retimed is not None:
            release(pair._retimed, settings)


def _compare(pair: PairReport, a_rep: TrackReport, a: Decoded, b_rep: TrackReport,
             b: Decoded, settings: Settings, cancel=None) -> PairReport:
    retimed = None
    if a.sample_rate != b.sample_rate:
        pair.notes.append(f"Different sample rates ({a.sample_rate} vs {b.sample_rate} Hz); "
                          f"comparison is approximate.")

    align = _align(a, b, settings, cancel)
    if align is None:
        pair.tier = "UNRELATED"
        pair.notes.append("No usable comparison windows.")
        return pair
    pair.total_windows = align["total"]
    pair.locked_windows = align["locked"]
    pair.best_corr = align["best_corr"]
    pair.median_corr = align["median_corr"]

    if not align["locked"]:
        pair.tier = "UNRELATED"
        pair.notes.append(
            f"No window aligned (best correlation {pair.best_corr:.3f}); envelope "
            f"correlation {align['env_corr']:.3f}. These do not look like the same content.")
        return pair

    segments = _segment_nulls(a, b, align.get("windows", []), settings)
    seg_null = float(np.median([x[2] for x in segments])) if segments else 0.0

    # Only reach for resampling if the segments do NOT already match. Apparent
    # drift is far more often different gaps between segments than a real speed
    # difference, and resampling a pair that already matches just adds error.
    if (seg_null > settings.null_same_master_db
            and settings.correct_speed
            and abs(align["speed_ratio"] - 1.0) > settings.speed_min_ratio
            and align["drift_ms"] > settings.drift_warn_ms):
        fixed = _retime_and_realign(a_rep, a, b_rep, b, align, settings, cancel)
        if fixed is not None:
            retimed, align, factor = fixed
            b = retimed
            pair._retimed = retimed
            pair.speed_corrected = True
            pair.speed_ratio = factor
            segments = _segment_nulls(a, b, align.get("windows", []), settings)
            seg_null = float(np.median([x[2] for x in segments])) if segments else 0.0
            pair.speed_label = describe_ratio(factor)
            label = f" - {pair.speed_label}" if pair.speed_label else ""
            pair.notes.append(
                f"One runs {abs(1-factor)*100:.4f}% {'fast' if factor > 1 else 'slow'} "
                f"relative to the other{label}. Re-timed before comparing; "
                f"alignment drift fell from {fixed[1]['prev_drift_ms']:.0f} ms to "
                f"{align['drift_ms']:.0f} ms.")
        else:
            pair.speed_ratio = align["speed_ratio"]
    else:
        pair.speed_ratio = align["speed_ratio"]

    pair.segment_null_db = seg_null
    pair.edit_points = _edit_points(segments, a.sample_rate, settings.edit_min_ms)
    lag = align["lag"]
    pair.offset_samples = lag
    pair.offset_ms = lag / a.sample_rate * 1000.0
    pair.drift_ms = align["drift_ms"]

    null_pos, gain_db, exact_pos = _null_at(a, b, lag, invert=False)
    null_neg, _, exact_neg = _null_at(a, b, lag, invert=True)
    pair.gain_db = gain_db
    if null_neg < null_pos - 3.0:
        pair.polarity_inverted = True
        pair.null_db, pair.bit_exact = null_neg, exact_neg
        pair.notes.append("Polarity is inverted between these two - one is phase-flipped. "
                          "Everything below is measured after correcting for that.")
    else:
        pair.null_db, pair.bit_exact = null_pos, exact_pos
    pair.channels_swapped = _channel_orientation(a, b, lag, settings)
    if pair.channels_swapped:
        pair.notes.append("Left/right appear swapped between these two.")

    _assign_tier(pair, settings)
    return pair


def _assign_tier(pair: PairReport, settings: Settings) -> None:
    best_null = min(pair.null_db, pair.segment_null_db) if pair.segment_null_db else pair.null_db
    if pair.bit_exact:
        pair.tier = "IDENTICAL"
        pair.notes.append("Bit-for-bit identical after alignment - the same audio data.")
    elif best_null <= settings.null_same_master_db:
        pair.tier = "SAME MASTER"
        if pair.edit_points:
            where = ", ".join(f"{t/60:.0f}:{t%60:04.1f} ({d:+.0f} ms)"
                              for t, d in pair.edit_points[:4])
            pair.notes.append(
                f"Every matched section cancels to {pair.segment_null_db:.1f} dB - the same "
                f"master. They are assembled differently though: the offset shifts at {where}"
                + (" and elsewhere." if len(pair.edit_points) > 4 else "."))
            pair.notes.append(
                "A whole-file null cannot show this, because no single offset fits the "
                "whole runtime; each section has to be lined up on its own.")
        else:
            pair.notes.append(
                f"Cancels to {best_null:.1f} dB at a constant offset. Only the same master "
                f"does that; the residual is encoder loss, not different audio.")
    elif (pair.speed_corrected and best_null <= settings.null_retimed_db
            and pair.median_corr >= settings.retimed_corr):
        pair.tier = "SAME MASTER (RE-TIMED)"
        pair.notes.append(
            f"Cancels to {best_null:.1f} dB after re-timing, with windows correlating at "
            f"{pair.median_corr:.3f}. Re-sampling sets a floor on how deep a re-timed null "
            f"can go, so this is as close to a match as speed-shifted sources get.")
    elif best_null <= settings.null_reworked_db:
        pair.tier = "SAME MASTER, REWORKED"
        pair.notes.append(
            f"Cancels to {best_null:.1f} dB - clearly the same performance, but level, EQ "
            f"or re-sampling differs, so it is not the same master file.")
    else:
        pair.tier = "RELATED"
        pair.notes.append(
            f"Aligns ({pair.locked_windows}/{pair.total_windows} windows, best correlation "
            f"{pair.best_corr:.3f}) but only cancels to {best_null:.1f} dB - same content "
            f"from a different master or mix.")

    if abs(pair.gain_db) > 0.2:
        pair.notes.append(f"Level differs by {pair.gain_db:+.2f} dB.")
    explained = bool(pair.edit_points) and pair.tier.startswith("SAME MASTER")
    if pair.drift_ms > settings.drift_warn_ms and not pair.speed_corrected and not explained:
        pair.notes.append(
            f"Alignment moves {pair.drift_ms:.1f} ms across the file - drift or an edit, "
            f"not a constant offset.")
    if not pair.speed_corrected and not explained and abs(pair.speed_ratio - 1.0) > 1e-5:
        named = describe_ratio(1.0 / pair.speed_ratio) or describe_ratio(pair.speed_ratio)
        pair.notes.append(
            f"Playback speed differs by {(pair.speed_ratio-1)*100:+.4f}%"
            f"{' - ' + named if named else ''}.")


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def run_analysis(sources: list[str], settings: Settings, progress=None,
                 status=None, cancel=None) -> Report:
    """
    Analyse every audio track in every source, then compare every pair.

    `progress(fraction)` and `status(text)` are optional callbacks; `cancel()`
    returning True aborts and raises InterruptedError after cleaning up.
    """
    report = Report()
    ok, msg = check_dependencies()
    if not ok:
        report.errors.append(msg)
        return report

    def say(text):
        if status:
            status(text)

    def tick(frac):
        if progress:
            progress(max(0.0, min(1.0, frac)))

    jobs = []
    for path in sources:
        label = Path(path).parent.name or Path(path).name
        try:
            streams = probe_audio_streams(path)
        except Exception as e:
            report.errors.append(f"{path}: {e}")
            continue
        if not streams:
            report.errors.append(f"{path}: no audio streams found.")
            continue
        for st in streams:
            jobs.append((path, label, st))

    if not jobs:
        return report

    decoded: list[tuple[TrackReport, Decoded | None]] = []
    n_pairs = len(jobs) * (len(jobs) - 1) // 2
    track_share = 0.65 if n_pairs else 1.0

    try:
        for i, (path, label, st) in enumerate(jobs):
            if cancel is not None and cancel():
                raise InterruptedError("cancelled")
            say(f"Analysing {label} #{st['index']} ({st['language']}, "
                f"{st['profile'] or st['codec']})  [{i+1}/{len(jobs)}]")
            base = i / len(jobs) * track_share
            rep, dec = analyze_track(
                path, st, label, settings,
                progress=lambda f, b=base: tick(b + f / len(jobs) * track_share),
                cancel=cancel)
            report.tracks.append(rep)
            decoded.append((rep, dec))

        usable = [(r, d) for r, d in decoded if d is not None]
        done = 0
        for i in range(len(usable)):
            for j in range(i + 1, len(usable)):
                if cancel is not None and cancel():
                    raise InterruptedError("cancelled")
                (ra, da), (rb, db) = usable[i], usable[j]
                say(f"Comparing {ra.name}  vs  {rb.name}")
                try:
                    report.pairs.append(compare_tracks(ra, da, rb, db, settings, cancel))
                except InterruptedError:
                    raise
                except Exception as e:
                    report.errors.append(f"compare {ra.name} vs {rb.name}: {e}")
                done += 1
                tick(track_share + done / max(n_pairs, 1) * (1 - track_share))
    finally:
        for _, dec in decoded:
            if dec is not None:
                release(dec, settings)

    tick(1.0)
    return report


def format_report(report: Report) -> str:
    """Plain-text rendering of a Report, suitable for the log pane or a file."""
    out: list[str] = []
    if report.errors:
        out.append("=== Errors ===")
        out.extend(f"  {e}" for e in report.errors)
        out.append("")

    out.append("=== Tracks ===")
    for t in report.tracks:
        out.append(f"\n{t.name}")
        out.append(f"  source        : {t.source}")
        bits = f"{t.declared_bits}-bit" if t.declared_bits else "bit depth n/a"
        declared = {True: "lossless", False: "lossy", None: "unknown"}[t.declared_lossless]
        out.append(f"  container     : {t.codec}"
                   f"{' / ' + t.profile if t.profile else ''}, {bits}, declared {declared}")
        out.append(f"  format        : {t.channels}ch {t.channel_layout or 'layout n/a'}, "
                   f"{t.sample_rate} Hz, {t.duration_s/60:.2f} min")
        if t.error:
            out.append(f"  ERROR         : {t.error}")
            continue
        out.append(f"  spectrum      : content to {t.cutoff_hz/1000:.2f} kHz "
                   f"(Nyquist {t.nyquist_hz/1000:.1f} kHz), wall {t.wall_db_per_khz:.1f} dB/kHz, "
                   f"{t.energy_above_wall:.2e} of energy above")
        if t.effective_bits:
            out.append(f"  bit depth     : {t.effective_bits} bits actually used")
        rms = ", ".join(f"{v:.1f}" for v in t.channel_rms_db)
        out.append(f"  levels        : RMS [{rms}] dBFS, peak "
                   f"{max(t.channel_peak_db) if t.channel_peak_db else 0:.2f} dBFS")
        out.append(f"  CONTENT       : {t.content_verdict}")
        out.extend(f"      - {r}" for r in t.content_reasons)
        out.append(f"  CHANNELS      : {t.channel_verdict}")
        out.extend(f"      - {r}" for r in t.channel_reasons)
        if t.issues:
            out.append("  ISSUES        :")
            out.extend(f"      ! {r}" for r in t.issues)

    if report.pairs:
        out.append("\n\n=== Source matching ===")
        for p in sorted(report.pairs, key=lambda x: x.null_db):
            out.append(f"\n{p.a}\n  vs {p.b}")
            out.append(f"  VERDICT       : {p.tier}")
            out.append(f"  alignment     : {p.offset_ms:+.2f} ms "
                       f"({p.locked_windows}/{p.total_windows} windows locked, "
                       f"best corr {p.best_corr:.3f})")
            out.append(f"  null residual : whole-file {p.null_db:+.2f} dB, "
                       f"per-section {p.segment_null_db:+.2f} dB "
                       f"(gain matched {p.gain_db:+.2f} dB)")
            if p.edit_points:
                pts = ", ".join(f"{t/60:.0f}:{t%60:04.1f} {d:+.0f} ms" for t, d in p.edit_points)
                out.append(f"  edit points   : {pts}")
            out.extend(f"      - {n}" for n in p.notes)
    return "\n".join(out)
