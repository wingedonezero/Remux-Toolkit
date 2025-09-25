# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/finalize.py
from pathlib import Path
from ..utils.helpers import run_stream

class FinalizeStep:
    def __init__(self, config):
        self.config = config

    def run(self, context: dict, log_emitter, stop_event) -> bool:
        step_info = context.get('step_info', '[STEP]')
        log_emitter(f"{step_info} Building final MKV file with mkvmerge...")

        title_num = context['title_num']
        out_folder = context['out_folder']
        final_mkv = out_folder / f"title_{title_num}.mkv"

        # Get extracted streams and metadata
        extracted_streams = context.get('extracted_streams', [])
        metadata = context.get('title_metadata', {})
        mkv_mapping = metadata.get('mkv_mapping', {})

        if not extracted_streams:
            log_emitter("!! ERROR: No extracted streams found to mux.")
            return False

        # Build mkvmerge command
        mkvmerge_cmd = ["mkvmerge", "-o", str(final_mkv), "--no-global-tags"]

        # Sort streams by type priority: video first, then audio, then subtitles
        type_order = {'video': 0, 'audio': 1, 'subtitle': 2}
        extracted_streams.sort(key=lambda x: (type_order.get(x['type'], 99), x['index']))

        # Track which streams we're adding
        added_streams = []

        # Process each extracted stream
        for i, stream_info in enumerate(extracted_streams):
            stream_file = stream_info['file']
            stream_type = stream_info['type']
            stream_idx = stream_info['index']
            delay_ms = stream_info.get('delay_ms', 0)
            stream_meta = stream_info.get('metadata', {})

            # Get MKV options for this stream
            mkv_params = mkv_mapping.get(stream_idx, {})

            # Add delay if present
            if delay_ms:
                mkvmerge_cmd.extend(["--sync", f"0:{delay_ms}"])
                log_emitter(f"  -> Applying {delay_ms}ms delay to {stream_type} stream #{stream_idx}")

            # Language
            lang = stream_meta.get('language', 'und')
            if lang != 'und':
                mkvmerge_cmd.extend(["--language", f"0:{lang}"])

            # Track name for audio
            if stream_type == 'audio':
                channels = stream_meta.get('channels', 0)
                layout = stream_meta.get('channel_layout', '')

                if '5.1' in layout or channels == 6:
                    track_name = "5.1 Surround"
                elif 'stereo' in layout.lower() or channels == 2:
                    track_name = "Stereo"
                elif channels == 1:
                    track_name = "Mono"
                else:
                    track_name = f"{channels}ch Audio"

                # Add codec info to track name
                codec = stream_meta.get('codec', '').upper()
                if codec in ['AC3', 'EAC3', 'DTS']:
                    track_name = f"{track_name} ({codec})"

                mkvmerge_cmd.extend(["--track-name", f"0:{track_name}"])

            # Video-specific options
            if stream_type == 'video':
                # Check if telecine detection determined this should be progressive
                detected_progressive = context.get('detected_progressive')

                # Field order handling
                field_order = stream_meta.get('field_order')
                if detected_progressive is True:
                    # Force progressive flag for telecined content
                    mkvmerge_cmd.extend(["--field-order", "0:0"])
                    log_emitter(f"  -> Setting field order: progressive (telecine detected)")
                elif field_order and detected_progressive is not False:
                    # Use original field order if not forced interlaced
                    field_map = {
                        'tt': '1', 'tb': '1',  # top field first
                        'bb': '2', 'bt': '2',  # bottom field first
                    }
                    if field_order in field_map:
                        mkvmerge_cmd.extend(["--field-order", f"0:{field_map[field_order]}"])
                        log_emitter(f"  -> Setting field order: {field_order}")

                # Display dimensions/aspect ratio
                if aspect := stream_meta.get('aspect_ratio'):
                    if 'x' in aspect:  # If it's dimensions like "720x480"
                        mkvmerge_cmd.extend(["--display-dimensions", f"0:{aspect}"])
                    elif ':' in aspect:  # If it's ratio like "4:3"
                        width = stream_meta.get('width', 720)
                        height = stream_meta.get('height', 480)
                        # Calculate display width from aspect ratio
                        if aspect == "4:3":
                            display_width = int(height * 4 / 3)
                        elif aspect == "16:9":
                            display_width = int(height * 16 / 9)
                        else:
                            display_width = width
                        mkvmerge_cmd.extend(["--display-dimensions", f"0:{display_width}x{height}"])

            # Subtitle-specific options
            if stream_type == 'subtitle':
                if stream_meta.get('forced'):
                    mkvmerge_cmd.extend(["--forced-display-flag", "0:yes"])

            # Default track flags (first of each type is default)
            if stream_type not in [s['type'] for s in added_streams]:
                mkvmerge_cmd.extend(["--default-track-flag", "0:yes"])
            else:
                mkvmerge_cmd.extend(["--default-track-flag", "0:no"])

            # Add the file
            mkvmerge_cmd.append(str(stream_file))
            added_streams.append(stream_info)

        # Add CCExtractor subtitle if found
        if context.get('cc_found', False):
            cc_srt = context.get('cc_srt_path')
            if cc_srt and cc_srt.exists():
                # The line 'mkvmerge_cmd.append("+")' has been removed.
                mkvmerge_cmd.extend([
                    "--language", "0:eng",
                    "--track-name", "0:Closed Captions (EIA-608)",
                    "--default-track-flag", "0:no",
                    str(cc_srt)
                ])
                log_emitter("  -> Adding extracted closed captions")

        # Add chapters if processed
        if context.get('chapters_ok', False):
            mod_chap_xml = context.get('mod_chap_xml_path')
            if mod_chap_xml and mod_chap_xml.exists():
                mkvmerge_cmd.extend(["--chapters", str(mod_chap_xml)])
                log_emitter("  -> Adding chapter information")

        # Execute mkvmerge
        log_emitter(f"  -> Muxing {len(added_streams)} streams into final MKV...")
        for line in run_stream(mkvmerge_cmd, stop_event):
            log_emitter(line)

        if stop_event.is_set():
            return False

        # Verify final file
        if not final_mkv.exists() or final_mkv.stat().st_size < 1024:
            log_emitter("!! ERROR: mkvmerge failed to create the final file.")
            return False

        # Clean up extracted streams (they're now in the MKV)
        log_emitter("  -> Cleaning up extracted streams...")
        for stream_info in extracted_streams:
            try:
                stream_info['file'].unlink()
            except OSError:
                pass

        # Also clean up any metadata files if configured
        if context.get('cleanup_metadata', True):
            try:
                if meta_file := context.get('metadata_file'):
                    if meta_file.exists():
                        meta_file.unlink()
            except OSError:
                pass

        log_emitter(f"🎉 Successfully created: {final_mkv.name} ({final_mkv.stat().st_size / 1024 / 1024:.1f} MB)")
        return True
