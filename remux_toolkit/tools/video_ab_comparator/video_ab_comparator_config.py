# remux_toolkit/tools/video_ab_comparator/video_ab_comparator_config.py

# Weighted Scoring Presets for Anime
# These weights determine how much each detector contributes to the final verdict
# Higher weight = more important for determining the winner
ANIME_PRESETS = {
    # BD vs BD comparison (standard anime quality comparison)
    "anime_bd_vs_bd": {
        # Critical - Most visible quality issues in anime (weight 3.0)
        "Compression Artifacts": 3.0,      # Blocking/mosquito noise destroys flat colors and line art
        "Color Banding": 3.0,              # Extremely visible in anime's smooth gradients
        "Upscaled Video": 3.0,             # Native resolution matters for BD vs BD

        # Important - Significant quality factors (weight 2.0)
        "Ringing / Halos": 2.0,            # Very visible on line art and sharp edges
        "Audio Analysis": 2.0,             # PAL speedup and quality issues critical
        "Ghosting / Blending": 2.0,        # IVTC issues common in anime releases
        "Excessive Sharpening": 2.0,       # Destroys line art quality

        # Normal - Less critical but still important (weight 1.0)
        "Aspect Ratio": 1.0,               # Important but usually obvious
        "Dot Crawl": 1.0,                  # Less common in modern releases
        "Chroma Shift": 1.0,               # Less visible in anime than live action
        "Rainbowing / Cross-Color": 1.0,  # Less common in modern sources
        "Color Cast": 1.0,                 # Can be stylistic choice
        "Over-DNR / Waxiness": 1.0,        # Less critical than compression
        "Interlace Combing": 1.0,          # Combined metric, less critical
        "Cadence Irregularity": 1.0,       # Combined metric, less critical
    },

    # DVD vs BD comparison (upscaling is expected, focus on encoding quality)
    "anime_dvd_vs_bd": {
        # Critical - Encoding quality matters more than resolution (weight 3.0)
        "Compression Artifacts": 3.5,      # DVD compression often worse, weight higher
        "Color Banding": 3.5,              # Critical for distinguishing good DVD from bad BD

        # Important - But upscaling is expected (weight 2.0)
        "Upscaled Video": 1.5,             # Lower weight - BD might be upscaled DVD source
        "Ringing / Halos": 2.5,            # Filtering artifacts more important
        "Audio Analysis": 2.5,             # Audio quality often better on BD
        "Ghosting / Blending": 2.5,        # IVTC quality critical
        "Excessive Sharpening": 2.0,       # BD might over-sharpen DVD source
        "Over-DNR / Waxiness": 2.0,        # BD might over-filter DVD source

        # Normal (weight 1.0)
        "Aspect Ratio": 1.0,
        "Dot Crawl": 1.5,                  # More common in DVD sources
        "Chroma Shift": 1.0,
        "Rainbowing / Cross-Color": 1.5,  # More common in DVD sources
        "Color Cast": 1.0,
        "Interlace Combing": 1.5,          # More relevant for DVD sources
        "Cadence Irregularity": 1.5,
    },

    # DVD vs DVD comparison (focus on encoding quality and filtering)
    "anime_dvd_vs_dvd": {
        # Critical - Compression and filtering quality (weight 3.0)
        "Compression Artifacts": 3.5,      # Most important for DVD vs DVD
        "Color Banding": 3.0,              # DVD often has banding issues

        # Important (weight 2.0)
        "Ringing / Halos": 2.5,            # Common filtering issue
        "Audio Analysis": 2.0,             # Audio quality and PAL speedup
        "Ghosting / Blending": 2.5,        # IVTC quality varies
        "Excessive Sharpening": 2.0,
        "Over-DNR / Waxiness": 2.0,        # Common DVD filtering issue
        "Interlace Combing": 2.0,          # More relevant for DVD
        "Cadence Irregularity": 2.0,

        # Normal (weight 1.0)
        "Upscaled Video": 0.5,             # Both are SD, less relevant
        "Aspect Ratio": 1.0,
        "Dot Crawl": 1.5,                  # More common in DVD
        "Chroma Shift": 1.5,               # More common in DVD
        "Rainbowing / Cross-Color": 1.5,  # More common in DVD
        "Color Cast": 1.0,
    },

    # Balanced preset (all detectors weighted equally - old behavior)
    "balanced": {
        "Compression Artifacts": 1.0,
        "Color Banding": 1.0,
        "Upscaled Video": 1.0,
        "Ringing / Halos": 1.0,
        "Audio Analysis": 1.0,
        "Ghosting / Blending": 1.0,
        "Excessive Sharpening": 1.0,
        "Over-DNR / Waxiness": 1.0,
        "Aspect Ratio": 1.0,
        "Dot Crawl": 1.0,
        "Chroma Shift": 1.0,
        "Rainbowing / Cross-Color": 1.0,
        "Color Cast": 1.0,
        "Interlace Combing": 1.0,
        "Cadence Irregularity": 1.0,
    }
}

