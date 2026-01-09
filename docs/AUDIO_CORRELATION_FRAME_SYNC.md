# Audio-Correlation-Anchored Frame Sync

## Overview

The Audio-Correlation-Anchored Frame Sync feature provides **frame-perfect alignment** for the Video A/B Comparator by combining the speed of audio correlation with the precision of visual frame matching.

This implementation adapts the proven "subtitle-anchored-frame-snap" methodology from Video-Sync-GUI, replacing subtitle anchors with audio correlation anchors.

## How It Works

### Algorithm Flow

1. **Audio Correlation (Fast)**
   - Analyzes audio to get rough offset estimate (~millisecond accuracy)
   - Uses Standard Cross-Correlation (SCC) with 30 chunks across video duration
   - Provides starting point for frame search

2. **Checkpoint Selection**
   - Selects 3 strategic checkpoints at **5%, 50%, 95%** of video duration
   - Avoids first/last 2 minutes (opening/ending credits)
   - Provides verification across entire video

3. **Sliding Window Frame Matching (Per Checkpoint)**
   - Extracts **11-frame window** (center ±5 frames) from source video
   - Computes perceptual hashes (dhash/phash) for all frames
   - Predicts target location using audio offset
   - Searches ±48 frames (~2 seconds at 24fps) around prediction
   - Slides 11-frame window through search range
   - Finds best match using total hash distance

4. **Checkpoint Agreement Verification**
   - Calculates precise offset from each checkpoint
   - Requires all checkpoints to agree within 100ms tolerance
   - Uses median offset if agreement met
   - Reports disagreement if checkpoints don't match

5. **Sub-Frame Precision**
   - Preserves sub-frame timing throughout calculation
   - Only rounds at final application step
   - Achieves frame-perfect accuracy

## Architecture

### Key Components

#### 1. VideoReader Class
```python
class VideoReader:
    """
    Priority order:
    1. VapourSynth + FFMS2 (fastest - persistent index caching, <1ms per frame)
    2. pyffms2 (fast - indexed seeking, but re-indexes each time)
    3. FFmpeg (slow - spawns process per frame)
    """
```

**VapourSynth Benefits:**
- **Persistent Index Caching**: FFMS2 index created once, reused across all operations
- **Frame-Accurate Seeking**: Direct frame number access (no timestamp conversion errors)
- **Thread-Safe**: Multiple workers can share the same index
- **Grayscale Extraction**: Extracts Y (luma) plane only for reliable hashing

#### 2. Perceptual Hashing
```python
def compute_frame_hash(frame: Image.Image, hash_size: int = 8, method: str = 'dhash'):
    """
    Supported methods:
    - dhash: Difference hash (fast, good for similar content)
    - phash: Perceptual hash (slower, more robust)
    - average_hash: Average hash (fastest, less accurate)
    """
```

**Hash Comparison:**
- Uses Hamming distance (number of differing bits)
- Default threshold: 5 bits difference for 64-bit hash (8x8)
- Requires ≥70% of frames in window to match

#### 3. Frame Timing Utilities
```python
def time_to_frame_floor(time_ms: float, fps: float) -> int:
    """Convert timestamp to frame number (which frame is displaying?)"""

def frame_to_time_floor(frame_num: int, fps: float) -> float:
    """Convert frame number to timestamp (when does frame start?)"""
```

**Why Floor Mode:**
- Consistent frame boundaries across operations
- Avoids floating-point rounding errors with NTSC framerates (23.976, 29.97)
- Matches VapourSynth frame indexing behavior

### Integration with Alignment Pipeline

The frame sync integrates seamlessly with the existing audio correlation:

```python
# alignment_advanced.py

def advanced_align(source_a_path, source_b_path, config, ...):
    # 1. Audio correlation (existing)
    audio_offset_sec = perform_scc_correlation(...)

    # 2. Frame sync (NEW)
    if config.visual_verification:
        frame_sync_result = audio_correlation_frame_sync(
            source_a_path,
            source_b_path,
            audio_offset_sec,  # Use audio offset as guide
            fps_a, fps_b,
            num_checkpoints=3,
            window_radius=5,
            search_range_frames=48,
            ...
        )

        if frame_sync_result.success:
            # Use frame-corrected offset
            final_offset_sec = frame_sync_result.offset_sec
            confidence = combine_audio_visual_confidence(...)
```

## Configuration Options

### AlignmentConfig Parameters

