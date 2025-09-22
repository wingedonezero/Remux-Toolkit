# remux_toolkit/tools/media_comparator/media_comparator_core.py

import subprocess
import json
import hashlib
import os
import tempfile
import re

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

# --- Core Hashing & Info Functions ---

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
    codec_to_format_map = {
        'truehd': 'truehd', 'ac3': 'ac3', 'dts': 'dts', 'aac': 'adts',
        'flac': 'flac', 'opus': 'opus', 'vorbis': 'ogg', 'h264': 'h264',
        'hevc': 'hevc', 'mpeg2video': 'mpeg2video', 'subrip': 'srt',
        'ass': 'ass', 'dvd_subtitle': 'vobsub',
        'pcm_s16le': 's16le', 'pcm_s24le': 's24le', 'pcm_s32le': 's32le'
    }
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
        self.hash_function = None; self.method_name = None; self.file1_path = None
        self.file2_path = None; self.stream_type_filter = None; self.align_apply_to = "file1"
        self.align_offset_ms = 0; self.align_auto = True; self.align_norm_sr = 48000
        self.align_norm_ch = None; self.align_win_ms = 3000; self.align_noise_db = -50
        self.align_min_gap_ms = 50; self.align_idx1 = 0; self.align_idx2 = 0

    @pyqtSlot()
    def run_full_comparison(self):
        if self.method_name == "Aligned Full Decode (Audio)": self._run_aligned_audio_compare(); return
        report = []; all_streams1, err1 = get_stream_info(self.file1_path)
        if err1: self.report_ready.emit([err1]); self.finished.emit(); return
        all_streams2, err2 = get_stream_info(self.file2_path)
        if err2: self.report_ready.emit([err2]); self.finished.emit(); return
        if self.stream_type_filter:
            streams1 = [s for s in all_streams1 if s.get('codec_type') == self.stream_type_filter]; streams2 = [s for s in all_streams2 if s.get('codec_type') == self.stream_type_filter]
            report.append(f"Comparing all individual '{self.stream_type_filter}' streams.")
        else: streams1, streams2 = all_streams1, all_streams2; report.append("Comparing all streams.")
        report.append(f"File 1: {os.path.basename(self.file1_path)} ({len(streams1)} matching streams found)"); report.append(f"File 2: {os.path.basename(self.file2_path)} ({len(streams2)} matching streams found)")
        report.append(f"Method: {self.method_name}"); report.append("-" * 60)
        if not streams1 or not streams2: report.append("No streams of the specified type found in one or both files."); self.report_ready.emit(report); self.finished.emit(); return
        total_streams_to_process = len(streams1) + len(streams2); processed_streams = 0
        stream_details_2 = {}
        for stream2 in streams2:
            processed_streams += 1; self.progress_updated.emit(int(100 * processed_streams / total_streams_to_process))
            s2_index, s2_codec_type, s2_codec_name = stream2['index'], stream2.get('codec_type', 'N/A'), stream2.get('codec_name', 'N/A')
            s2_hash, err = get_raw_stream_hash_in_memory(self.file2_path, s2_index, s2_codec_name) if self.method_name == "Raw In-Memory Hash" else self.hash_function(self.file2_path, s2_index)
            stream_details_2[s2_index] = {"hash": s2_hash if not err else f"Error: {err}", "type": s2_codec_type, "codec": s2_codec_name, "matched": False}
        matched_streams_1 = set(); stream_details_1 = {}; report.append("--- Detailed Comparison ---")
        for stream1 in streams1:
            processed_streams += 1; self.progress_updated.emit(int(100 * processed_streams / total_streams_to_process))
            s1_index, s1_codec, s1_type = stream1['index'], stream1.get('codec_name', 'N/A'), stream1.get('codec_type', 'N/A')
            report.append(f"\n[File 1] Stream #{s1_index} ({s1_type.upper()}, {s1_codec})")
            s1_hash, err = get_raw_stream_hash_in_memory(self.file1_path, s1_index, s1_codec) if self.method_name == "Raw In-Memory Hash" else self.hash_function(self.file1_path, s1_index)
            stream_details_1[s1_index] = {"hash": s1_hash if not err else f"HASH ERROR: {err}"}
            if err: report.append(f"  - HASH ERROR: {err}"); continue
            report.append(f"  - Hash (MD5): {s1_hash}")
            found_in_file2 = False
            for s2_index, s2_details in stream_details_2.items():
                if s1_hash == s2_details['hash']:
                    report.append(f"  - MATCH: Identical to File 2, Stream #{s2_index} ({s2_details['type'].upper()}, {s2_details['codec']})")
                    found_in_file2, stream_details_2[s2_index]['matched'], _ = True, True, matched_streams_1.add(s1_index); break
            if not found_in_file2: report.append("  - NO MATCH found in File 2.")
        report.append("\n" + "-" * 60); report.append("--- Summary of Unmatched Streams ---")
        unmatched_in_1_found = any(s1['index'] not in matched_streams_1 for s1 in streams1)
        if not unmatched_in_1_found and streams1: report.append(f"[File 1] All '{self.stream_type_filter or 'streams'}' streams found a match in File 2.")
        else:
            for s1 in streams1:
                if s1['index'] not in matched_streams_1: report.append(f"[File 1] Unmatched: Stream #{s1['index']} ({s1.get('codec_type', 'N/A').upper()}, {s1.get('codec_name', 'N/A')})")
        report.append("")
        unmatched_in_2_found = any(not s2_details['matched'] for s2_details in stream_details_2.values())
        if not unmatched_in_2_found and streams2: report.append(f"[File 2] All '{self.stream_type_filter or 'streams'}' streams were matched by a stream in File 1.")
        else:
            for s2_index, s2_details in stream_details_2.items():
                if not s2_details['matched']: report.append(f"[File 2] Unmatched: Stream #{s2_index} ({s2_details['type'].upper()}, {s2_details['codec']})")
        self.report_ready.emit(report); self.finished.emit()

    def _run_aligned_audio_compare(self):
        report = []; s1_all, e1 = get_stream_info(self.file1_path); s2_all, e2 = get_stream_info(self.file2_path)
        if e1: self.report_ready.emit([e1]); self.finished.emit(); return
        if e2: self.report_ready.emit([e2]); self.finished.emit(); return
        a1 = [s for s in s1_all if s.get('codec_type') == 'audio']; a2 = [s for s in s2_all if s.get('codec_type') == 'audio']
        report.append("Comparing all individual 'audio' streams with decode→align→hash."); report.append(f"File 1: {os.path.basename(self.file1_path)} ({len(a1)} audio streams)"); report.append(f"File 2: {os.path.basename(self.file2_path)} ({len(a2)} audio streams)"); report.append(f"Method: Aligned Full Decode (Audio)"); report.append("-"*60)
        if not a1 or not a2: report.append("No audio streams in one or both files."); self.report_ready.emit(report); self.finished.emit(); return
        idx1 = self.align_idx1 if any(int(s['index']) == int(self.align_idx1) for s in a1) else a1[0]['index']
        idx2 = self.align_idx2 if any(int(s['index']) == int(self.align_idx2) for s in a2) else a2[0]['index']
        offset_sec = max(0.0, (self.align_offset_ms or 0)/1000.0)
        if self.align_auto:
            L1_ms, _ = measure_leading_silence_ms(self.file1_path, idx1, self.align_win_ms, self.align_noise_db, self.align_min_gap_ms)
            L2_ms, _ = measure_leading_silence_ms(self.file2_path, idx2, self.align_win_ms, self.align_noise_db, self.align_min_gap_ms)
            report.append(f"Auto-detect: leading silence  F1(idx {idx1})={L1_ms} ms,  F2(idx {idx2})={L2_ms} ms"); diff = abs(L1_ms - L2_ms)
            if diff >= 100:
                if L1_ms > L2_ms: offset_sec = (L1_ms - L2_ms)/1000.0; self.align_apply_to = "file1"; report.append(f"Auto chose: trim {offset_sec:.3f}s from File 1.")
                else: offset_sec = (L2_ms - L1_ms)/1000.0; self.align_apply_to = "file2"; report.append(f"Auto chose: trim {offset_sec:.3f}s from File 2.")
            else: report.append("Auto-detect: inconclusive (<100 ms). Using manual offset setting if provided.")
        norm_sr = self.align_norm_sr if (self.align_norm_sr and self.align_norm_sr>0) else None; norm_ch = self.align_norm_ch if (self.align_norm_ch and self.align_norm_ch>0) else None
        report.append(f"Alignment plan: trim {offset_sec:.3f}s from {'File 1' if self.align_apply_to=='file1' else 'File 2'}; normalize sr={norm_sr or 'keep'}, ch={norm_ch or 'keep'}"); report.append("--- Detailed Comparison ---")
        stream_details_2 = {}
        for st2 in a2: s2_idx = st2['index']; s2_codec = st2.get('codec_name','N/A'); t2 = offset_sec if self.align_apply_to == "file2" else 0.0; h2, e2 = get_stream_hash_decoded_with_filters(self.file2_path, s2_idx, t2, norm_sr, norm_ch); stream_details_2[s2_idx] = {"hash": h2 if not e2 else f"Error: {e2}", "codec": s2_codec, "matched": False}
        matched_1 = set()
        for st1 in a1:
            s1_idx = st1['index']; s1_codec = st1.get('codec_name','N/A'); report.append(f"\n[File 1] Audio stream #{s1_idx} ({s1_codec})"); t1 = offset_sec if self.align_apply_to == "file1" else 0.0; h1, e1 = get_stream_hash_decoded_with_filters(self.file1_path, s1_idx, t1, norm_sr, norm_ch)
            if e1: report.append(f"  - HASH ERROR: {e1}"); continue
            report.append(f"  - Hash (MD5): {h1}"); found = False
            for s2_idx, d in stream_details_2.items():
                if h1 == d['hash']: report.append(f"  - MATCH: File 2 stream #{s2_idx} ({d['codec']})"); d['matched'] = True; matched_1.add(s1_idx); found = True; break
            if not found: report.append("  - NO MATCH found in File 2.")
        report.append("\n" + "-"*60); report.append("--- Summary of Unmatched Streams ---")
        any_unmatched1 = not all(st1['index'] in matched_1 for st1 in a1)
        if not any_unmatched1: report.append("[File 1] All audio streams matched after alignment.")
        else:
            for st1 in a1:
                if st1['index'] not in matched_1: report.append(f"[File 1] Unmatched: Stream #{st1['index']} ({st1.get('codec_name','N/A')})")
        any_unmatched2 = any(not d['matched'] for d in stream_details_2.values())
        if not any_unmatched2: report.append("[File 2] All audio streams were matched by a stream in File 1.")
        else:
            for s2_idx, d in stream_details_2.items():
                if not d['matched']: report.append(f"[File 2] Unmatched: Stream #{s2_idx} ({d['codec']})  MD5={d['hash']}")
        self.report_ready.emit(report); self.finished.emit()

    @pyqtSlot()
    def run_analysis(self):
        report = []
        def _process_file(filepath):
            file_report = []; file_report.append(f"--- Analysis for: {os.path.basename(filepath)} ---")
            all_streams, err = get_stream_info(filepath)
            if err: file_report.append(f"  Error: {err}"); return file_report
            streams_to_analyze = [s for s in all_streams if s.get('codec_type') == self.stream_type_filter]
            if not streams_to_analyze: file_report.append(f"No '{self.stream_type_filter}' streams found to analyze."); return file_report
            for stream in streams_to_analyze:
                stream_index, codec_name = stream['index'], stream['codec_name']
                file_report.append(f"\nAnalyzing Stream #{stream_index} ({codec_name.upper()})")
                raw_hash, raw_err = get_raw_stream_hash_in_memory(filepath, stream_index, codec_name); file_report.append(f"  - Raw In-Memory Hash: {'ERROR - ' + raw_err if raw_err else raw_hash}")
                extract_hash, extract_err = get_mkvextract_hash(filepath, stream_index); file_report.append(f"  - Extracted File Hash: {'ERROR - ' + extract_err if extract_err else extract_hash}")
                if not raw_err and not extract_err: file_report.append("  - RESULT: Hashes match. No container delay is affecting extraction." if raw_hash == extract_hash else "  - RESULT: Hashes DO NOT match. A container delay is present.")
            return file_report
        if self.file1_path: report.extend(_process_file(self.file1_path))
        if self.file2_path: report.append("\n" + "="*40 + "\n"); report.extend(_process_file(self.file2_path))
        self.report_ready.emit(report); self.finished.emit()
