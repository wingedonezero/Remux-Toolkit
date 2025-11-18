"""
Configuration for Video Sync Tool
"""

DEFAULTS = {
    'reference_file': '',
    'target_files': [],
    'output_directory': '',
    'output_filename': 'aligned_output.mkv',
    'audio_language': 'jpn',  # Default to Japanese
    'correlation_threshold': 0.7,  # Minimum confidence for segment match
    'chunk_duration_sec': 5.0,  # Chunk size for coarse analysis
    'fine_precision_ms': 100,  # Precision for fine alignment at boundaries
    'min_segment_duration_sec': 10.0,  # Minimum segment length to keep
    'max_offset_ms': 500,  # Maximum expected timing offset between parts
    'trim_end_buffer_sec': 2.6,  # Seconds to trim from end of segments (17:40 → 17:37.4)
    'trim_start_buffer_sec': 0.9,  # Seconds to trim from start of segments (01:35 → 01:34.1)
}
