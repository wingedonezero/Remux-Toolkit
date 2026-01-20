# remux_toolkit/tools/ffmpeg_dvd_gui/ffmpeg_dvd_gui_config.py

DEFAULTS = {
    "output_root": "",  # Will be dynamically set from a default location
    "ffmpeg_path": "ffmpeg",
    "ffprobe_path": "ffprobe",
    "mkvpropedit_path": "mkvpropedit",
    "minlength": 60,  # 1 minute minimum (user preference)
    "enable_preindex": True,  # Use -preindex for accurate chapters
    "trim_padding": True,  # Use -trim option
    "default_region": 0,  # 0 = world/auto
    "chapter_naming": "numbered",  # "numbered" or "unnamed"
    "extra_args": "",
    "preserve_structure": True,
    "col_widths": [],
    "center_split_sizes": [],
    "v_split_sizes": [],
}
