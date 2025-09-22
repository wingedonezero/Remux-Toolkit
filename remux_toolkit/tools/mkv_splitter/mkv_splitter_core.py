# remux_toolkit/tools/mkv_splitter/mkv_splitter_core.py

import json
import subprocess
from collections import Counter
from datetime import timedelta
import os
import xml.etree.ElementTree as ET
import tempfile
import statistics

def run_command(command, tool_name, capture_json=True):
    try:
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        result = subprocess.run(
            command, check=True, capture_output=True, text=True,
            encoding='utf-8', startupinfo=startupinfo
        )
        if not capture_json: return None, None
        return json.loads(result.stdout), None
    except FileNotFoundError:
        return None, f"Error: '{tool_name}' not found. Is mkvtoolnix installed and in your system's PATH?"
    except subprocess.CalledProcessError as e:
        return None, f"Error executing command: {' '.join(command)}\n{tool_name} stderr: {e.stderr}"
    except json.JSONDecodeError as e:
        return None, f"Error: Could not parse JSON output from {tool_name}.\nRaw output: {result.stdout}"

def get_chapter_info(file_path):
    mkvmerge_command = ["mkvmerge", "-J", file_path]
    container_info, error = run_command(mkvmerge_command, "mkvmerge")
    if error: return None, error
    with tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix=".xml") as tmp:
        temp_xml_path = tmp.name
    chapters, error_msg = [], None
    try:
        mkvextract_command = ["mkvextract", file_path, "chapters", temp_xml_path]
        _, error = run_command(mkvextract_command, "mkvextract", capture_json=False)
        if error:
            if os.path.exists(temp_xml_path): os.remove(temp_xml_path)
            # This is not a fatal error, could just be a file with no chapters
            return container_info, None
        tree = ET.parse(temp_xml_path)
        root = tree.getroot()
        ns = {'c': 'urn:matroskachapters'}
        chapter_atoms = root.findall('.//c:ChapterAtom', ns)
        if not chapter_atoms: chapter_atoms = root.findall('.//ChapterAtom')
        for atom in chapter_atoms:
            start_time_element = atom.find('c:ChapterTimeStart', ns)
            if start_time_element is None: start_time_element = atom.find('ChapterTimeStart')
            if start_time_element is not None:
                chapters.append({'properties': {'time_start': start_time_element.text}})
    except Exception as e:
        error_msg = f"An unexpected error occurred while parsing chapters: {e}"
    finally:
        if os.path.exists(temp_xml_path): os.remove(temp_xml_path)
    if error_msg: return None, error_msg
    container_info['chapters'] = chapters
    return container_info, None

def parse_time(time_str):
    parts = time_str.split(':')
    h, m = int(parts[0]), int(parts[1])
    s_ms_part = parts[2]
    if '.' in s_ms_part:
        s, ms_ns = s_ms_part.split('.')
        ms = ms_ns.ljust(6, '0')[:6]
    else:
        s, ms = s_ms_part, '0'
    return timedelta(hours=h, minutes=m, seconds=int(s), microseconds=int(ms))

