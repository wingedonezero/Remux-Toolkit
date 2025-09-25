# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/telecine_detection.py
import re
from pathlib import Path
from ..utils.helpers import run_stream

class TelecineDetectionStep:
    """Detect telecined film content in interlaced video streams."""

    def __init__(self, config):
        self.config = config

    @property
    def is_enabled(self):
        """Only run if telecine detection is enabled in config."""
        mode = self.config.get("telecine_detection_mode", "disabled")
        return mode != "disabled"

    def run(self, context: dict, log_emitter, stop_event) -> bool:
        """Analyze video for telecine patterns and determine if it should be flagged as progressive."""

        mode = self.config.get("telecine_detection_mode", "disabled")

        # Handle forced modes
        if mode == "force_progressive":
            context['detected_progressive'] = True
            log_emitter(f"{context.get('step_info', '[STEP]')} Telecine detection: Forcing progressive flag")
            return True
        elif mode == "force_interlaced":
            context['detected_progressive'] = False
            log_emitter(f"{context.get('step_info', '[STEP]')} Telecine detection: Forcing interlaced flag")
            return True
        elif mode == "disabled":
            context['detected_progressive'] = None
            return True

        # Auto-detect mode
        step_info = context.get('step_info', '[STEP]')
        log_emitter(f"{step_info} Analyzing video for telecine/progressive content...")

        input_path = context['input_path']
        title_num = context['title_num']

        # Get metadata to check if already marked progressive
        metadata = context.get('title_metadata', {})
        video_stream = next((s for s in metadata.get('streams', []) if s['type'] == 'video'), None)

        if not video_stream:
            log_emitter("  -> No video stream found for telecine detection")
            context['detected_progressive'] = None
            return True

        # Check current field order
        field_order = video_stream.get('field_order')
        if not field_order or field_order not in ['tt', 'bb', 'tb', 'bt']:
            log_emitter(f"  -> Video appears to already be progressive or has no field order")
            context['detected_progressive'] = None
            return True

        # Get config values
        threshold = self.config.get("telecine_threshold", 85)
        sample_duration = self.config.get("telecine_sample_duration", 60)

        # Run idet filter to detect interlacing patterns
        # Sample from multiple points in the video for accuracy
        log_emitter(f"  -> Sampling up to {sample_duration} seconds with idet filter (threshold: {threshold}%)")

        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner",
            "-probesize", "100M",
            "-analyzeduration", "100M",
            "-preindex", "1",
            "-f", "dvdvideo",
            "-title", str(title_num),
            "-i", str(input_path),
            "-t", str(sample_duration),  # Analyze first N seconds
            "-vf", "idet",  # Interlace detection filter
            "-f", "null",
            "-"
        ]

        # Track idet statistics
        tff_frames = 0  # Top field first interlaced
        bff_frames = 0  # Bottom field first interlaced
        prog_frames = 0  # Progressive
        undetermined = 0

        for line in run_stream(ffmpeg_cmd, stop_event):
            # Parse idet output: [Parsed_idet_0 @ ...] Single frame detection: TFF:0 BFF:0 Progressive:1 Undetermined:0
            if "Single frame detection:" in line:
                match = re.search(r'TFF:(\d+)\s+BFF:(\d+)\s+Progressive:(\d+)\s+Undetermined:(\d+)', line)
                if match:
                    tff_frames = int(match.group(1))
                    bff_frames = int(match.group(2))
                    prog_frames = int(match.group(3))
                    undetermined = int(match.group(4))
            # Also catch the summary line
            elif "Multi frame detection:" in line:
                # This gives us the more accurate multi-frame detection results
                match = re.search(r'TFF:(\d+)\s+BFF:(\d+)\s+Progressive:(\d+)\s+Undetermined:(\d+)', line)
                if match:
                    tff_frames = int(match.group(1))
                    bff_frames = int(match.group(2))
                    prog_frames = int(match.group(3))
                    undetermined = int(match.group(4))

        if stop_event.is_set():
            return False

        # Calculate percentages
        total_frames = tff_frames + bff_frames + prog_frames + undetermined
        if total_frames == 0:
            log_emitter("  -> No frames analyzed, skipping telecine detection")
            context['detected_progressive'] = None
            return True

        prog_percent = (prog_frames / total_frames) * 100
        interlaced_percent = ((tff_frames + bff_frames) / total_frames) * 100

        log_emitter(f"  -> Analysis complete: {prog_percent:.1f}% progressive, {interlaced_percent:.1f}% interlaced")
        log_emitter(f"     (TFF:{tff_frames} BFF:{bff_frames} Prog:{prog_frames} Undet:{undetermined})")

        # Determine if content should be flagged as progressive
        if prog_percent >= threshold:
            log_emitter(f"  -> DETECTED: Telecined film content (>{threshold}% progressive)")
            log_emitter(f"  -> Will flag as progressive for optimal playback")
            context['detected_progressive'] = True
        else:
            log_emitter(f"  -> Video is truly interlaced, keeping interlaced flag")
            context['detected_progressive'] = False

        # Store detailed results for reference
        context['telecine_analysis'] = {
            'progressive_percent': prog_percent,
            'interlaced_percent': interlaced_percent,
            'tff_frames': tff_frames,
            'bff_frames': bff_frames,
            'progressive_frames': prog_frames,
            'undetermined_frames': undetermined,
            'threshold_used': threshold,
            'detected_as_progressive': context['detected_progressive']
        }

        # Save to metadata file for debugging
        if metadata_file := context.get('metadata_file'):
            try:
                import json
                with open(metadata_file, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
                meta['telecine_analysis'] = context['telecine_analysis']
                with open(metadata_file, 'w', encoding='utf-8') as f:
                    json.dump(meta, f, indent=2)
            except:
                pass

        return True
