# remux_toolkit/tools/ffmpeg_dvd_remuxer/core/error_handler.py
import logging
from enum import Enum
from pathlib import Path

class ErrorSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

class ErrorHandler:
    """Centralized error handling with recovery strategies."""

    def __init__(self, config, log_emitter):
        self.config = config
        self.log_emitter = log_emitter
        self.errors = []
        self.warnings = []

    def handle_sync_error(self, stream_idx, expected_pts, actual_pts):
        """Handle audio/video sync issues."""
        diff_ms = abs(expected_pts - actual_pts) / 90  # PTS to ms

        if diff_ms < 100:
            # Small sync issue - auto-correct
            self.add_warning(
                f"Small sync issue on stream {stream_idx}: {diff_ms:.1f}ms - auto-correcting"
            )
            return True
        elif diff_ms < 1000:
            # Medium sync issue - attempt correction
            if self.config.get("auto_fix_sync", True):
                self.add_warning(
                    f"Sync issue on stream {stream_idx}: {diff_ms:.1f}ms - applying correction"
                )
                return True
            else:
                self.add_error(
                    f"Sync issue on stream {stream_idx}: {diff_ms:.1f}ms - manual review needed"
                )
                return False
        else:
            # Large sync issue - likely corrupt
            self.add_error(
                f"Major sync issue on stream {stream_idx}: {diff_ms:.1f}ms - stream may be corrupt"
            )
            return False

    def handle_read_error(self, sector, retry_count=3):
        """Handle DVD read errors with retry logic."""
        for attempt in range(retry_count):
            self.log_emitter(f"  -> Read error at sector {sector}, attempt {attempt + 1}/{retry_count}")
            # In real implementation, would retry the read here
            # For now, just log the attempt

        self.add_error(f"Failed to read sector {sector} after {retry_count} attempts")

        if self.config.get("skip_damaged_sectors", True):
            self.add_warning(f"Skipping damaged sector {sector}")
            return True
        return False

    def handle_missing_stream(self, stream_type, stream_idx):
        """Handle missing or unreadable streams."""
        if stream_type == "subtitle":
            # Subtitles are optional
            self.add_warning(f"Subtitle stream {stream_idx} could not be extracted - continuing without it")
            return True
        elif stream_type == "audio":
            # Check if we have at least one audio track
            if self.has_valid_audio():
                self.add_warning(f"Audio stream {stream_idx} could not be extracted - using remaining audio")
                return True
            else:
                self.add_error("No valid audio streams found - cannot continue")
                return False
        else:
            # Video is critical
            self.add_error(f"Video stream {stream_idx} could not be extracted - cannot continue")
            return False

    def handle_chapter_error(self, chapter_num, issue):
        """Handle chapter marker issues."""
        if self.config.get("auto_fix_chapters", True):
            self.add_warning(f"Chapter {chapter_num}: {issue} - auto-correcting")
            return True
        else:
            self.add_warning(f"Chapter {chapter_num}: {issue} - skipping chapter")
            return False

    def handle_extraction_error(self, stream_idx, error_msg):
        """Handle stream extraction failures."""
        # Parse error message for known issues
        if "No space left" in error_msg:
            self.add_error("Insufficient disk space for extraction")
            return False
        elif "Permission denied" in error_msg:
            self.add_error(f"Permission denied writing stream {stream_idx}")
            return False
        elif "Conversion failed" in error_msg:
            # Try alternative extraction method
            self.add_warning(f"Stream {stream_idx} extraction failed, trying alternative method")
            return True
        else:
            self.add_error(f"Stream {stream_idx} extraction failed: {error_msg}")
            return False

    def validate_output(self, output_file: Path):
        """Validate the final output file."""
        if not output_file.exists():
            self.add_error("Output file was not created")
            return False

        size_mb = output_file.stat().st_size / (1024 * 1024)

        if size_mb < 10:
            self.add_error(f"Output file suspiciously small: {size_mb:.1f}MB")
            return False

        if size_mb > 50000:  # 50GB
            self.add_warning(f"Output file very large: {size_mb:.1f}MB")

        return True

    def has_valid_audio(self):
        """Check if we have at least one valid audio stream."""
        # This would check the context for valid audio streams
        # Placeholder for now
        return True

    def add_error(self, message):
        """Add an error message."""
        self.errors.append(message)
        self.log_emitter(f"!! ERROR: {message}")

    def add_warning(self, message):
        """Add a warning message."""
        self.warnings.append(message)
        self.log_emitter(f"⚠ WARNING: {message}")

    def add_info(self, message):
        """Add an info message."""
        self.log_emitter(f"ℹ INFO: {message}")

    def get_summary(self):
        """Get error/warning summary."""
        return {
            'errors': self.errors,
            'warnings': self.warnings,
            'has_errors': len(self.errors) > 0,
            'has_warnings': len(self.warnings) > 0
        }

    def should_continue(self):
        """Determine if processing should continue despite errors."""
        if not self.errors:
            return True

        # Check if all errors are recoverable
        critical_keywords = ['cannot continue', 'corrupt', 'no valid']
        for error in self.errors:
            if any(keyword in error.lower() for keyword in critical_keywords):
                return False

        # If only non-critical errors, continue with warnings
        return self.config.get("continue_on_error", False)
