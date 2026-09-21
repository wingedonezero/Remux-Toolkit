# remux_toolkit/tools/audio_authenticity/audio_authenticity_config.py
"""
Defaults for the Audio Authenticity & Provenance tool.

Thresholds that came from measurement rather than guesswork are marked with the
number they were calibrated against; see the docstrings in the core module.
"""
from pathlib import Path

DEFAULTS = {
    # --- where full-file decodes are staged (NOT /tmp: that is tmpfs here) ---
    "work_dir": str(Path.home() / ".cache" / "remux_toolkit" / "audio_authenticity"),
    "keep_decodes": False,

    # --- lossy-source (fake lossless) detection ---
    # Calibrated by encoding known-lossless PCM to AC3 224/448/640k, MP3 320k,
    # AAC 256k and DTS 1536k: every AC3 landed at 20414 Hz, MP3 320k at 20367,
    # AAC 256k at 21762, true lossless at 23918.
    "cutoff_floor_db": 50.0,        # how far under mid-band counts as "gone"
    "lossy_cutoff_hz": 22500.0,     # a wall below this is not natural for 48 kHz
    "wall_steepness_db": 8.0,       # dB per kHz across the wall
    "hf_energy_frac": 1.0e-6,       # share of total energy above the wall

    # --- channel authenticity ---
    "dual_mono_corr": 0.99999,      # L/R this correlated is mono in a stereo wrapper
    "fake_stereo_corr": 0.98,
    "dead_channel_db": -90.0,
    "lfe_dead_db": -90.0,

    # --- level / integrity ---
    "clip_threshold": 0.9995,
    "clip_warn_frac": 0.0001,
    "dc_offset_warn": 0.002,

    # --- cross-source matching ---
    "envelope_lock": 0.5,
    # Re-timing means re-sampling, which sets a floor on null depth,
    # so a corrected pair is judged on correlation as well.
    "edit_min_ms": 1.0,
    "null_retimed_db": -10.0,
    "retimed_corr": 0.85,
    "correct_speed": True,          # undo NTSC/PAL speed differences before comparing
    "speed_min_ratio": 2.0e-5,
    "align_search_s": 45.0,         # how far apart two sources may start
    "window_s": 20.0,               # length of each alignment probe
    "window_step_s": 60.0,
    "window_corr_lock": 0.30,       # a window at/above this counts as locked
    # Measured encoder loss against the very same source master:
    #   AC3 224k -28.6 dB, DTS 1536k -47 dB; a different master sits near -10 dB.
    "null_same_master_db": -20.0,
    "null_reworked_db": -6.0,
    "drift_warn_ms": 5.0,
}
