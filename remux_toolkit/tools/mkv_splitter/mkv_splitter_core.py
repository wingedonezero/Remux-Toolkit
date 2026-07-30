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

def get_mkv_info(file_path):
    """Gets full container and chapter info for an MKV file."""
    mkvmerge_command = ["mkvmerge", "-J", file_path]
    container_info, error = run_command(mkvmerge_command, "mkvmerge")
    if error: return None, error

    # Use a temporary file to extract chapters XML
    with tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix=".xml") as tmp:
        temp_xml_path = tmp.name

    chapters, error_msg = [], None
    try:
        mkvextract_command = ["mkvextract", file_path, "chapters", temp_xml_path]
        _, error = run_command(mkvextract_command, "mkvextract", capture_json=False)
        if error:
            # Not a fatal error, the file might just not have chapters
            pass
        else:
            tree = ET.parse(temp_xml_path)
            root = tree.getroot()
            ns = {'c': 'urn:matroskachapters'}
            chapter_atoms = root.findall('.//c:ChapterAtom', ns)
            if not chapter_atoms: chapter_atoms = root.findall('.//ChapterAtom')
            for atom in chapter_atoms:
                start_time_element = atom.find('c:ChapterTimeStart', ns)
                if start_time_element is None: start_time_element = atom.find('ChapterTimeStart')

                # Also try to get chapter title
                title_element = atom.find('c:ChapterDisplay/c:ChapterString', ns)
                if title_element is None: title_element = atom.find('ChapterDisplay/ChapterString')
                title = title_element.text if title_element is not None else ""

                if start_time_element is not None:
                    chapters.append({
                        'properties': {
                            'time_start': start_time_element.text,
                            'title': title
                        }
                    })
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

def is_likely_credits(duration_min, position, chapter_title=""):
    """
    Improved credits detection using duration, position, and title hints.

    Args:
        duration_min: Chapter duration in minutes
        position: 'start', 'middle', or 'end' relative to main content
        chapter_title: Optional chapter title for additional hints
    """
    title_lower = chapter_title.lower()

    # Check title hints first
    credit_keywords = ['credit', 'ending', 'ed', 'outro']
    opening_keywords = ['opening', 'op', 'intro', 'preview']

    if any(keyword in title_lower for keyword in credit_keywords):
        return position == 'end' and 0.3 < duration_min < 3.0

    if any(keyword in title_lower for keyword in opening_keywords):
        return position == 'start' and 0.5 < duration_min < 4.0

    # Duration-based detection
    if position == 'end':
        # Ending credits: typically 0.5-2.5 minutes
        return 0.3 < duration_min < 2.5
    elif position == 'start':
        # Opening credits/recap: typically 0.5-4 minutes
        return 0.5 < duration_min < 4.0
    elif position == 'middle':
        # Mid-roll credits (rare): very short
        return duration_min < 0.5

    return False

def fuzzy_pattern_match(pattern1, pattern2, allowed_mismatches=1):
    """
    Compare two patterns allowing for minor variations.

    Args:
        pattern1: First pattern string
        pattern2: Second pattern string
        allowed_mismatches: Number of character differences allowed
    """
    if len(pattern1) != len(pattern2):
        return False

    mismatches = sum(c1 != c2 for c1, c2 in zip(pattern1, pattern2))
    return mismatches <= allowed_mismatches

