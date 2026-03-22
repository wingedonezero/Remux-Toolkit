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

    # Neural Frame Matching (ISC model) Settings
    "align_visual_verification": True,  # Enable ISC neural frame matching
    "align_neural_num_positions": 3,  # Test positions across video (at 20%, 50%, 80%)
    "align_neural_window_seconds": 10,  # Duration of frame window per position
    "align_neural_slide_range_seconds": 5,  # ±N seconds sliding range
    "align_neural_batch_size": 32,  # GPU batch size for ISC feature extraction

    # Frame Mapping Settings
    "use_frame_mapper": True,  # Use VideoTimestamps for frame-perfect mapping
    "use_pyav_seeking": False,  # Use PyAV for frame-accurate seeking (DISABLED: causes black frames)
}
