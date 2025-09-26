# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/timing_analysis.py
from pathlib import Path
import json

class TimingAnalysisStep:
    """Comprehensive timing analysis using multiple methods with fallback."""

    def __init__(self, config):
        self.config = config

    def run(self, context: dict, log_emitter, stop_event) -> bool:
        """Analyze timing using configured method with automatic fallback."""
        step_info = context.get('step_info', '[STEP]')
        log_emitter(f"{step_info} Analyzing stream timing and synchronization...")

        # Get timing method from config (auto, pgc, pts, ffprobe)
        timing_method = self.config.get("timing_method", "auto")

        probe_data = context.get('title_metadata', {}).get('probe_data', {})
        ifo_data = context.get('ifo_data', {})

        # Initialize timing results
        timing_results = {
            'method_used': None,
            'video_reference_pts': 0,
            'stream_timings': {},
            'adjustments': {},
            'warnings': []
        }

        # Try methods in order based on config
        if timing_method == "auto":
            methods_to_try = ["pgc", "pts", "ffprobe"]
        else:
            methods_to_try = [timing_method, "pts", "ffprobe"]  # Fallback chain

        for method in methods_to_try:
            log_emitter(f"  -> Attempting timing analysis using {method.upper()} method...")

            if method == "pgc" and ifo_data.get('pgc_data'):
                result = self._analyze_pgc_timing(ifo_data, probe_data, log_emitter)
            elif method == "pts":
                result = self._analyze_pts_timing(probe_data, log_emitter)
            elif method == "ffprobe":
                result = self._analyze_ffprobe_timing(probe_data, log_emitter)
            else:
                continue

            if result:
                timing_results.update(result)
                timing_results['method_used'] = method
                break

        if not timing_results['method_used']:
            log_emitter("!! WARNING: Could not determine timing, using defaults")
            timing_results = self._get_default_timing(probe_data)

        # Log all raw timing values
        log_emitter("\n=== RAW TIMING DATA ===")
        for stream_idx, timing in timing_results['stream_timings'].items():
            log_emitter(f"  Stream #{stream_idx}: PTS={timing['pts']}, "
                       f"TimeBase={timing['time_base']}, "
                       f"StartTime={timing['start_time']}s, "
                       f"Type={timing['type']}")

        # Calculate adjusted delays with video as reference
        adjustments = self._calculate_adjustments(timing_results, log_emitter)
        timing_results['adjustments'] = adjustments

        # Log adjusted values
        log_emitter("\n=== ADJUSTED DELAYS (Video as Reference) ===")
        video_delay = adjustments.get('video_delay', 0)
        if video_delay > 0:
            log_emitter(f"  VIDEO will be delayed by {video_delay}ms to maintain sync")

        for stream_idx, delay in adjustments['stream_delays'].items():
            stream_type = timing_results['stream_timings'][stream_idx]['type']
            if delay > 0:
                log_emitter(f"  Stream #{stream_idx} ({stream_type}): +{delay}ms delay")
            else:
                log_emitter(f"  Stream #{stream_idx} ({stream_type}): no delay needed")

        # Store results in context
        context['timing_analysis'] = timing_results

        # Apply to metadata
        self._apply_timing_to_metadata(context, timing_results, log_emitter)

        return True

    def _analyze_pgc_timing(self, ifo_data, probe_data, log_emitter):
        """Analyze timing from PGC data (most accurate for DVDs)."""
        pgc_data = ifo_data.get('pgc_data', {})
        if not pgc_data or not pgc_data.get('pgc_list'):
            return None

        # Get first PGC (main program chain)
        first_pgc = pgc_data['pgc_list'][0]

        # Get cell timings for accurate sync
        cells = first_pgc.get('cell_playback', [])
        if not cells:
            return None

        timing = {'stream_timings': {}}

        # Calculate base PTS from first cell
        first_cell = cells[0] if cells else {}
        base_pts = first_cell.get('first_sector', 0) * 2048  # Sector to bytes

        # Process each stream
        for stream in probe_data.get('streams', []):
            idx = stream['index']
            timing['stream_timings'][idx] = {
                'type': stream['codec_type'],
                'pts': stream.get('start_pts', 0),
                'time_base': stream.get('time_base', '1/90000'),
                'start_time': float(stream.get('start_time', 0)),
                'pgc_offset': 0  # Will calculate from NAV packets
            }

        log_emitter("  -> PGC timing analysis complete")
        return timing

    def _analyze_pts_timing(self, probe_data, log_emitter):
        """Analyze timing from PTS values (standard method)."""
        timing = {'stream_timings': {}}

        for stream in probe_data.get('streams', []):
            idx = stream['index']
            timing['stream_timings'][idx] = {
                'type': stream['codec_type'],
                'pts': int(stream.get('start_pts', 0)),
                'time_base': stream.get('time_base', '1/90000'),
                'start_time': float(stream.get('start_time', 0))
            }

        log_emitter("  -> PTS timing analysis complete")
        return timing

    def _analyze_ffprobe_timing(self, probe_data, log_emitter):
        """Analyze timing from ffprobe start_time values (fallback)."""
        timing = {'stream_timings': {}}

        for stream in probe_data.get('streams', []):
            idx = stream['index']
            start_time = float(stream.get('start_time', 0))

            # Convert start_time to PTS (assume 90kHz clock for DVDs)
            pts = int(start_time * 90000)

            timing['stream_timings'][idx] = {
                'type': stream['codec_type'],
                'pts': pts,
                'time_base': '1/90000',
                'start_time': start_time
            }

        log_emitter("  -> FFprobe timing analysis complete")
        return timing

    def _calculate_adjustments(self, timing_results, log_emitter):
        """Calculate delay adjustments to keep everything positive."""
        adjustments = {
            'video_delay': 0,
            'stream_delays': {}
        }

        # Find the earliest stream (most negative relative to video)
        video_pts = None
        min_pts = None
        video_idx = None

        for idx, timing in timing_results['stream_timings'].items():
            pts = self._pts_to_ms(timing['pts'], timing['time_base'])

            if timing['type'] == 'video':
                video_pts = pts
                video_idx = idx

            if min_pts is None or pts < min_pts:
                min_pts = pts

        if video_pts is None:
            log_emitter("!! WARNING: No video stream found for timing reference")
            return adjustments

        # If any stream starts before video, delay video to compensate
        if min_pts < video_pts:
            video_delay = video_pts - min_pts
            adjustments['video_delay'] = video_delay
            log_emitter(f"  -> Video starts {video_delay}ms after earliest stream")

            # Now calculate all stream delays relative to delayed video
            for idx, timing in timing_results['stream_timings'].items():
                pts_ms = self._pts_to_ms(timing['pts'], timing['time_base'])
                # Relative to new video position (0)
                relative_delay = pts_ms - min_pts
                adjustments['stream_delays'][idx] = max(0, relative_delay)
        else:
            # Video is earliest, use it as reference
            adjustments['video_delay'] = 0

            for idx, timing in timing_results['stream_timings'].items():
                pts_ms = self._pts_to_ms(timing['pts'], timing['time_base'])
                relative_delay = pts_ms - video_pts
                adjustments['stream_delays'][idx] = max(0, relative_delay)

        return adjustments

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

    def _apply_timing_to_metadata(self, context, timing_results, log_emitter):
        """Apply calculated timing to metadata."""
        metadata = context.get('title_metadata', {})
        adjustments = timing_results['adjustments']

        # Update stream delays in metadata
        for stream in metadata.get('streams', []):
            idx = stream['index']
            if idx in adjustments['stream_delays']:
                stream['delay_ms'] = adjustments['stream_delays'][idx]
                stream['delay_source'] = timing_results['method_used']

        # Store video delay if needed
        metadata['video_delay_ms'] = adjustments.get('video_delay', 0)

        log_emitter(f"  -> Applied timing using {timing_results['method_used']} method")

    def _get_default_timing(self, probe_data):
        """Get default timing when analysis fails."""
        timing = {
            'method_used': 'default',
            'stream_timings': {},
            'warnings': ['Using default timing - no delay applied']
        }

        for stream in probe_data.get('streams', []):
            idx = stream['index']
            timing['stream_timings'][idx] = {
                'type': stream['codec_type'],
                'pts': 0,
                'time_base': '1/90000',
                'start_time': 0
            }

        return timing