def analyze_chapters(mkv_info, min_duration, num_episodes, analysis_mode, target_duration):
    """Performs chapter analysis and returns the log and a list of split points."""
    analysis_log, split_points = [], []
    chapters = mkv_info.get("chapters", [])
    container_duration_ns = mkv_info.get("container", {}).get("properties", {}).get("duration", 0)
    if not chapters: return "❌ No chapters found in this file.", []
    if container_duration_ns == 0: return "❌ Could not determine container duration.", []

    container_duration = timedelta(microseconds=container_duration_ns / 1000)
    analysis_log.append("--- Step 1: Chapter Analysis ---")
    chapter_durations = []
    for i, chapter in enumerate(chapters):
        start_time_str = chapter.get("properties", {}).get("time_start")
        chapter_title = chapter.get("properties", {}).get("title", "")
        if not start_time_str: continue
        start_time = parse_time(start_time_str)
        end_time = container_duration
        if i + 1 < len(chapters):
            next_chapter = chapters[i + 1]
            end_time_str = next_chapter.get("properties", {}).get("time_start")
            if end_time_str: end_time = parse_time(end_time_str)
        duration = end_time - start_time
        chapter_durations.append({
            "num": i + 1,
            "start_min": start_time.total_seconds() / 60,
            "duration_min": duration.total_seconds() / 60,
            "title": chapter_title
        })
        title_display = f" ({chapter_title})" if chapter_title else ""
        analysis_log.append(f"  Chapter {i+1:<3} | Duration: {duration.total_seconds() / 60:.2f} minutes{title_display}")

    if analysis_mode == "Time-based Grouping":
        analysis_log.append(f"\n--- Step 2 (Time-based): Snapping to Chapter Boundaries ---")
        total_min = container_duration.total_seconds() / 60
        analysis_log.append(f"  Total duration: {total_min:.2f} min | Target episode length: {target_duration:.2f} min")
        analysis_log.append(f"  Estimated episodes: {total_min / target_duration:.2f}")

        # Each episode should end as close to (previous split + target) as
        # possible, snapped to a real chapter boundary. The final episode
        # absorbs any short tail (e.g. extra creditless OP/ED segments on
        # JPN discs), so it is allowed to run longer than the target.
        min_episode = target_duration * 0.5

        # Hypothesis 1: the disc is authored as N exactly equal episodes
        # (common on BDs — e.g. 6 x 23:42). If every k*(total/N) lands on a
        # real chapter boundary almost exactly, those ARE the episode
        # boundaries; no structural guessing needed or wanted.
        equal_fit = None
        n_eps = round(total_min / target_duration)
        if n_eps >= 2:
            anchor = total_min / n_eps
            fit_tol = 0.05
            picks = []
            for k in range(1, n_eps):
                near = min(chapter_durations[1:], key=lambda ch: abs(ch['start_min'] - k * anchor))
                if abs(near['start_min'] - k * anchor) > fit_tol:
                    analysis_log.append(f"  Equal-division check: no boundary within {fit_tol:.2f} min of {k * anchor:.2f} min (closest: Chapter {near['num']} at {near['start_min']:.2f}) — disc is not evenly authored.")
                    picks = None
                    break
                picks.append(near)
            if picks and len({ch['num'] for ch in picks}) == len(picks):
                equal_fit = picks

        if equal_fit:
            m = int(anchor)
            s = (anchor - m) * 60
            analysis_log.append(f"  ✅ Disc divides evenly into {n_eps} episodes of {anchor:.2f} min ({m}:{s:04.1f}) — every boundary matches exactly. Using equal-parts splits.")
            for k, ch in enumerate(equal_fit, 1):
                analysis_log.append(f"  Split {k}: Chapter {ch['num']} start at {ch['start_min']:.2f} min ({ch['start_min'] - k * anchor:+.3f} min from exact).")
                split_points.append(ch['num'])

        prev_split_min = 0.0
        prev_num = 1
        while not equal_fit:
            ideal = prev_split_min + target_duration
            # Boundaries after the previous split that wouldn't create a
            # too-short episode. Splitting before chapter N happens at N's start.
            candidates = [
                ch for ch in chapter_durations
                if ch['num'] > prev_num and ch['start_min'] >= prev_split_min + min_episode
            ]
            if not candidates:
                break

            # Pure closest boundary — no structural guessing. Chapter meanings
            # vary too much across discs (a sub-minute chapter can be a
            # trailing preview OR the next episode's cold open) for heuristics
            # to be trustworthy; the log flags near-ties for eyeballing.
            best = min(candidates, key=lambda ch: abs(ch['start_min'] - ideal))
            others = [ch for ch in candidates if ch['num'] != best['num']]
            if others:
                runner = min(others, key=lambda ch: abs(ch['start_min'] - ideal))
                if abs(runner['start_min'] - ideal) - abs(best['start_min'] - ideal) < 0.5:
                    analysis_log.append(f"  ⚠️ Near-tie: Chapter {runner['num']} at {runner['start_min']:.2f} min was almost as close as Chapter {best['num']} — worth double-checking this split.")

            remaining = total_min - best['start_min']

            if remaining < min_episode:
                # Tail is too short to be an episode (extra credits segment,
                # previews, etc.) — leave it attached to the final episode.
                analysis_log.append(f"  Remaining {remaining:.2f} min after Chapter {best['num']} is under {min_episode:.2f} min — keeping it with the last episode.")
                break

            deviation = best['start_min'] - ideal
            analysis_log.append(
                f"  Split {len(split_points) + 1}: ideal at {ideal:.2f} min → snapped to Chapter {best['num']} "
                f"start ({best['start_min']:.2f} min, {deviation:+.2f} min off). "
                f"Episode duration: {best['start_min'] - prev_split_min:.2f} min."
            )
            split_points.append(best['num'])
            prev_split_min = best['start_min']
            prev_num = best['num']

        if split_points:
            episode_durations = []
            bounds = [0.0] + [
                next(ch['start_min'] for ch in chapter_durations if ch['num'] == sp)
                for sp in split_points
            ] + [total_min]
            for k in range(len(bounds) - 1):
                episode_durations.append(bounds[k + 1] - bounds[k])
            analysis_log.append(f"\n  📊 Episode durations: " + ", ".join(f"{d:.2f}" for d in episode_durations) + " min")
            analysis_log.append(f"     Average: {statistics.mean(episode_durations):.2f} min (last episode may run long by design)")
        else:
            analysis_log.append(f"  File is not long enough for more than one episode at this target — no splits.")

    elif analysis_mode == "Pattern Recognition":
        analysis_log.append(f"\n--- Step 2: Finding Main Content (Min Duration > {min_duration} min) ---")
        long_chapters = [ch for ch in chapter_durations if ch["duration_min"] > min_duration]
        if not long_chapters:
            analysis_log.append(f"❌ No chapters found longer than {min_duration} minutes.")
            return "\n".join(analysis_log), []
        main_content_chapter_nums = {ch['num'] for ch in long_chapters}
        analysis_log.append("Found potential main content chapters: " + ", ".join(str(n) for n in sorted(list(main_content_chapter_nums))))

        analysis_log.append("\n--- Step 3 (Pattern Recognition): Enhanced Pattern Analysis ---")
        signature = "".join(
            "L" if ch['num'] in main_content_chapter_nums
            else "S" if ch['duration_min'] < 2.5
            else "M"
            for ch in chapter_durations
        )
        analysis_log.append(f"  Generated Signature: {signature}")
        analysis_log.append(f"  L = Long/Main content (>{min_duration} min)")
        analysis_log.append(f"  M = Medium content (2.5-{min_duration} min)")
        analysis_log.append(f"  S = Short content (<2.5 min)")

        analysis_log.append("\n--- Step 4 (Pattern Recognition): Finding Repeating Pattern with Fuzzy Matching ---")
        best_pattern = ""
        best_coverage = 0
        best_match_info = None

        # Start from longer patterns first (more likely to be meaningful episodes)
        # But not longer than half the signature
        for p_len in range(min(len(signature) // 2, 10), 0, -1):
            pattern = signature[:p_len]
            num_consecutive_exact = 1
            num_consecutive_fuzzy = 0

            # Check for consecutive matches (with optional fuzzy matching)
            for i in range(p_len, len(signature) - p_len + 1, p_len):
                segment = signature[i:i+p_len]

                if segment == pattern:
                    num_consecutive_exact += 1
                    if num_consecutive_fuzzy > 0:
                        # Had fuzzy matches before, add them to exact count
                        num_consecutive_exact += num_consecutive_fuzzy
                        num_consecutive_fuzzy = 0
                elif fuzzy_pattern_match(pattern, segment, allowed_mismatches=1):
                    num_consecutive_fuzzy += 1
                    analysis_log.append(f"  ~ Fuzzy match at position {i}: '{segment}' ≈ '{pattern}'")
                else:
                    # Pattern broken, stop looking
                    break

            # Total consecutive matches (exact + fuzzy before break)
            total_consecutive = num_consecutive_exact + num_consecutive_fuzzy
            coverage = (total_consecutive * p_len) / len(signature)

            # Require at least 75% coverage AND at least 2 consecutive matches for confidence
            if coverage >= 0.75 and total_consecutive >= 2:
                # Prefer longer patterns over shorter ones if coverage is similar
                if coverage > best_coverage or (coverage >= best_coverage * 0.95 and len(pattern) > len(best_pattern)):
                    best_pattern = pattern
                    best_coverage = coverage
                    best_match_info = (total_consecutive, num_consecutive_exact, num_consecutive_fuzzy)
                    analysis_log.append(f"  Candidate pattern: '{pattern}' (length: {len(best_pattern)}, consecutive matches: {total_consecutive}, coverage: {coverage*100:.1f}%)")
                    # If we have excellent coverage with a good pattern length, accept it
                    if coverage >= 0.90 and len(pattern) >= 3:
                        break

        if best_pattern:
            analysis_log.append(f"  ✅ Found repeating pattern: '{best_pattern}' (length: {len(best_pattern)}, coverage: {best_coverage*100:.1f}%)")
            if best_pattern.count('L') > 1:
                analysis_log.append(f"  ℹ️ Pattern contains {best_pattern.count('L')} main content chapters. Treating as a single multi-part episode.")

            # Generate split points based on pattern length
            for i in range(len(best_pattern), len(signature), len(best_pattern)):
                if i < len(signature):
                    split_points.append(i + 1)
        else:
            analysis_log.append("  ❌ Could not determine a confident repeating pattern (even with fuzzy matching).")

    elif analysis_mode == "Statistical Gap Analysis":
        analysis_log.append(f"\n--- Step 2: Finding Main Content (Min Duration > {min_duration} min) ---")
        long_chapters = [ch for ch in chapter_durations if ch["duration_min"] > min_duration]
        if not long_chapters:
            analysis_log.append(f"❌ No chapters found longer than {min_duration} minutes.")
            return "\n".join(analysis_log), []
        main_content_chapter_nums = {ch['num'] for ch in long_chapters}
        analysis_log.append("Found potential main content chapters: " + ", ".join(str(n) for n in sorted(list(main_content_chapter_nums))))
        sorted_main_nums = sorted(list(main_content_chapter_nums))

        analysis_log.append("\n--- Step 3 (Statistical Gap): Finding Episode Gaps ---")
        gaps = [
            {
                'duration': sum(ch['duration_min'] for ch in chapter_durations if ch['num'] >= sorted_main_nums[i]+1 and ch['num'] <= sorted_main_nums[i+1]-1),
                'end_chapter': sorted_main_nums[i+1]-1
            }
            for i in range(len(sorted_main_nums)-1)
            if sorted_main_nums[i+1] > sorted_main_nums[i]+1
        ]

        for gap in gaps:
            analysis_log.append(f"  Gap ending at chapter {gap['end_chapter']}. Duration: {gap['duration']:.2f} min.")

        if len(gaps) > 1:
            gap_durations = [g['duration'] for g in gaps]
            mean_duration = statistics.mean(gap_durations)
            stdev_duration = statistics.stdev(gap_durations)
            threshold = mean_duration + (1.5 * stdev_duration)

            analysis_log.append(f"\n  Gap stats: Avg={mean_duration:.2f}, StdDev={stdev_duration:.2f}")
            analysis_log.append(f"  Identifying splits as gaps > threshold of {threshold:.2f} min.")

            for gap in gaps:
                if gap['duration'] > threshold:
                    split_points.append(gap['end_chapter'] + 1)
        elif len(gaps) == 1:
            analysis_log.append("  Only one gap found, assuming it's the split point.")
            split_points.append(gaps[0]['end_chapter'] + 1)

    elif analysis_mode == "Shortest Chapter Analysis":
        analysis_log.append(f"\n--- Step 2: Finding Main Content (Min Duration > {min_duration} min) ---")
        long_chapters = [ch for ch in chapter_durations if ch["duration_min"] > min_duration]
        if not long_chapters:
            analysis_log.append(f"❌ No chapters found longer than {min_duration} minutes.")
            return "\n".join(analysis_log), []
        main_content_chapter_nums = {ch['num'] for ch in long_chapters}
        analysis_log.append("Found potential main content chapters: " + ", ".join(str(n) for n in sorted(list(main_content_chapter_nums))))
        sorted_main_nums = sorted(list(main_content_chapter_nums))

        analysis_log.append("\n--- Step 3 (Shortest Chapter): Grouping and Finding Splits ---")
        groups = []
        if sorted_main_nums:
            current_group = [sorted_main_nums[0]]
            for i in range(1, len(sorted_main_nums)):
                if sorted_main_nums[i] == sorted_main_nums[i-1] + 1:
                    current_group.append(sorted_main_nums[i])
                else:
                    groups.append(current_group)
                    current_group = [sorted_main_nums[i]]
            groups.append(current_group)

        analysis_log.append("Detected main content groups: " + str(groups))

        if len(groups) > 1:
            for i in range(len(groups) - 1):
                gap_chapters = [ch for ch in chapter_durations if ch['num'] in range(groups[i][-1] + 1, groups[i+1][0])]
                if not gap_chapters:
                    continue
                min_duration_chapter = min(gap_chapters, key=lambda x: x['duration_min'])
                analysis_log.append(f"  Shortest chapter in gap is Chapter {min_duration_chapter['num']} ({min_duration_chapter['duration_min']:.2f} min). Splitting before Chapter {min_duration_chapter['num'] + 1}.")
                split_points.append(min_duration_chapter['num'] + 1)
        else:
            analysis_log.append("  Only one main content group found — nothing to split. If this file should contain multiple episodes, try lowering 'Min Content Duration' (episodes chaptered as Part A/Part B may have no single chapter above the threshold).")

    elif analysis_mode == "Remove Chapters from End":
        chapters_to_remove = num_episodes  # reusing the param slot
        total_chapters = len(chapter_durations)
        analysis_log.append(f"\n--- Step 2 (Remove from End): Removing last {chapters_to_remove} chapter(s) ---")
        analysis_log.append(f"  Total chapters: {total_chapters}")

        if chapters_to_remove >= total_chapters:
            analysis_log.append(f"❌ Cannot remove {chapters_to_remove} chapters from a file with only {total_chapters} chapters.")
            return "\n".join(analysis_log), []

        split_before = total_chapters - chapters_to_remove + 1
        analysis_log.append(f"  Splitting before chapter {split_before} to keep chapters 1-{split_before - 1}")
        split_points.append(split_before)

    elif analysis_mode == "Manual Episode Count":
        analysis_log.append(f"\n--- Step 2: Finding Main Content (Min Duration > {min_duration} min) ---")
        long_chapters = [ch for ch in chapter_durations if ch["duration_min"] > min_duration]
        if not long_chapters:
            analysis_log.append(f"❌ No chapters found longer than {min_duration} minutes.")
            return "\n".join(analysis_log), []
        main_content_chapter_nums = {ch['num'] for ch in long_chapters}
        analysis_log.append("Found potential main content chapters: " + ", ".join(str(n) for n in sorted(list(main_content_chapter_nums))))
        sorted_main_nums = sorted(list(main_content_chapter_nums))

        analysis_log.append(f"\n--- Step 3 (Manual): Clustering into {num_episodes} Episodes ---")
        gaps = [
            {
                'size': sorted_main_nums[i+1] - sorted_main_nums[i],
                'start_chapter': sorted_main_nums[i+1]
            }
            for i in range(len(sorted_main_nums) - 1)
            if sorted_main_nums[i+1] - sorted_main_nums[i] > 1
        ]

        if len(gaps) < num_episodes - 1:
            analysis_log.append(f"⚠️ Warning: Found {len(gaps)} gaps, but expected {num_episodes - 1}.")
            split_points = [g['start_chapter'] for g in gaps]
        else:
            largest_gaps = sorted(gaps, key=lambda x: x['size'], reverse=True)[:num_episodes - 1]
            split_points = sorted([g['start_chapter'] for g in largest_gaps])

    analysis_log.append("\n--- Final Step: Finalizing Split Points ---")
    analysis_log.append(f"Final split points (chapter numbers to split BEFORE): {split_points if split_points else 'None'}")
    analysis_log.append(f"\n✅ Total Episodes Found: {len(split_points) + 1}")

    return "\n".join(analysis_log), split_points

def generate_mkvmerge_command(input_file_path, split_points, track_mods):
    """Generates the final mkvmerge command string including track modifications."""
    if not input_file_path:
        return ""

    output_dir = os.path.dirname(input_file_path)
    base_name = os.path.splitext(os.path.basename(input_file_path))[0]

    # When splitting by chapters, mkvmerge automatically appends -001, -002, etc.
    # Add a suffix to distinguish from the source file
    output_path = os.path.join(output_dir, f"{base_name}-split.mkv")

    command_parts = ['mkvmerge', '-o', f'"{output_path}"']

    # Add track language modifications
    for mod in track_mods:
        tid = mod.get('tid')
        lang = mod.get('language')
        if tid is not None and lang:
            command_parts.append(f'--language {tid}:"{lang}"')

    # Add split command if there are split points
    if split_points:
        split_string = ",".join(str(ch) for ch in split_points)
        command_parts.append(f'--split chapters:{split_string}')

    command_parts.append(f'"{input_file_path}"')

    return " ".join(command_parts)
