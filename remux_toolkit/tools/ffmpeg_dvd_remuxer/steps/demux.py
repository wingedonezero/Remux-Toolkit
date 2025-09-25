# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/demux.py
import re
from pathlib import Path
from ..utils.helpers import run_stream, time_str_to_seconds

class DemuxStep:
    def __init__(self, config):
        self.config = config

    def run(self, context: dict, log_emitter, stop_event):
        """Extract streams individually to preserve metadata."""
        step_info = context.get('step_info', '[STEP]')
        log_emitter(f"{step_info} Extracting streams individually...")

        input_path = context['input_path']
        title_num = context['title_num']
        out_folder = context['out_folder']

        # Get metadata from previous step
        metadata = context.get('title_metadata')
        if not metadata:
            log_emitter("!! ERROR: No metadata found. MetadataAnalysisStep must run first.")
            yield False
            return

        # Track extracted files for context
        context['extracted_streams'] = []

        # Get total duration for progress calculation
        title_info = context.get('title_info', {})
        duration_s = time_str_to_seconds(title_info.get('length'))

        total_streams = len(metadata['streams'])
        current_stream = 0

        for stream_meta in metadata['streams']:
            current_stream += 1
            stream_idx = stream_meta['index']
            stream_type = stream_meta['type']
            codec = stream_meta.get('codec', 'unknown')
            extension = stream_meta.get('extract_extension', '.bin')
            delay_ms = stream_meta.get('delay_ms', 0)

            # Build output filename with delay in name if present
            if delay_ms and stream_type == 'audio':
                output_file = out_folder / f"title_{title_num}_s{stream_idx}_{stream_type}_{delay_ms}ms{extension}"
            else:
                output_file = out_folder / f"title_{title_num}_s{stream_idx}_{stream_type}{extension}"

            log_emitter(f"  [{current_stream}/{total_streams}] Extracting {stream_type} stream #{stream_idx} ({codec}) -> {output_file.name}")

            # Build ffmpeg command for individual stream extraction
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-hide_banner",
                "-progress", "-", "-nostats",
                "-probesize", "100M",  # Larger probe size for DVDs
                "-analyzeduration", "100M",
                "-preindex", "1",  # CRITICAL: Ensures accurate chapter markers and duration from NAV packets
                "-fflags", "+genpts+discardcorrupt",  # Generate PTS, discard corrupt frames
            ]

            # Add trim option if configured (for padding cells)
            if not self.config.get("ffmpeg_trim_padding", True):
                ffmpeg_cmd.extend(["-trim", "0"])

            # Input specification
            ffmpeg_cmd.extend([
                "-f", "dvdvideo",
                "-title", str(title_num),
                "-i", str(input_path),
                "-map", f"0:{stream_idx}",
            ])

            # Handle video streams
            if stream_type == "video":
                ffmpeg_cmd.extend(["-c:v", "copy"])

                # Remove EIA-608 if configured
                if self.config.get("remove_eia_608", True):
                    ffmpeg_cmd.extend(["-bsf:v", "filter_units=remove_types=178"])

                # For raw video streams, we need specific format flags
                if extension == ".m2v":
                    ffmpeg_cmd.extend(["-f", "mpeg2video"])
                elif extension == ".264":
                    ffmpeg_cmd.extend(["-f", "h264"])

            # Handle audio streams
            elif stream_type == "audio":
                ffmpeg_cmd.extend(["-c:a", "copy"])

                # For raw audio, specify format
                if extension in [".ac3", ".eac3", ".dts", ".mp2"]:
                    # These formats are self-contained
                    pass
                elif extension == ".wav":
                    ffmpeg_cmd.extend(["-c:a", "pcm_s16le", "-f", "wav"])

            # Handle subtitle streams
            elif stream_type == "subtitle":
                ffmpeg_cmd.extend(["-c:s", "copy"])

                # DVD subtitles need special handling
                if codec in ["dvd_subtitle", "dvdsub"]:
                    # Extract as raw DVD subtitle
                    ffmpeg_cmd.extend(["-f", "sup"])

            # Output file
            ffmpeg_cmd.append(str(output_file))

            # Track progress for this stream
            stream_duration_us = duration_s * 1_000_000 / total_streams if duration_s > 0 else 0
            base_progress = (current_stream - 1) * 100 / total_streams

            for line in run_stream(ffmpeg_cmd, stop_event):
                # Parse ffmpeg's progress output
                if line.strip().startswith("out_time_us="):
                    try:
                        current_us = int(line.strip().split('=')[1])
                        if stream_duration_us > 0:
                            stream_percent = (current_us / stream_duration_us) * (100 / total_streams)
                            total_percent = int(base_progress + stream_percent)
                            yield min(100, max(0, total_percent))
                    except (ValueError, IndexError):
                        pass
                else:
                    log_emitter(line)

            if stop_event.is_set():
                yield False
                return

            # Verify extraction succeeded
            if not output_file.exists() or output_file.stat().st_size < 1024:
                log_emitter(f"!! WARNING: Failed to extract stream #{stream_idx}. File missing or too small.")
                # Continue with other streams rather than failing completely
            else:
                # Store extracted file info
                context['extracted_streams'].append({
                    'index': stream_idx,
                    'type': stream_type,
                    'file': output_file,
                    'delay_ms': delay_ms,
                    'metadata': stream_meta
                })
                log_emitter(f"     -> Extracted successfully: {output_file.stat().st_size / 1024 / 1024:.1f} MB")

        if not context['extracted_streams']:
            log_emitter("!! ERROR: No streams were successfully extracted.")
            yield False
            return

        log_emitter(f"  -> Extracted {len(context['extracted_streams'])} streams successfully.")
        yield True
