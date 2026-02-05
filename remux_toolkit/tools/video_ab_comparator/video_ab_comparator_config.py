# remux_toolkit/tools/video_ab_comparator/video_ab_comparator_config.py

DEFAULTS = {
    "source_a_path": "",
    "source_b_path": "",
    # Analysis sampling settings (IMPROVED: 3.75x more coverage)
    "analysis_chunk_count": 60,  # Increased from 8: better detection of issues
    "analysis_chunk_duration": 1.0,  # Reduced from 2.0s: more granular sampling

    # Scoring settings
    "tie_threshold": 0.5,  # Reduced from 2.0: more sensitive winner detection
    "filter_low_information_frames": True,  # Filter credits/black frames from worst frame selection

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

    # Video-Verified Frame Sync Settings (matches Video-Sync-GUI defaults)
    "align_visual_verification": True,  # Enable frame-perfect visual verification
    "align_visual_num_checkpoints": 5,  # Checkpoints at 15%, 30%, 50%, 70%, 85%
    "align_visual_sequence_length": 10,  # Consecutive frames to verify per checkpoint
    "align_visual_candidate_range": 3,  # Search ±N frames around audio correlation
    "align_visual_comparison_method": "hash",  # Comparison method: hash or ssim
    "align_visual_hash_algorithm": "dhash",  # Hash method: dhash/phash/average_hash/whash
    "align_visual_hash_size": 8,  # Hash size (8x8 = 64 bits)
    "align_visual_hash_threshold": 5,  # Max hamming distance per frame
    "align_visual_match_threshold_pct": 70.0,  # % of sequence that must match

    # Frame Mapping Settings
    "use_frame_mapper": True,  # Use VideoTimestamps for frame-perfect mapping
    "use_pyav_seeking": False,  # Use PyAV for frame-accurate seeking (DISABLED: causes black frames)
}
