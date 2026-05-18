# remux_toolkit/tools/ffmpeg_dvd_gui/ffmpeg_dvd_gui_config.py

DEFAULTS = {
    "output_root": "",  # Will be dynamically set from a default location
    "ffmpeg_path": "ffmpeg",
    "ffprobe_path": "ffprobe",
    "mkvpropedit_path": "mkvpropedit",
    "minlength": 120,  # 2 minutes — matches MakeMKV's default auto-skip threshold
    "use_native_probe": True,  # libdvdread+analyzer backend; False = old ffprobe-dvdvideo path
    "enable_preindex": True,  # Use -preindex for accurate chapters
    "trim_padding": True,  # Use -trim option
    "default_region": 0,  # 0 = world/auto
    "chapter_naming": "numbered",  # "numbered" or "unnamed"
    "max_muxing_queue_size": False,  # -max_muxing_queue_size 9999
    "avoid_negative_ts": False,  # -avoid_negative_ts make_zero
    "extra_args": "",
    "preserve_structure": True,
    # Native muxer (libmkv_shim) — opt-in until cross-validated against
    # MakeMKV on the corpus. Default False so existing flow is unchanged.
    "use_native_remux": False,
    "apply_trim": False,                   # cell-trim deciders (Step F port)
    "include_subpictures": True,           # subs in native path
    "include_closed_captions": True,       # EIA-608 via ccextractor
    "col_widths": [],
    "center_split_sizes": [],
    "v_split_sizes": [],
}