```python
@dataclass
class AlignmentConfig:
    # Visual verification (audio-correlation-anchored frame sync)
    visual_verification: bool = True  # Enable frame sync
    visual_num_checkpoints: int = 3  # Number of checkpoints
    visual_window_radius: int = 5  # Frames before/after center (5 = 11 frame window)
    visual_search_range_frames: int = 48  # Search ±N frames (~2s at 24fps)
    visual_hash_size: int = 8  # Hash size (8x8 = 64 bits)
    visual_hash_algorithm: str = "dhash"  # 'dhash', 'phash', 'average_hash'
    visual_hash_threshold: int = 5  # Max hamming distance per frame
    visual_agreement_tolerance_ms: float = 100.0  # Max deviation between checkpoints
```

### Tuning Guidelines

**window_radius** (default: 5)
- Larger = more robust matching (more context)
- Smaller = faster processing
- 5 frames (11 total) works well for most content

**search_range_frames** (default: 48)
- Depends on audio correlation accuracy
- 48 frames = ±2 seconds at 24fps
- Increase if audio correlation is unreliable

**hash_size** (default: 8)
- 8x8 = 64-bit hash (fast, sufficient for most content)
- 16x16 = 256-bit hash (slower, more precise)

**hash_algorithm** (default: 'dhash')
- dhash: Fast, good for similar encodes
- phash: Slower, more robust to scaling/cropping
- average_hash: Fastest, but less accurate

**hash_threshold** (default: 5)
- Lower = stricter matching (may reject valid matches)
- Higher = looser matching (may accept false matches)
- 5 bits for 64-bit hash = ~8% difference tolerance

**agreement_tolerance_ms** (default: 100ms)
- Checkpoints must agree within this tolerance
- ~2-3 frames at 24fps
- Increase if videos have slight drift

## Performance Characteristics

### Speed Comparison

| Method | Speed | Accuracy | Index Creation |
|--------|-------|----------|----------------|
| VapourSynth + FFMS2 | **<1ms/frame** | Frame-perfect | 1-2 min (once) |
| pyffms2 | ~10ms/frame | Frame-perfect | 1-2 min (each run) |
| FFmpeg | ~500ms/frame | Timestamp-based | N/A |

### Typical Runtime

For 3 checkpoints with 11-frame window and ±48 frame search:

- **VapourSynth**: ~3-5 seconds total
  - Index creation: 1-2 minutes (first run only)
  - Frame extraction: <100ms (3 checkpoints × 11 frames × 2 videos)
  - Search: ~3 seconds (3 checkpoints × ~100 positions × 11 frames)

- **FFmpeg fallback**: ~5-10 minutes
  - Frame extraction: ~3 minutes (3300 frame extractions)
  - Search: Same as above

**Recommendation**: Install VapourSynth + FFMS2 for 100x speedup!

## Installation Requirements

### Core Requirements (Always Needed)
```bash
pip install imagehash pillow numpy
```

### Optimal Performance (Highly Recommended)
```bash
# Install VapourSynth (see https://www.vapoursynth.com/)
pip install VapourSynth

# Install FFMS2 plugin for VapourSynth
# - Windows: Install FFMS2 plugin DLL to VapourSynth plugins directory
# - Linux: Install vapoursynth-plugin-ffms2 package
# - macOS: brew install vapoursynth ffms2
```

### Alternative (Good Performance)
```bash
pip install ffms2  # pyffms2 bindings
```

### Fallback (Slow)
- FFmpeg only (no additional install needed)

## Example Results

### Successful Alignment
```
======================================================================
Audio-Correlation-Anchored Frame Sync
======================================================================
Audio offset: -0.083000s (-83.000ms)

Checkpoints: ['120.0s', '600.0s', '1140.0s']

[FrameSync] Checkpoint at 120.00s
[FrameSync]   Source center frame: 2880
[FrameSync]   Predicted target center: frame 2878 (-83.0ms)
[FrameSync]   Best match: target frame 2877
[FrameSync]   Frame adjustment: -1 frames from prediction
[FrameSync]   Match quality: 11/11 frames (100.0%)
[FrameSync]   Precise offset: -125.125ms

[FrameSync] Checkpoint at 600.00s
[FrameSync]   Match quality: 10/11 frames (90.9%)
[FrameSync]   Precise offset: -125.083ms

[FrameSync] Checkpoint at 1140.00s
[FrameSync]   Match quality: 11/11 frames (100.0%)
[FrameSync]   Precise offset: -125.167ms

[FrameSync] ✓ Checkpoints AGREE (within 100.0ms tolerance)
[FrameSync] Final offset: -0.125125s (-125.125ms)
[FrameSync] Confidence: 97%

✓ Frame sync successful!
  Audio offset: -0.083000s
  Frame-corrected offset: -0.125125s
  Correction: -42.125ms (~-1 frame)
  Confidence: 97%
```

