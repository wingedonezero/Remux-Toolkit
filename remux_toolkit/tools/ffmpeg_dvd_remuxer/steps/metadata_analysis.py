# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/metadata_analysis.py
import json
import subprocess
from pathlib import Path
from ..utils.helpers import run_capture

class MetadataAnalysisStep:
    """Deep metadata analysis step that captures all stream metadata for accurate remuxing."""

    def __init__(self, config):
        self.config = config

    def run(self, context: dict, log_emitter, stop_event) -> bool:
        """Analyze and save comprehensive metadata for the title."""
        step_info = context.get('step_info', '[STEP]')
        log_emitter(f"{step_info} Analyzing complete metadata...")

        input_path = context['input_path']
        title_num = context['title_num']
        out_folder = context['out_folder']

        # Create metadata file path
        metadata_file = out_folder / f"title_{title_num}_metadata.json"
        context['metadata_file'] = metadata_file

        # Get comprehensive ffprobe data with extra details
        ffprobe_cmd = [
            "ffprobe", "-v", "error",
            "-preindex", "1",  # Critical for accurate chapter markers and duration
            "-f", "dvdvideo", "-title", str(title_num),
            "-show_streams",
            "-show_format",
            "-show_chapters",
            "-show_data",  # Include packet data for more details
            "-print_format", "json",
            str(input_path)
        ]

        rc, probe_out = run_capture(ffprobe_cmd)
        if rc != 0:
            log_emitter(f"!! ERROR: Failed to probe metadata: {probe_out}")
            return False

        try:
            probe_data = json.loads(probe_out)
        except json.JSONDecodeError as e:
            log_emitter(f"!! ERROR: Invalid probe JSON: {e}")
            return False

        # Parse and enhance metadata
        metadata = {
            "source": str(input_path),
            "title_num": title_num,
            "probe_data": probe_data,
            "streams": [],
            "mkv_mapping": {}
        }

        # Process each stream
        for stream in probe_data.get("streams", []):
            stream_idx = stream.get("index", -1)
            stream_type = stream.get("codec_type", "unknown")

            stream_meta = {
                "index": stream_idx,
                "type": stream_type,
                "codec": stream.get("codec_name"),
                "codec_long": stream.get("codec_long_name"),

                # Timing information - CRITICAL for sync
                "start_pts": stream.get("start_pts", 0),
                "start_time": stream.get("start_time", "0"),
                "delay_ms": self._pts_to_ms(stream.get("start_pts", 0), stream.get("time_base", "1/90000")),

                # Stream-specific metadata
                "language": stream.get("tags", {}).get("language", "und"),
                "disposition": stream.get("disposition", {}),
            }

            # Video-specific metadata
            if stream_type == "video":
                stream_meta.update({
                    "width": stream.get("width"),
                    "height": stream.get("height"),
                    "aspect_ratio": stream.get("display_aspect_ratio"),
                    "pixel_aspect": stream.get("sample_aspect_ratio"),
                    "frame_rate": stream.get("r_frame_rate"),
                    "field_order": stream.get("field_order"),
                    "color_space": stream.get("pix_fmt"),
                    "color_range": stream.get("color_range"),
                    "gop_size": stream.get("gop_size"),
                    "has_b_frames": stream.get("has_b_frames"),
                    "level": stream.get("level"),
                })

                # Determine extraction format
                if stream.get("codec_name") == "mpeg2video":
                    stream_meta["extract_extension"] = ".m2v"
                    stream_meta["extract_codec"] = "copy"
                elif stream.get("codec_name") == "h264":
                    stream_meta["extract_extension"] = ".264"
                    stream_meta["extract_codec"] = "copy"
                else:
                    stream_meta["extract_extension"] = ".mkv"
                    stream_meta["extract_codec"] = "copy"

            # Audio-specific metadata
            elif stream_type == "audio":
                stream_meta.update({
                    "sample_rate": stream.get("sample_rate"),
                    "channels": stream.get("channels"),
                    "channel_layout": stream.get("channel_layout"),
                    "bit_rate": stream.get("bit_rate"),
                    "bits_per_sample": stream.get("bits_per_sample"),
                })

                # Determine extraction format based on codec
                codec = stream.get("codec_name", "").lower()
                if codec == "ac3":
                    stream_meta["extract_extension"] = ".ac3"
                elif codec == "eac3":
                    stream_meta["extract_extension"] = ".eac3"
                elif codec == "dts":
                    stream_meta["extract_extension"] = ".dts"
                elif codec == "mp2":
                    stream_meta["extract_extension"] = ".mp2"
                elif codec == "pcm_s16le" or "pcm" in codec:
                    stream_meta["extract_extension"] = ".wav"
                else:
                    stream_meta["extract_extension"] = ".mka"
                stream_meta["extract_codec"] = "copy"

            # Subtitle-specific metadata
            elif stream_type == "subtitle":
                stream_meta.update({
                    "forced": stream.get("disposition", {}).get("forced", 0),
                    "codec_private": stream.get("codec_private"),
                })

                codec = stream.get("codec_name", "").lower()
                if codec in ["dvd_subtitle", "dvdsub"]:
                    stream_meta["extract_extension"] = ".sup"
                elif codec == "hdmv_pgs_subtitle":
                    stream_meta["extract_extension"] = ".sup"
                else:
                    stream_meta["extract_extension"] = ".srt"
                stream_meta["extract_codec"] = "copy"

            metadata["streams"].append(stream_meta)

            # Create MKV mapping for this stream
            self._create_mkv_mapping(stream_meta, metadata["mkv_mapping"])

        # Process chapters
        metadata["chapters"] = self._process_chapters(probe_data.get("chapters", []))

        # Save metadata to JSON
        try:
            with open(metadata_file, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)
            log_emitter(f"  -> Metadata saved to {metadata_file.name}")
        except IOError as e:
            log_emitter(f"!! ERROR: Failed to save metadata: {e}")
            return False

        # Store in context for other steps
        context['title_metadata'] = metadata

        # Log summary
        log_emitter(f"  -> Found {len(metadata['streams'])} streams:")
        for s in metadata["streams"]:
            delay_info = f" (delay: {s['delay_ms']}ms)" if s['delay_ms'] else ""
            log_emitter(f"     Stream #{s['index']}: {s['type']} [{s['codec']}] {s['language']}{delay_info}")

        return True

    def _pts_to_ms(self, pts, time_base_str):
        """Convert PTS to milliseconds."""
        if not pts:
            return 0
        try:
            # Parse time_base (e.g., "1/90000")
            num, denom = map(int, time_base_str.split('/'))
            seconds = pts * num / denom
            return int(seconds * 1000)
        except:
            return 0

    def _process_chapters(self, chapters):
        """Process chapter data for MKV."""
        processed = []
        for i, chap in enumerate(chapters):
            processed.append({
                "number": i + 1,
                "start_time": chap.get("start_time", "0"),
                "end_time": chap.get("end_time", "0"),
                "start_pts": chap.get("start", 0),
                "end_pts": chap.get("end", 0),
                "title": f"Chapter {i+1:02d}"  # Will be overridden by chapters step if needed
            })
        return processed

    def _create_mkv_mapping(self, stream_meta, mkv_map):
        """Create MKV muxing parameters for this stream."""
        stream_idx = stream_meta['index']

        mkv_params = {
            "type": stream_meta['type'],
            "source_index": stream_idx,
            "language": stream_meta['language'],
            "delay_ms": stream_meta['delay_ms'],
            "mkvmerge_options": []
        }

        # Add delay if present
        if stream_meta['delay_ms']:
            mkv_params["mkvmerge_options"].append(f"--sync 0:{stream_meta['delay_ms']}")

        # Language tag
        if stream_meta['language'] != 'und':
            mkv_params["mkvmerge_options"].append(f"--language 0:{stream_meta['language']}")

        # Video-specific MKV settings
        if stream_meta['type'] == 'video':
            if stream_meta.get('field_order'):
                field_map = {
                    'tt': '1',  # top field first
                    'bb': '2',  # bottom field first
                    'tb': '1',  # top field first (alternative notation)
                    'bt': '2',  # bottom field first (alternative notation)
                }
                if stream_meta['field_order'] in field_map:
                    mkv_params["mkvmerge_options"].append(f"--field-order 0:{field_map[stream_meta['field_order']]}")

            if stream_meta.get('aspect_ratio'):
                mkv_params["mkvmerge_options"].append(f"--display-dimensions 0:{stream_meta['aspect_ratio']}")

        # Audio-specific MKV settings
        elif stream_meta['type'] == 'audio':
            # Track name based on channel layout
            layout = stream_meta.get('channel_layout', '')
            if '5.1' in layout or stream_meta.get('channels') == 6:
                mkv_params["track_name"] = "5.1 Surround"
            elif 'stereo' in layout or stream_meta.get('channels') == 2:
                mkv_params["track_name"] = "Stereo"

            if mkv_params.get("track_name"):
                mkv_params["mkvmerge_options"].append(f"--track-name 0:{mkv_params['track_name']}")

        # Subtitle-specific settings
        elif stream_meta['type'] == 'subtitle':
            if stream_meta.get('forced'):
                mkv_params["mkvmerge_options"].append("--forced-display-flag 0:yes")

        # Default flag (first audio/video track should be default)
        if stream_idx == 0 or (stream_meta['type'] == 'audio' and stream_idx == 1):
            mkv_params["mkvmerge_options"].append("--default-track-flag 0:yes")

        mkv_map[stream_idx] = mkv_params

        return mkv_params
