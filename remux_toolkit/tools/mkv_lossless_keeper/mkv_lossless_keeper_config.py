# remux_toolkit/tools/mkv_lossless_keeper/mkv_lossless_keeper_config.py

# Defaults mirror REMOVABLE_CATEGORIES / SUB_CATEGORIES in the core module.
DEFAULTS = {
    "remove_categories": {
        "dts_core": True,
        "dts_hra": True,
        "ac3": True,
        "eac3": True,
        "aac": True,
        "mp3": True,
        "opus": False,
        "vorbis": False,
    },
    "audio_keep_langs": "",
    "sub_remove_categories": {
        "srt": False,
        "ass": False,
        "pgs": False,
        "vobsub": False,
    },
    "sub_keep_langs": "",
    "keep_und": True,
}
