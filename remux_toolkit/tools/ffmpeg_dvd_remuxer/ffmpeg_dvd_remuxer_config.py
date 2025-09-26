# remux_toolkit/tools/ffmpeg_dvd_remuxer/ffmpeg_dvd_remuxer_config.py
DEFAULTS = {
    "default_output_directory": "",
    "minimum_title_length": 120,

    # Processing options
    "remove_eia_608": True,
    "run_ccextractor": True,
    "ffmpeg_trim_padding": True,
    "keep_metadata_json": False,  # Keep JSON files for debugging timing
    "keep_temp_files": False,      # Keep all temp files for debugging

    # Track naming options
    "audio_track_names": True,    # Include descriptive audio track names
    "subtitle_track_names": True, # Include descriptive subtitle track names
    "cc_track_names": True,       # Include descriptive CC track name

    # Timing method
    "timing_method": "auto",       # auto, pgc, pts, ffprobe

    # Error handling
    "auto_fix_sync": True,         # Automatically fix small sync issues
    "auto_fix_chapters": True,     # Automatically fix chapter issues
    "skip_damaged_sectors": True,  # Skip unreadable sectors
    "continue_on_error": False,    # Continue processing despite non-critical errors

    # Telecine detection options
    "telecine_detection_mode": "disabled",  # disabled, auto, force_progressive, force_interlaced
    "telecine_threshold": 85,     # Percentage threshold for progressive detection
    "telecine_sample_duration": 60,  # Seconds to sample for telecine detection
}
