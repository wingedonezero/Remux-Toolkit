# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/chapter_normalization.py
import xml.etree.ElementTree as ET
from pathlib import Path

class ChapterNormalizationStep:
    """Normalize and fix chapter markers after extraction."""

    def __init__(self, config):
        self.config = config

    def run(self, context: dict, log_emitter, stop_event) -> bool:
        """Normalize chapters to fix overlaps, gaps, and invalid timecodes."""
        step_info = context.get('step_info', '[STEP]')

        # Only run if we have chapters
        if not context.get('chapters_ok', False):
            return True

        log_emitter(f"{step_info} Normalizing and correcting chapter markers...")

        chap_xml_path = context.get('mod_chap_xml_path')
        if not chap_xml_path or not chap_xml_path.exists():
            log_emitter("  -> No chapter file to normalize")
            return True

        # Parse the chapter XML
        try:
            tree = ET.parse(chap_xml_path)
            root = tree.getroot()

            edition = root.find('.//EditionEntry')
            if not edition:
                log_emitter("  -> No edition entry found in chapters")
                return True

            atoms = edition.findall('ChapterAtom')
            if not atoms:
                log_emitter("  -> No chapter atoms found")
                return True

            # Extract chapter data for processing
            chapters = []
            for atom in atoms:
                start_elem = atom.find('ChapterTimeStart')
                end_elem = atom.find('ChapterTimeEnd')
                display = atom.find('ChapterDisplay')
                name_elem = display.find('ChapterString') if display else None

                if start_elem is not None and end_elem is not None:
                    chapters.append({
                        'atom': atom,
                        'start': self._time_to_seconds(start_elem.text),
                        'end': self._time_to_seconds(end_elem.text),
                        'name': name_elem.text if name_elem is not None else ""
                    })

            log_emitter(f"  -> Found {len(chapters)} chapters to normalize")

            # Apply video delay if needed
            video_delay = context.get('timing_analysis', {}).get('adjustments', {}).get('video_delay', 0)
            if video_delay > 0:
                delay_seconds = video_delay / 1000.0
                log_emitter(f"  -> Adjusting chapters for {video_delay}ms video delay")
                for ch in chapters:
                    ch['start'] += delay_seconds
                    ch['end'] += delay_seconds

            # Sort by start time
            chapters.sort(key=lambda x: x['start'])

            # Fix issues
            fixed_chapters = self._fix_chapter_issues(chapters, log_emitter)

            # Rebuild the XML with fixed chapters
            # Clear existing atoms
            for atom in atoms:
                edition.remove(atom)

            # Add fixed chapters
            for i, ch in enumerate(fixed_chapters):
                atom = ET.SubElement(edition, 'ChapterAtom')

                # Time elements
                start = ET.SubElement(atom, 'ChapterTimeStart')
                start.text = self._seconds_to_time(ch['start'])

                end = ET.SubElement(atom, 'ChapterTimeEnd')
                end.text = self._seconds_to_time(ch['end'])

                # Flags
                ET.SubElement(atom, 'ChapterFlagHidden').text = '0'
                ET.SubElement(atom, 'ChapterFlagEnabled').text = '1'

                # Display name
                display = ET.SubElement(atom, 'ChapterDisplay')
                name = ET.SubElement(display, 'ChapterString')
                name.text = f"Chapter {i+1:02d}"  # Renumber sequentially
                ET.SubElement(display, 'ChapterLanguage').text = 'eng'

            # Write the fixed XML
            ET.indent(tree, space="  ")
            tree.write(chap_xml_path, encoding='UTF-8', xml_declaration=True)

            log_emitter(f"  -> Normalized {len(fixed_chapters)} chapters successfully")

        except Exception as e:
            log_emitter(f"!! ERROR: Failed to normalize chapters: {e}")
            context['chapters_ok'] = False
            return False

        return True

    def _fix_chapter_issues(self, chapters, log_emitter):
        """Fix overlaps, gaps, and invalid timecodes."""
        if not chapters:
            return chapters

        fixed = []
        issues_found = []

        for i, ch in enumerate(chapters):
            # Check for invalid times
            if ch['start'] < 0:
                issues_found.append(f"Chapter {i+1} had negative start time")
                ch['start'] = 0

            if ch['end'] <= ch['start']:
                issues_found.append(f"Chapter {i+1} had end <= start")
                # Give it at least 1 second duration
                ch['end'] = ch['start'] + 1

            # Check for overlap with previous chapter
            if fixed and ch['start'] < fixed[-1]['end']:
                overlap = fixed[-1]['end'] - ch['start']
                issues_found.append(f"Chapter {i+1} overlapped previous by {overlap:.2f}s")

                # Fix by adjusting the previous chapter's end
                midpoint = (fixed[-1]['end'] + ch['start']) / 2
                fixed[-1]['end'] = midpoint
                ch['start'] = midpoint

            # Check for gap with previous chapter
            if fixed and ch['start'] > fixed[-1]['end']:
                gap = ch['start'] - fixed[-1]['end']
                if gap > 0.1:  # Only report significant gaps
                    issues_found.append(f"Gap of {gap:.2f}s before chapter {i+1}")
                # Extend previous chapter to fill gap
                fixed[-1]['end'] = ch['start']

            fixed.append(ch)

        # Log issues found
        if issues_found:
            log_emitter("  -> Fixed chapter issues:")
            for issue in issues_found[:5]:  # Limit to first 5 issues
                log_emitter(f"     - {issue}")
            if len(issues_found) > 5:
                log_emitter(f"     ... and {len(issues_found)-5} more issues")
        else:
            log_emitter("  -> No chapter issues found")

        return fixed

    def _time_to_seconds(self, time_str):
        """Convert HH:MM:SS.nnnnnnnnn to seconds."""
        try:
            parts = time_str.split(':')
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
            return hours * 3600 + minutes * 60 + seconds
        except:
            return 0

    def _seconds_to_time(self, seconds):
        """Convert seconds to HH:MM:SS.nnnnnnnnn format."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:012.9f}"
