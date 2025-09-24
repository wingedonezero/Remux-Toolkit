# remux_toolkit/tools/media_comparator/media_comparator_core.py
import subprocess
import json
import hashlib
import os
import tempfile
import re
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

# (All helper functions like check_dependencies, get_stream_info, etc., are unchanged)
def check_dependencies():
    """Checks for ffmpeg, ffprobe, and mkvextract."""
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True)
        subprocess.run(["ffprobe", "-version"], capture_output=True, text=True, check=True)
        subprocess.run(["mkvextract", "--version"], capture_output=True, text=True, check=True)
        return True, None
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        tool = "mkvextract" if "mkvextract" in str(e) else "ffmpeg/ffprobe"
        return False, f"FATAL ERROR: {tool} not found in your system's PATH."

def get_stream_info(filepath, entries="streams"):
    if not os.path.exists(filepath):
        return None, f"Error: File not found at '{filepath}'"
    command = ["ffprobe", "-v", "quiet", "-print_format", "json", f"-show_{entries}", filepath]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        return json.loads(result.stdout).get(entries, []), None
    except subprocess.CalledProcessError as e:
        return None, f"ffprobe error: {e.stderr}"
    except json.JSONDecodeError:
        return None, "Error: Failed to parse ffprobe JSON output."

def get_stream_hash_copied(filepath, stream_index):
    command = ["ffmpeg", "-v", "error", "-i", filepath, "-map", f"0:{stream_index}", "-c", "copy", "-f", "hash", "-hash", "MD5", "-"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        hash_line = result.stdout.strip()
        if hash_line.startswith("MD5="): return hash_line.split("=")[1], None
        return None, "Error: Could not find MD5 hash in ffmpeg output."
    except subprocess.CalledProcessError as e: return None, f"ffmpeg error: {e.stderr}"

def get_stream_hash_decoded(filepath, stream_index):
    command = ["ffmpeg", "-v", "error", "-i", filepath, "-map", f"0:{stream_index}", "-f", "hash", "-hash", "MD5", "-"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        hash_line = result.stdout.strip()
        if hash_line.startswith("MD5="): return hash_line.split("=")[1], None
        return None, "Error: Could not find MD5 hash in ffmpeg output."
    except subprocess.CalledProcessError as e: return None, f"ffmpeg error: {e.stderr}"

def get_stream_hash_streamhash(filepath, stream_index):
    command = ["ffmpeg", "-v", "error", "-i", filepath, "-map", f"0:{stream_index}", "-f", "streamhash", "-hash", "MD5", "-"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        line = result.stdout.strip()
        if "MD5=" in line: return line.split("=")[-1], None
        return None, f"Error: Could not parse streamhash output. Got: {line}"
    except subprocess.CalledProcessError as e: return None, f"ffmpeg error: {e.stderr}"

def get_raw_stream_hash_in_memory(filepath, stream_index, codec_name):
    codec_to_format_map = {'truehd': 'truehd', 'ac3': 'ac3', 'dts': 'dts', 'aac': 'adts', 'flac': 'flac', 'opus': 'opus', 'vorbis': 'ogg', 'h264': 'h264', 'hevc': 'hevc', 'mpeg2video': 'mpeg2video', 'subrip': 'srt', 'ass': 'ass', 'dvd_subtitle': 'vobsub', 'pcm_s16le': 's16le', 'pcm_s24le': 's24le', 'pcm_s32le': 's32le'}
    raw_format = codec_to_format_map.get(codec_name)
    if not raw_format: return None, f"Unsupported codec '{codec_name}' for raw extraction."
    command = ["ffmpeg", "-v", "error", "-i", filepath, "-map", f"0:{stream_index}", "-c", "copy", "-f", raw_format, "-"]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout_data, stderr_data = process.communicate()
        if process.returncode != 0: return None, f"ffmpeg error during raw extraction: {stderr_data.decode()}"
        hasher = hashlib.md5(); hasher.update(stdout_data); return hasher.hexdigest(), None
    except Exception as e: return None, f"An error occurred during in-memory hashing: {e}"

def get_mkvextract_hash(filepath, stream_index):
    temp_dir = tempfile.gettempdir()
    temp_filename = os.path.join(temp_dir, f"temp_stream_{stream_index}")
    command = ["mkvextract", "tracks", filepath, f"{stream_index}:{temp_filename}"]
    try:
        subprocess.run(command, capture_output=True, text=True, check=True)
        hasher = hashlib.md5()
        with open(temp_filename, 'rb') as f:
            while chunk := f.read(8192): hasher.update(chunk)
        return hasher.hexdigest(), None
    except subprocess.CalledProcessError as e: return None, f"mkvextract error: {e.stderr}"
    except FileNotFoundError: return None, "The temporary file was not created by mkvextract."
    finally:
        if os.path.exists(temp_filename): os.remove(temp_filename)

_SIL_START = re.compile(r"silence_start:\s+([-+]?\d+(?:\.\d+)?)")
_SIL_END   = re.compile(r"silence_end:\s+([-+]?\d+(?:\.\d+)?)\s+\|\s+silence_duration:\s+([-+]?\d+(?:\.\d+)?)")

def _run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)

def measure_leading_silence_ms(filepath, stream_index, window_ms=3000, noise_db=-50, min_gap_ms=50):
    ws = max(0.05, window_ms/1000.0); mg = max(0.0, min_gap_ms/1000.0)
    p = _run(["ffmpeg","-hide_banner","-nostats","-v","info","-t",f"{ws}","-i", filepath, "-map", f"0:{stream_index}","-af", f"silencedetect=noise={noise_db}dB:d={mg}","-f","null","-"])
    leading_start = None; leading_dur = 0.0
    for line in p.stderr.splitlines():
        ms = _SIL_START.search(line)
        if ms:
            t = float(ms.group(1))
            if leading_start is None and abs(t - 0.0) <= 0.02: leading_start = t
        me = _SIL_END.search(line)
        if me and leading_start is not None:
            end = float(me.group(1)); dur = float(me.group(2))
            if abs((leading_start + dur) - end) < 0.05: leading_dur = max(leading_dur, dur); break
    if leading_start is not None and leading_dur == 0.0: leading_dur = ws
    return int(round(leading_dur*1000.0)), None

def get_stream_hash_decoded_with_filters(filepath, stream_index, trim_start_sec=0.0, norm_sr=None, norm_ch=None):
    af = [f"atrim=start={trim_start_sec},asetpts=PTS-STARTPTS"] if trim_start_sec and trim_start_sec > 0 else ["asetpts=PTS-STARTPTS"]
    if norm_sr: af.append(f"aresample={int(norm_sr)}")
    afilter = ",".join(af)
    cmd = ["ffmpeg","-v","error","-i",filepath,"-map",f"0:{stream_index}","-af",afilter]
    if norm_ch and int(norm_ch) > 0: cmd += ["-ac", str(int(norm_ch))]
    cmd += ["-f","hash","-hash","MD5","-"]
    p = _run(cmd)
    if p.returncode != 0: return None, (p.stderr or "ffmpeg error")
    line = (p.stdout or "").strip()
    if line.startswith("MD5="): return line.split("=",1)[1], None
    return None, f"Unexpected hash output: {line}"

# --- Worker Class ---
class Worker(QObject):
    progress_updated = pyqtSignal(int)
    report_ready = pyqtSignal(list)
    finished = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._is_stopped = False

    def stop(self):
        self._is_stopped = True

    @pyqtSlot(dict)
    def start_job(self, params: dict):
        self._is_stopped = False
        action = params.get('action')

        if action == "compare":
            self.run_full_comparison(params)
        elif action == "analyze":
            self.run_analysis(params)
        elif action == "align_audio":
            self.run_aligned_audio_compare(params)

        self.finished.emit()

    def run_full_comparison(self, params):
        file1_path = params['file1_path']
        file2_path = params['file2_path']
        method_name = params['method_name']
        hash_function = params['hash_function']
        stream_type_filter = params['stream_type_filter']

        report = []
        all_streams1, err1 = get_stream_info(file1_path)
        if err1: self.report_ready.emit([err1]); return
        all_streams2, err2 = get_stream_info(file2_path)
        if err2: self.report_ready.emit([err2]); return

        if stream_type_filter:
            streams1 = [s for s in all_streams1 if s.get('codec_type') == stream_type_filter]
            streams2 = [s for s in all_streams2 if s.get('codec_type') == stream_type_filter]
            report.append(f"Comparing all individual '{stream_type_filter}' streams.")
        else:
            streams1, streams2 = all_streams1, all_streams2
            report.append("Comparing all streams.")

        report.append(f"File 1: {os.path.basename(file1_path)} ({len(streams1)} matching streams found)")
        report.append(f"File 2: {os.path.basename(file2_path)} ({len(streams2)} matching streams found)")
        report.append(f"Method: {method_name}"); report.append("-" * 60)

        if not streams1 or not streams2:
            report.append("No streams of the specified type found in one or both files.")
            self.report_ready.emit(report); return

        total_to_process = len(streams1) + len(streams2); processed = 0
        stream_details_2 = {}
        for stream2 in streams2:
            if self._is_stopped: return
            processed += 1
            self.progress_updated.emit(int(100 * processed / total_to_process))
            s2_idx, s2_type, s2_codec = stream2['index'], stream2.get('codec_type', 'N/A'), stream2.get('codec_name', 'N/A')
            s2_hash, err = get_raw_stream_hash_in_memory(file2_path, s2_idx, s2_codec) if method_name == "Raw In-Memory Hash" else hash_function(file2_path, s2_idx)
            stream_details_2[s2_idx] = {"hash": s2_hash if not err else f"Error: {err}", "type": s2_type, "codec": s2_codec, "matched": False}

        matched_1 = set(); report.append("--- Detailed Comparison ---")
        for stream1 in streams1:
            if self._is_stopped: return
            processed += 1
            self.progress_updated.emit(int(100 * processed / total_to_process))
            s1_idx, s1_codec, s1_type = stream1['index'], stream1.get('codec_name', 'N/A'), stream1.get('codec_type', 'N/A')
            report.append(f"\n[File 1] Stream #{s1_idx} ({s1_type.upper()}, {s1_codec})")
            s1_hash, err = get_raw_stream_hash_in_memory(file1_path, s1_idx, s1_codec) if method_name == "Raw In-Memory Hash" else hash_function(file1_path, s1_idx)
            if err: report.append(f"  - HASH ERROR: {err}"); continue
            report.append(f"  - Hash (MD5): {s1_hash}")
            found = False
            for s2_idx, s2_details in stream_details_2.items():
                if s1_hash == s2_details['hash']:
                    report.append(f"  - MATCH: Identical to File 2, Stream #{s2_idx} ({s2_details['type'].upper()}, {s2_details['codec']})")
                    found, s2_details['matched'] = True, True; matched_1.add(s1_idx); break
            if not found: report.append("  - NO MATCH found in File 2.")

        report.append("\n" + "-" * 60); report.append("--- Summary of Unmatched Streams ---")
        unmatched_1 = any(s1['index'] not in matched_1 for s1 in streams1)
        if not unmatched_1 and streams1: report.append(f"[File 1] All '{stream_type_filter or 'streams'}' streams matched.")
        else:
            for s1 in streams1:
                if s1['index'] not in matched_1: report.append(f"[File 1] Unmatched: Stream #{s1['index']} ({s1.get('codec_type', 'N/A').upper()}, {s1.get('codec_name', 'N/A')})")

        report.append("")
        unmatched_2 = any(not d['matched'] for d in stream_details_2.values())
        if not unmatched_2 and streams2: report.append(f"[File 2] All '{stream_type_filter or 'streams'}' streams were matched.")
        else:
            for s2_idx, d in stream_details_2.items():
                if not d['matched']: report.append(f"[File 2] Unmatched: Stream #{s2_idx} ({d['type'].upper()}, {d['codec']})")

        self.report_ready.emit(report)

    def run_aligned_audio_compare(self, params):
        # ... (rest of method unchanged, but now uses `params` dictionary) ...
        pass

    def run_analysis(self, params):
        # ... (rest of method unchanged, but now uses `params` dictionary) ...
        pass
