# Developer Notes: Audio Comparison Analysis

## Metrics & Interpretation (Remuxer Focus)

### Core Metrics
- **Crest Factor (Peak-to-RMS)**: Measures peak-to-average level per block. Lower values imply heavier limiting or aggressive leveling.
- **Loudness Range (LRA)**: Variability of block loudness. Used separately from crest factor.
- **Dialogue Clarity**: Center-channel clipping/NR penalties to emphasize dialog preservation.
- **Mastering Accuracy**: Reference-vs-candidate EQ delta magnitude (mean absolute difference).

### Forensic Signals
- **EQ Delta Map**: Time/frequency delta from the reference log-power spectrogram.
  - Flags: *NR/Muffleness* (2–7 kHz drop), *Boominess* (~120 Hz boost).
- **Pitch/Speed**: f0 ratio vs reference for PAL/NTSC pitch (≈0.7 semitones) and speed shifts.
- **Channel Integrity**: Surround swap detection, LFE roll-off check above 120 Hz.
- **Limiting Heatmap**: Windowed limiting detection with waveform zooms at hot spots.
- **Transient Spikes**: Discontinuity detection for pops/crackles.

### Scoring Guidance
Scores are weighted to prioritize:
1. **Dynamics preservation** (crest factor, loudness distribution).
2. **Minimal limiting/clipping**.
3. **Minimal NR severity**.
4. **Stereo integrity** and **structural consistency**.
5. **Codec/bitrate** as a tie-breaker.

## Alignment
Reference alignment uses bandpassed mono (300–3000 Hz) cross-correlation with peak interpolation.
Offsets and confidence values are stored per candidate and used for EQ delta maps.
