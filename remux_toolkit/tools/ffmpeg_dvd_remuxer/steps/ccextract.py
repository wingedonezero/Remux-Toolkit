# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/ccextract.py
from ..utils.helpers import run_stream

class CCExtractStep:
    def __init__(self, config):
        self.config = config

    @property
    def is_enabled(self):
        return self.config.get("run_ccextractor", True)

    def run(self, context: dict, log_emitter, stop_event) -> bool:
        if not self.is_enabled:
            log_emitter(f"{context.get('step_info', '[OPTIONAL]')} Skipping CCExtractor (disabled in settings).")
            context['cc_found'] = False
            return True

        step_info = context.get('step_info', '[STEP]')
        log_emitter(f"{step_info} Extracting closed captions...")

        # Check if we removed EIA-608 during extraction
        remove_eia = self.config.get("remove_eia_608", True)

        if remove_eia:
            # If we removed EIA-608, we need to extract from the source DVD
            log_emitter("  -> Extracting CC from original DVD source (EIA-608 was removed from video)")
            input_path = context['input_path']
            title_num = context['title_num']

            # Create a temporary video file without removing EIA-608 for CC extraction
            temp_video = context['out_folder'] / f"title_{title_num}_cc_temp.m2v"

            ffmpeg_cmd = [
                "ffmpeg", "-y", "-hide_banner",
                "-probesize", "100M",
                "-analyzeduration", "100M",
                "-preindex", "1",
                "-f", "dvdvideo",
                "-title", str(title_num),
                "-i", str(input_path),
                "-map", "0:v:0",  # First video stream
                "-c:v", "copy",
                "-f", "mpeg2video",
                str(temp_video)
            ]

            # Don't apply EIA-608 removal filter for this extraction
            log_emitter("  -> Extracting video with EIA-608 data intact...")
            for line in run_stream(ffmpeg_cmd, stop_event):
                if "frame=" in line:  # Only show progress lines
                    continue

            if stop_event.is_set():
                return False

            if not temp_video.exists():
                log_emitter("  -> Failed to extract video for CC analysis")
                context['cc_found'] = False
                return True

            source_file = temp_video
        else:
            # If we didn't remove EIA-608, use the extracted video file
            extracted_streams = context.get('extracted_streams', [])
            video_file = None
            for stream_info in extracted_streams:
                if stream_info['type'] == 'video':
                    video_file = stream_info['file']
                    break

            if not video_file or not video_file.exists():
                log_emitter("  -> No video stream found for caption extraction.")
                context['cc_found'] = False
                return True

            source_file = video_file

        # Run CCExtractor on the source file
        cc_srt = context['out_folder'] / f"title_{context['title_num']}_cc.srt"
        context['cc_srt_path'] = cc_srt

        ccextractor_cmd = ["ccextractor", "-out=srt", "-o", str(cc_srt), str(source_file), "-quiet"]
        for line in run_stream(ccextractor_cmd, stop_event):
            log_emitter(line)

        if stop_event.is_set():
            return False

        # Clean up temp file if we created one
        if remove_eia and 'temp_video' in locals() and temp_video.exists():
            try:
                temp_video.unlink()
            except:
                pass

        cc_found = cc_srt.exists() and cc_srt.stat().st_size > 10
        if cc_found:
            log_emitter("  -> Closed captions extracted successfully.")
        else:
            log_emitter("  -> No EIA-608 captions found.")

        context['cc_found'] = cc_found
        return True
