# A/B Comparator Fixes Summary

## Issues and Solutions

### 1. Float Error in Frame Sync ✅ FIXED
**Issue:** `'float' object cannot be interpreted as an integer`
**Location:** `audio_correlation_frame_sync.py` line 611-612
**Fix:** Cast `search_start_frame` and `search_end_frame` to `int()` before using in `range()`

### 2. Old Settings Being Loaded
**Issue:** User sees 8 chunks instead of 60 because their saved config has old values
**Location:** `video_ab_comparator_gui.py` line 73
```python
self.settings = self.app_manager.load_config(self.tool_name, DEFAULTS)
```
**Explanation:** When settings are saved, they override DEFAULTS. User's saved config has:
- `analysis_chunk_count: 8` (old)
- `analysis_chunk_duration: 2.0` (old)
- `tie_threshold: 2.0` (old, if it exists)

**Solution:** Need to add migration logic or have user reset settings.

### 3. Settings Dialog Needs Tabs
**Issue:** All settings in one long scrolling form, missing new controls
**Location:** `gui/settings_dialog.py`
**Missing controls:**
- `tie_threshold` (scoring)
- `filter_low_information_frames` (scoring)
- `align_visual_num_checkpoints` (frame sync)
- `align_visual_window_radius` (frame sync)
- `align_visual_hash_size` (frame sync)
- `align_visual_hash_algorithm` (frame sync)
- `align_visual_hash_threshold` (frame sync)
- `align_visual_agreement_tolerance_ms` (frame sync)

**Solution:** Reorganize into tabs:
- **Analysis** tab: chunk_count, chunk_duration, tie_threshold, filter
- **Alignment** tab: all align_* settings
- **Frame Sync** tab: all visual_* settings
- **Detectors** tab: enable_* checkboxes

### 4. No Resource Cleanup
**Issue:** VideoSource objects keep files open, RAM not freed
**Location:** `video_ab_comparator_gui.py`

**Missing cleanup:**
```python
# Need to add:
def _cleanup_pipeline(self):
    if self.pipeline:
        if hasattr(self.pipeline, 'source_a'):
            self.pipeline.source_a.close()
        if hasattr(self.pipeline, 'source_b'):
            self.pipeline.source_b.close()
        self.pipeline = None
```

**Where to call:**
- In `start_comparison()` before creating new pipeline
- In `shutdown()` when tab closes

### 5. Logging Goes to Terminal
**Issue:** All print() statements go to terminal, not to Analysis Log tab
**Locations:**
- `pipeline.py` - many print statements
- `alignment_advanced.py` - print statements
- `audio_correlation_frame_sync.py` - print statements

**Solution:** Pass a logger or callback to capture print output

**Current:**
```python
self.pipeline.progress.connect(self.update_progress)  # Only gets progress updates
```

**Need:**
```python
# Redirect print statements to log
import sys
from io import StringIO

class LogCapture:
    def __init__(self, log_widget):
        self.log_widget = log_widget
        self.stdout_backup = sys.stdout

    def write(self, message):
        if message.strip():
            self.log_widget.append(message.rstrip())
        self.stdout_backup.write(message)

    def flush(self):
        pass
```

### 6. No Temp File Cleanup
**Issue:** Temp files accumulate, not cleaned between runs
**Location:** Pipeline creates temp files but doesn't clean them up

**Solution:**
```python
def _cleanup_temp_files(self):
    temp_dir = self.app_manager.get_temp_dir(self.tool_name)
    if temp_dir and os.path.exists(temp_dir):
        for file in os.listdir(temp_dir):
            try:
                os.remove(os.path.join(temp_dir, file))
            except:
                pass
```

**Where to call:**
- In `start_comparison()` before running new comparison
- In `shutdown()` when tab closes (optional - may want to keep last results)

## Implementation Priority

1. ✅ Fix float error (done)
2. 🔴 Add resource cleanup (critical - prevents file locks)
3. 🔴 Improve logging (critical - user experience)
4. 🟡 Update settings dialog with tabs (important - missing features)
5. 🟡 Add temp file cleanup (important - disk space)
6. 🟢 Add settings migration (nice to have - user can reset manually)

## Quick Fix for User

**To get 60 chunks immediately:**
1. Delete saved settings file (location depends on app_manager implementation)
2. Or manually edit settings file to change `analysis_chunk_count` to 60
3. Or add "Reset to Defaults" button in settings dialog

**To see logs:**
- Currently only way is to run from terminal
- After logging fix, everything will appear in Analysis Log tab
