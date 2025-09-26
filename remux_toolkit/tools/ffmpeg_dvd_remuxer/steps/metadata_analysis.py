# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/metadata_analysis.py
import json
import subprocess
from pathlib import Path
from ..utils.helpers import run_capture

class MetadataAnalysisStep:
    """Deep metadata analysis step that merges IFO and ffprobe data for accurate remuxing."""

    def __init__(self, config):
        self.config = config

    def run(self, context: dict, log_emitter, stop_event) -> bool:
        """Analyze and save comprehensive metadata for the title."""
        step_info = context.get('step_info', '[STEP]')
        log_emitter(f"{step_info} Analyzing complete metadata...")

        input_path = context['input_path']
        title_num = context['title_num']
        out_folder = context['out_folder']

        # Get IFO data from previous step
        ifo_data = context.get('ifo_data', {})
        ifo_audio = ifo_data.get('audio_tracks', [])
        ifo_subs = ifo_data.get('subtitle_tracks', [])
        ifo_chapters = ifo_data.get('chapters', [])
        ifo_cells = ifo_data.get('cell_data', [])

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

        # Calculate relative delays from PTS with video as reference
        stream_delays = self._calculate_relative_delays(probe_data)

        # Parse and enhance metadata
        metadata = {
            "source": str(input_path),
            "title_num": title_num,
            "probe_data": probe_data,
            "streams": [],
            "mkv_mapping": {},
            "ifo_data": ifo_data,  # Store raw IFO data for reference
            "keep_metadata_json": self.config.get("keep_metadata_json", False)
        }

        # Track audio/subtitle indices separately for IFO matching
        audio_idx = 0
        sub_idx = 0

        # Process each stream
        for stream_idx, stream in enumerate(probe_data.get("streams", [])):
            stream_type = stream.get("codec_type", "unknown")

            stream_meta = {
                "index": stream.get("index", stream_idx),
                "type": stream_type,
                "codec": stream.get("codec_name"),
                "codec_long": stream.get("codec_long_name"),

                # Timing information - CRITICAL for sync
                "start_pts": stream.get("start_pts", 0),
                "start_time": stream.get("start_time", "0"),
                "delay_ms": stream_delays.get(stream_idx, 0),
                "time_base": stream.get("time_base", "1/90000"),

                # Default values (will be enriched from IFO)
                "language": stream.get("tags", {}).get("language", "und"),
                "disposition": stream.get("disposition", {}),
            }

            # Video-specific metadata
            if stream_type == "video":
                # Use IFO aspect ratio if available
                if ifo_data.get('aspect_ratio'):
                    stream_meta["aspect_ratio"] = ifo_data['aspect_ratio']
                    log_emitter(f"  -> Using IFO aspect ratio: {ifo_data['aspect_ratio']}")
                else:
                    # Parse ffprobe aspect ratio
                    dar = stream.get("display_aspect_ratio", "")
                    if dar and ':' in dar:
                        stream_meta["aspect_ratio"] = dar
                    else:
                        # Calculate from dimensions
                        width = stream.get("width", 720)
                        height = stream.get("height", 480)
                        stream_meta["aspect_ratio"] = f"{width}:{height}"

                # Video format from IFO
                stream_meta["video_format"] = ifo_data.get('video_format', 'NTSC')

                # Set video language to first audio track's language if available
                if ifo_audio and len(ifo_audio) > 0:
                    stream_meta["language"] = ifo_audio[0].get('language', 'und')

                stream_meta.update({
                    "width": stream.get("width"),
                    "height": stream.get("height"),
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
                elif stream.get("codec_name") == "h264":
                    stream_meta["extract_extension"] = ".264"
                else:
                    stream_meta["extract_extension"] = ".mkv"

            # Audio-specific metadata - MERGE WITH IFO DATA
            elif stream_type == "audio":
                # Store the calculated delay for this specific audio stream
                stream_meta["delay_ms"] = stream_delays.get(stream_idx, 0)

                # Match with IFO audio track data
                if audio_idx < len(ifo_audio):
                    ifo_track = ifo_audio[audio_idx]

                    # Only override with IFO data if it's not 'und' or empty
                    ifo_lang = ifo_track.get('language', 'und')
                    ffprobe_lang = stream_meta["language"]

                    # Prefer ffprobe language if IFO doesn't have it
                    if ifo_lang != 'und':
                        stream_meta["language"] = ifo_lang
                    elif ffprobe_lang == 'und' and audio_idx == 0:
                        # First audio track often defaults to source video language
                        stream_meta["language"] = 'eng'  # Common default

                    # Add IFO-specific metadata
                    stream_meta["ifo_format"] = ifo_track.get('format')
                    stream_meta["ifo_channels"] = ifo_track.get('channels')
                    stream_meta["ifo_sample_rate"] = ifo_track.get('sample_rate')
                    stream_meta["content_type"] = ifo_track.get('content', 'Normal')

                    # Only use track name if enabled in config
                    if self.config.get("audio_track_names", True):
                        stream_meta["track_name"] = ifo_track.get('name', '')
                    else:
                        stream_meta["track_name"] = ''

                    # Track delay source
                    if stream_meta["delay_ms"] > 0:
                        stream_meta["delay_source"] = "pts"
                    else:
                        stream_meta["delay_source"] = "none"

                    log_emitter(f"  -> Enriched audio #{audio_idx} with IFO: {stream_meta.get('track_name', 'no name')}")
                else:
                    # No IFO data for this track, use ffprobe data only
                    stream_meta["track_name"] = ''
                    if stream_meta["delay_ms"] > 0:
                        stream_meta["delay_source"] = "pts"
                    else:
                        stream_meta["delay_source"] = "none"

                audio_idx += 1

                # FFprobe audio metadata
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
                elif codec in ["mp2", "mp3"]:
                    stream_meta["extract_extension"] = ".mp2"
                elif codec == "pcm_s16le" or "pcm" in codec:
                    stream_meta["extract_extension"] = ".wav"
                else:
                    stream_meta["extract_extension"] = ".mka"

            # Subtitle-specific metadata - MERGE WITH IFO DATA
            elif stream_type == "subtitle":
                # Match with IFO subtitle track data
                if sub_idx < len(ifo_subs):
                    ifo_track = ifo_subs[sub_idx]

                    # Merge IFO data
                    stream_meta["language"] = ifo_track.get('language', stream_meta["language"])
                    stream_meta["content_type"] = ifo_track.get('content', 'Normal')
                    stream_meta["forced"] = ifo_track.get('forced', False)
                    stream_meta["track_name"] = ifo_track.get('name', '')

                    log_emitter(f"  -> Enriched subtitle #{sub_idx} with IFO: {stream_meta['track_name']}")

                sub_idx += 1

                # Update forced flag from ffprobe if not set by IFO
                if not stream_meta.get("forced"):
                    stream_meta["forced"] = stream.get("disposition", {}).get("forced", 0)

                stream_meta["codec_private"] = stream.get("codec_private")

                # DVD subtitles need the palette from IFO
                codec = stream.get("codec_name", "").lower()
                if codec in ["dvd_subtitle", "dvdsub"]:
                    stream_meta["extract_extension"] = ".sup"
                    if ifo_data.get('palette'):
                        stream_meta["palette"] = ifo_data['palette']
                        log_emitter(f"  -> Added palette data for DVD subtitle")
                elif codec == "hdmv_pgs_subtitle":
                    stream_meta["extract_extension"] = ".sup"
                else:
                    stream_meta["extract_extension"] = ".srt"

            metadata["streams"].append(stream_meta)

            # Create MKV mapping for this stream
            self._create_mkv_mapping(stream_meta, metadata["mkv_mapping"])

        # Process chapters - prefer IFO chapters if available and reasonable
        if ifo_chapters:
            # Validate IFO chapters have reasonable timing
            max_time = max((c.get('end_time', 0) for c in ifo_chapters), default=0)
            if max_time > 0 and max_time < 36000:  # Less than 10 hours
                metadata["chapters"] = self._process_ifo_chapters(ifo_chapters)
                log_emitter(f"  -> Using {len(ifo_chapters)} chapters from IFO data")
            else:
                # Fall back to ffprobe if IFO chapters seem wrong
                metadata["chapters"] = self._process_ffprobe_chapters(probe_data.get("chapters", []))
                if metadata["chapters"]:
                    log_emitter(f"  -> Using {len(metadata['chapters'])} chapters from ffprobe (IFO chapters had invalid timing)")
        else:
            metadata["chapters"] = self._process_ffprobe_chapters(probe_data.get("chapters", []))
            if metadata["chapters"]:
                log_emitter(f"  -> Using {len(metadata['chapters'])} chapters from ffprobe")

        # Add cell timing data if available and reasonable
        if ifo_cells:
            # Validate cell timing
            total_cell_time = sum(c.get('playback_time', 0) for c in ifo_cells)
            if total_cell_time > 0 and total_cell_time < 36000:  # Less than 10 hours
                metadata["cells"] = ifo_cells
                log_emitter(f"  -> Found {len(ifo_cells)} cells with total time: {total_cell_time:.2f}s")
            else:
                log_emitter(f"  -> Ignoring {len(ifo_cells)} cells with invalid timing")
                metadata["cells"] = []
        else:
            metadata["cells"] = []

        # Add additional IFO metadata
        metadata["angles"] = ifo_data.get("angles", 1)
        metadata["video_format"] = ifo_data.get("video_format", "NTSC")

        # Save metadata to JSON
        try:
            with open(metadata_file, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)
            log_emitter(f"  -> Metadata saved to {metadata_file.name}")

            # Store whether to keep metadata file after processing
            context['keep_metadata_json'] = self.config.get("keep_metadata_json", False)
        except IOError as e:
            log_emitter(f"!! ERROR: Failed to save metadata: {e}")
            return Falseemitter(f"!! ERROR: Failed to save metadata: {e}")
            return False

        # Store in context for other steps
        context['title_metadata'] = metadata

        # Log summary
        log_emitter(f"  -> Found {len(metadata['streams'])} streams:")
        for s in metadata["streams"]:
            # Format delay info
            delay_info = ""
            if s.get('delay_ms') and s['delay_ms'] > 0:
                source = s.get('delay_source', 'unknown')
                delay_info = f" (delay: {s['delay_ms']}ms from {source})"

            # Format track name
            name_info = f" [{s.get('track_name', '')}]" if s.get('track_name') else ""

            # Show language only if it's not 'und'
            lang = s['language'] if s['language'] != 'und' else ''

            log_emitter(f"     Stream #{s['index']}: {s['type']} [{s['codec']}] {lang}{name_info}{delay_info}")

        return True

    def _calculate_relative_delays(self, probe_data):
        """Calculate relative delays with video as reference (PTS 0).
        Returns dict of stream_index -> delay_ms
        """
        delays = {}
        video_pts = None
        video_timebase = None

        # First find the video stream's PTS as reference
        for stream in probe_data.get("streams", []):
            if stream.get("codec_type") == "video":
                video_pts = int(stream.get("start_pts", 0))
                video_timebase = stream.get("time_base", "1/90000")
                break

        # Calculate all stream delays relative to video
        for stream in probe_data.get("streams", []):
            stream_idx = stream.get("index", 0)
            stream_type = stream.get("codec_type")

            if stream_type == "video":
                # Video is always the reference, delay = 0
                delays[stream_idx] = 0
            else:
                # Calculate relative delay from video
                stream_pts = int(stream.get("start_pts", 0))
                stream_timebase = stream.get("time_base", video_timebase)

                # Convert both to milliseconds
                video_ms = self._pts_to_ms(video_pts, video_timebase)
                stream_ms = self._pts_to_ms(stream_pts, stream_timebase)

                # Calculate relative delay (positive means stream starts after video)
                relative_delay = stream_ms - video_ms

                # Only apply positive delays (never cut content)
                # Negative delay means stream starts before video - we keep it at 0
                if relative_delay > 0:
                    delays[stream_idx] = relative_delay
                else:
                    delays[stream_idx] = 0

        return delays

    def _pts_to_ms(self, pts, timebase_str):
        """Convert PTS to milliseconds."""
        try:
            if '/' in timebase_str:
                num, denom = map(int, timebase_str.split('/'))
            else:
                num = 1
                denom = int(1 / float(timebase_str))

            seconds = (pts * num) / denom
            return int(seconds * 1000)
        except:
            return 0

    def _process_ifo_chapters(self, ifo_chapters):
        """Process chapter data from IFO."""
        processed = []
        for chap in ifo_chapters:
            processed.append({
                "number": chap.get('number', len(processed) + 1),
                "start_time": str(chap.get('start_time', 0)),
                "end_time": str(chap.get('end_time', 0)),
                "cell": chap.get('cell'),
                "title": f"Chapter {chap.get('number', len(processed) + 1):02d}"
            })
        return processed

    def _process_ffprobe_chapters(self, chapters):
        """Process chapter data from ffprobe."""
        processed = []
        for i, chap in enumerate(chapters):
            processed.append({
                "number": i + 1,
                "start_time": chap.get("start_time", "0"),
                "end_time": chap.get("end_time", "0"),
                "start_pts": chap.get("start", 0),
                "end_pts": chap.get("end", 0),
                "title": f"Chapter {i+1:02d}"
            })
        return processed

    def _create_mkv_mapping(self, stream_meta, mkv_map):
        """Create MKV muxing parameters for this stream."""
        stream_idx = stream_meta['index']

        mkv_params = {
            "type": stream_meta['type'],
            "source_index": stream_idx,
            "language": stream_meta['language'],
            "delay_ms": stream_meta.get('delay_ms', 0),
            "mkvmerge_options": []
        }

        # Add delay if present
        if stream_meta.get('delay_ms'):
            mkv_params["mkvmerge_options"].append(f"--sync 0:{stream_meta['delay_ms']}")

        # Language tag
        if stream_meta['language'] != 'und':
            mkv_params["mkvmerge_options"].append(f"--language 0:{stream_meta['language']}")

        # Video-specific MKV settings
        if stream_meta['type'] == 'video':
            # Field order
            if stream_meta.get('field_order'):
                field_map = {
                    'tt': '1',  # top field first
                    'tb': '1',  # top field first (alternative)
                    'bb': '2',  # bottom field first
                    'bt': '2',  # bottom field first (alternative)
                }
                if stream_meta['field_order'] in field_map:
                    mkv_params["mkvmerge_options"].append(f"--field-order 0:{field_map[stream_meta['field_order']]}")

            # Aspect ratio / display dimensions
            if stream_meta.get('aspect_ratio'):
                ar = stream_meta['aspect_ratio']
                if ar in ['4:3', '16:9']:
                    # Calculate display dimensions
                    width = stream_meta.get('width', 720)
                    height = stream_meta.get('height', 480)

                    if ar == '4:3':
                        display_width = int(height * 4 / 3)
                    else:  # 16:9
                        display_width = int(height * 16 / 9)

                    mkv_params["mkvmerge_options"].append(f"--display-dimensions 0:{display_width}x{height}")

        # Audio-specific MKV settings
        elif stream_meta['type'] == 'audio':
            # Use track name from IFO or build from metadata
            track_name = stream_meta.get('track_name', '')

            if not track_name:
                # Build track name from channel layout
                layout = stream_meta.get('channel_layout', '')
                channels = stream_meta.get('channels', 0)
                codec = stream_meta.get('codec', '').upper()

                if '5.1' in layout or channels == 6:
                    ch_name = "5.1 Surround"
                elif 'stereo' in layout.lower() or channels == 2:
                    ch_name = "Stereo"
                elif channels == 1:
                    ch_name = "Mono"
                else:
                    ch_name = f"{channels}ch"

                # Add codec to name
                if codec in ['AC3', 'EAC3', 'DTS']:
                    track_name = f"{ch_name} ({codec})"
                else:
                    track_name = ch_name

            if track_name:
                mkv_params["track_name"] = track_name
                mkv_params["mkvmerge_options"].append(f"--track-name 0:{track_name}")

        # Subtitle-specific settings
        elif stream_meta['type'] == 'subtitle':
            if stream_meta.get('forced'):
                mkv_params["mkvmerge_options"].append("--forced-display-flag 0:yes")

            # Add track name from IFO if available
            if track_name := stream_meta.get('track_name'):
                mkv_params["track_name"] = track_name
                mkv_params["mkvmerge_options"].append(f"--track-name 0:{track_name}")

        # Default flag (first track of each type)
        # Note: This is simplified - should check per type
        if stream_idx == 0:
            mkv_params["mkvmerge_options"].append("--default-track-flag 0:yes")
        else:
            mkv_params["mkvmerge_options"].append("--default-track-flag 0:no")

        mkv_map[stream_idx] = mkv_params
        return mkv_params