### Checkpoint Disagreement
```
[FrameSync] ✗ Checkpoints DISAGREE (spread 208.3ms > 100.0ms)
[FrameSync] This may indicate different cuts or timing drift

✗ Frame sync failed: Checkpoints disagree: max deviation 208.3ms
  Keeping audio-only offset: -0.083000s
```

## Use Cases

### When Frame Sync Helps

1. **Different Encodes**
   - Same source, different encoders
   - Different frame drop/duplicate patterns
   - Requires frame-perfect alignment

2. **IVTC/Decimation Differences**
   - One video properly IVTC'd, other not
   - Different decimation strategies
   - Frame positions don't match mathematically

3. **High-Precision Requirements**
   - Artifact detection needs exact frame alignment
   - Comparing compressor artifacts
   - Detecting duplicate/dropped frames

### When Audio-Only Is Sufficient

1. **Same Encode, Different Containers**
   - Just remuxed, no re-encode
   - Frame positions identical
   - Audio correlation sufficient

2. **Different Audio Only**
   - Video streams identical
   - Only audio tracks differ
   - No need for visual verification

3. **Performance Priority**
   - Fast preview comparison
   - Don't need frame-perfect accuracy

## Limitations

1. **Different Cuts**
   - If videos have different scene cuts, checkpoints will disagree
   - Frame sync will report failure and fall back to audio offset

2. **Heavy Filtering**
   - If one video heavily processed (extreme DNR, sharpening, etc.)
   - Perceptual hashing may fail to match
   - Increase hash_threshold or use phash instead of dhash

3. **Variable Frame Rate (VFR)**
   - Currently assumes CFR (constant frame rate)
   - May have issues with VFR content
   - Future enhancement: integrate VideoTimestamps library

4. **Telecine Patterns**
   - If videos have different telecine patterns (hard vs soft)
   - Frame matching may be inconsistent
   - May need special handling

## Troubleshooting

### "No checkpoints matched successfully"
- Videos may be too different (different sources, cuts, etc.)
- Try increasing `visual_hash_threshold`
- Try using 'phash' instead of 'dhash'
- Check if videos are actually related

### "Checkpoints disagree"
- Videos may have different scene cuts
- Videos may have timing drift
- Increase `visual_agreement_tolerance_ms`
- Check if videos are from same source

### "VapourSynth init failed"
- VapourSynth not installed or FFMS2 plugin missing
- Will fall back to pyffms2 or FFmpeg (slower)
- Install VapourSynth + FFMS2 for best performance

### Slow performance
- Install VapourSynth + FFMS2 for 100x speedup
- Reduce `visual_search_range_frames` (if audio correlation is accurate)
- Reduce `visual_num_checkpoints` to 2 (less verification)

## Technical References

### Adapted From
- **Video-Sync-GUI**: [subtitle-anchored-frame-snap mode](https://github.com/wingedonezero/Video-Sync-GUI)
  - `vsg_core/subtitles/frame_sync.py`: Core frame matching logic
  - `vsg_core/subtitles/frame_matching.py`: VideoReader and hashing

### Key Adaptations
1. **Replaced Subtitle Anchors → Audio Correlation Anchors**
   - Original: Uses subtitle positions as known reference points
   - Adapted: Uses audio correlation to predict reference points

2. **Checkpoint Selection Strategy**
   - Original: Selects subtitles at 1/6, 1/2, 5/6 of subtitle duration
   - Adapted: Selects checkpoints at 5%, 50%, 95% of video duration

3. **Search Window Centering**
   - Original: Centers on subtitle position
   - Adapted: Centers on audio-predicted position

4. **Integration Point**
   - Original: Applies offset to subtitle events
   - Adapted: Returns offset for frame mapper integration

## Future Enhancements

1. **VFR Support**
   - Integrate VideoTimestamps library
   - Handle variable frame rate videos
   - Map between different VFR patterns

2. **Adaptive Search Range**
   - Start with small search range
   - Expand if no match found
   - Optimize performance for accurate audio correlation

3. **Multi-Resolution Hashing**
   - Try multiple hash sizes
   - Start with larger hash (more tolerant), refine with smaller
   - Better handling of filtered content

4. **Scene Change Detection**
   - Use scene changes as natural checkpoints
   - More reliable than arbitrary time positions
   - Better for videos with irregular content distribution

5. **Drift Detection**
   - Detect if offset changes across video (PAL speedup, etc.)
   - Apply drift correction
   - Better handling of speed-altered content

## License

This implementation is part of Remux-Toolkit and follows the same license.

The core algorithm is adapted from Video-Sync-GUI's subtitle-anchored-frame-snap methodology.