DEFAULTS = {
    "source_a_path": "",
    "source_b_path": "",
    # Analysis sampling settings (IMPROVED: 3.75x more coverage)
    "analysis_chunk_count": 60,  # Increased from 8: better detection of issues
    "analysis_chunk_duration": 1.0,  # Reduced from 2.0s: more granular sampling

    # Scoring settings
    "tie_threshold": 0.5,  # Reduced from 2.0: more sensitive winner detection
    "filter_low_information_frames": True,  # Filter credits/black frames from worst frame selection

    # Weighted scoring for anime (NEW)
    "enable_weighted_scoring": True,  # Use weighted scoring instead of simple majority vote
    "scoring_preset": "anime_bd_vs_bd",  # Preset to use: anime_bd_vs_bd, anime_dvd_vs_bd, anime_dvd_vs_dvd, balanced
    "show_detailed_breakdown": True,  # Show per-detector weight contribution in results

    # Custom detector weights (used when scoring_preset is "custom")
    # Format: {"Detector Name": weight} - set to override preset
    "custom_detector_weights": {},

    "enable_audio_analysis": True,
    "enable_interlace_detection": True,
    "enable_cadence_detection": True,

    # Advanced Audio Alignment Settings
    "use_advanced_alignment": False,  # Enable advanced SCC-based alignment
    "align_chunk_count": 30,  # Number of audio chunks to analyze
    "align_chunk_duration": 30.0,  # Duration of each audio chunk in seconds
    "align_scan_start_pct": 5.0,  # Start scanning at 5% into video
    "align_scan_end_pct": 95.0,  # End scanning at 95% into video
    "align_min_match_pct": 20.0,  # Minimum match percentage to accept chunk
    "align_target_confidence_pct": 70.0,  # Target confidence percentage
    "align_sample_rate": 48000,  # Audio sample rate for correlation
    "align_use_soxr": True,  # Use high-quality SOXR resampling
    "align_peak_fit": True,  # Enable sub-sample peak fitting
    "align_delay_selection": "first",  # Delay selection strategy: first/median/mean
    "align_audio_lang": "jpn",  # Audio language to use for alignment (None = first track)

    # Audio-Correlation-Anchored Frame Sync Settings (NEW)
    "align_visual_verification": True,  # Enable frame-perfect visual verification
    "align_visual_num_checkpoints": 3,  # Number of checkpoints (at 5%, 50%, 95%)
    "align_visual_window_radius": 5,  # Frames before/after center (5 = 11 frame window)
    "align_visual_search_frames": 48,  # Search ±N frames around audio offset (~2s at 24fps)
    "align_visual_hash_size": 8,  # Hash size (8x8 = 64 bits)
    "align_visual_hash_algorithm": "dhash",  # Hash method: dhash/phash/average_hash
    "align_visual_hash_threshold": 5,  # Max hamming distance per frame
    "align_visual_agreement_tolerance_ms": 100.0,  # Max deviation between checkpoints

    # Frame Mapping Settings
    "use_frame_mapper": True,  # Use VideoTimestamps for frame-perfect mapping
    "use_pyav_seeking": False,  # Use PyAV for frame-accurate seeking (DISABLED: causes black frames)
}