def analyze_chapters(mkv_info, min_duration, num_episodes, analysis_mode, target_duration, input_file_path):
    analysis_log, split_points = [], []
    chapters = mkv_info.get("chapters", [])
    container_duration_ns = mkv_info.get("container", {}).get("properties", {}).get("duration", 0)
    if not chapters: return "❌ No chapters found in this file.", ""
    if container_duration_ns == 0: return "❌ Could not determine container duration.", ""
    container_duration = timedelta(microseconds=container_duration_ns / 1000)
    analysis_log.append("--- Step 1: Chapter Analysis ---")
    chapter_durations = []
    for i, chapter in enumerate(chapters):
        start_time_str = chapter.get("properties", {}).get("time_start")
        if not start_time_str: continue
        start_time = parse_time(start_time_str)
        end_time = container_duration
        if i + 1 < len(chapters):
            next_chapter = chapters[i + 1]
            end_time_str = next_chapter.get("properties", {}).get("time_start")
            if end_time_str: end_time = parse_time(end_time_str)
        duration = end_time - start_time
        chapter_durations.append({"num": i + 1, "duration_min": duration.total_seconds() / 60})
        analysis_log.append(f"  Chapter {i+1:<3} | Duration: {duration.total_seconds() / 60:.2f} minutes")

    if analysis_mode == "Time-based Grouping":
        analysis_log.append(f"\n--- Step 2 (Time-based): Learning Episode Structure ---")
        current_sum, start_chapter_index, learned_duration = 0.0, 0, target_duration
        while start_chapter_index < len(chapter_durations):
            found_episode_break = False
            for i in range(start_chapter_index, len(chapter_durations)):
                current_sum += chapter_durations[i]['duration_min']
                if current_sum >= learned_duration - 5.0:
                    for j in range(i, len(chapter_durations)):
                        if chapter_durations[j]['duration_min'] < 1.0:
                            split_point = chapter_durations[j]['num'] + 1
                            if split_point > len(chapter_durations): continue
                            episode_block_duration = sum(c['duration_min'] for c in chapter_durations[start_chapter_index : j+1])
                            if not split_points:
                                learned_duration = episode_block_duration
                                analysis_log.append(f"  ✅ Learned first episode duration: {learned_duration:.2f} min.")
                            analysis_log.append(f"  Episode block [{start_chapter_index+1}-{chapter_durations[j]['num']}] duration: {episode_block_duration:.2f} min.")
                            analysis_log.append(f"  Found credits at Chapter {chapter_durations[j]['num']}. Splitting after.")
                            split_points.append(split_point)
                            start_chapter_index, current_sum, found_episode_break = j + 1, 0.0, True
                            break
                    if found_episode_break: break
            if not found_episode_break: break
    else:
        analysis_log.append(f"\n--- Step 2: Finding Main Content (Min Duration > {min_duration} min) ---")
        long_chapters = [ch for ch in chapter_durations if ch["duration_min"] > min_duration]
        if not long_chapters:
            analysis_log.append(f"❌ No chapters found longer than {min_duration} minutes.")
            return "\n".join(analysis_log), ""
        main_content_chapter_nums = {ch['num'] for ch in long_chapters}
        analysis_log.append("Found potential main content chapters: " + ", ".join(str(n) for n in sorted(list(main_content_chapter_nums))))
        sorted_main_nums = sorted(list(main_content_chapter_nums))

        if analysis_mode == "Pattern Recognition":
            analysis_log.append("\n--- Step 3 (Pattern Recognition): Creating Chapter Signature ---")
            signature = "".join("L" if ch['num'] in main_content_chapter_nums else "S" if ch['duration_min'] < 2.5 else "M" for ch in chapter_durations)
            analysis_log.append(f"  Generated Signature: {signature}")
            analysis_log.append("\n--- Step 4 (Pattern Recognition): Finding Repeating Pattern ---")
            best_pattern = ""
            for p_len in range(1, len(signature) // 2 + 1):
                pattern = signature[:p_len]; num_consecutive_matches = 1
                for i in range(p_len, len(signature) - p_len + 1, p_len):
                    if signature[i:i+p_len] == pattern: num_consecutive_matches += 1
                    else: break
                if (num_consecutive_matches * p_len) >= (len(signature) * 0.75): best_pattern = pattern; break
            if best_pattern:
                analysis_log.append(f"  ✅ Found repeating pattern: '{best_pattern}' (length: {len(best_pattern)})")
                if best_pattern.count('L') > 1: analysis_log.append(f"  ℹ️ Pattern contains {best_pattern.count('L')} main content chapters. Treating as a single multi-part episode.")
                for i in range(len(best_pattern), len(signature), len(best_pattern)):
                    if i < len(signature): split_points.append(i + 1)
            else: analysis_log.append("  ❌ Could not determine a confident repeating pattern.")

        elif analysis_mode == "Statistical Gap Analysis":
            analysis_log.append("\n--- Step 3 (Statistical Gap): Finding Episode Gaps ---")
            gaps = [{'duration': sum(ch['duration_min'] for ch in chapter_durations if ch['num'] >= sorted_main_nums[i]+1 and ch['num'] <= sorted_main_nums[i+1]-1), 'end_chapter': sorted_main_nums[i+1]-1} for i in range(len(sorted_main_nums)-1) if sorted_main_nums[i+1] > sorted_main_nums[i]+1]
            for gap in gaps: analysis_log.append(f"  Gap between main content {gap['end_chapter']-len(gap.get('chapters',[]))+1} & {gap['end_chapter']+2}. Duration: {gap['duration']:.2f} min.")
            if len(gaps) > 1:
                gap_durations = [g['duration'] for g in gaps]; mean_duration, stdev_duration = statistics.mean(gap_durations), statistics.stdev(gap_durations); threshold = mean_duration + (1.5 * stdev_duration)
                analysis_log.append(f"\n  Gap stats: Avg={mean_duration:.2f}, StdDev={stdev_duration:.2f}"); analysis_log.append(f"  Identifying splits as gaps > threshold of {threshold:.2f} min.")
                for gap in gaps:
                    if gap['duration'] > threshold: split_points.append(gap['end_chapter'] + 1)
            elif len(gaps) == 1: analysis_log.append("  Only one gap found, assuming it's the split point."); split_points.append(gaps[0]['end_chapter'] + 1)

        elif analysis_mode == "Shortest Chapter Analysis":
            analysis_log.append("\n--- Step 3 (Shortest Chapter): Grouping and Finding Splits ---")
            groups = []
            if sorted_main_nums:
                current_group = [sorted_main_nums[0]]
                for i in range(1, len(sorted_main_nums)):
                    if sorted_main_nums[i] == sorted_main_nums[i-1] + 1: current_group.append(sorted_main_nums[i])
                    else: groups.append(current_group); current_group = [sorted_main_nums[i]]
                groups.append(current_group)
            analysis_log.append("Detected main content groups: " + str(groups))
            if len(groups) > 1:
                for i in range(len(groups) - 1):
                    gap_chapters = [ch for ch in chapter_durations if ch['num'] in range(groups[i][-1] + 1, groups[i+1][0])]
                    if not gap_chapters: continue
                    min_duration_chapter = min(gap_chapters, key=lambda x: x['duration_min'])
                    analysis_log.append(f"  Shortest chapter in gap is Chapter {min_duration_chapter['num']}"); split_points.append(min_duration_chapter['num'] + 1)

        elif analysis_mode == "Manual Episode Count":
            analysis_log.append(f"\n--- Step 3 (Manual): Clustering into {num_episodes} Episodes ---")
            gaps = [{'size': sorted_main_nums[i+1] - sorted_main_nums[i], 'start_chapter': sorted_main_nums[i+1]} for i in range(len(sorted_main_nums) - 1) if sorted_main_nums[i+1] - sorted_main_nums[i] > 1]
            if len(gaps) < num_episodes - 1:
                analysis_log.append(f"⚠️ Warning: Found {len(gaps)} gaps, but expected {num_episodes - 1}.")
                split_points = [g['start_chapter'] for g in gaps]
            else:
                largest_gaps = sorted(gaps, key=lambda x: x['size'], reverse=True)[:num_episodes - 1]; split_points = sorted([g['start_chapter'] for g in largest_gaps])

    analysis_log.append("\n--- Final Step: Finalizing Split Points ---")
    analysis_log.append(f"Final split points (chapter numbers to split BEFORE): {split_points if split_points else 'None'}")
    analysis_log.append(f"\n✅ Total Episodes Found: {len(split_points) + 1}")
    if not split_points: return "\n".join(analysis_log) + "\n\nℹ️ No split points found.", ""

    output_dir = os.path.dirname(input_file_path)
    base_name = os.path.splitext(os.path.basename(input_file_path))[0]
    output_pattern = os.path.join(output_dir, f"{base_name} - S01E%02d.mkv")
    split_string = ",".join(str(ch-1) for ch in split_points) # mkvmerge splits by chapter number-1
    final_command = f'mkvmerge -o "{output_pattern}" --split chapters:{split_string} "{input_file_path}"'
    return "\n".join(analysis_log), final_command
