# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/ifo_parser.py
import struct
import json
from pathlib import Path
from ..utils.helpers import run_capture

class IfoParserStep:
    """Parse IFO files directly to extract DVD metadata including PGC timing data."""

    def __init__(self, config):
        self.config = config

    def run(self, context: dict, log_emitter, stop_event) -> bool:
        """Parse IFO files for the selected title to get proper metadata."""
        step_info = context.get('step_info', '[STEP]')
        log_emitter(f"{step_info} Parsing DVD IFO files for metadata...")

        input_path = Path(context['input_path'])
        title_num = context['title_num']
        out_folder = context['out_folder']

        # Initialize IFO data storage
        ifo_data = {
            'audio_tracks': [],
            'subtitle_tracks': [],
            'aspect_ratio': None,
            'palette': None,
            'chapters': [],
            'angles': 1,
            'audio_delays': {},  # Stream index -> delay in ms
            'cell_data': [],     # Cell playback info
            'pgc_data': None,    # Program Chain data
        }

        # Find VIDEO_TS folder
        if input_path.is_file() and input_path.suffix.lower() == '.iso':
            # For ISO files, we need to mount or use a different approach
            log_emitter("  -> ISO files require mounting for IFO parsing (not implemented)")
            context['ifo_data'] = ifo_data
            return True

        video_ts = input_path / "VIDEO_TS" if (input_path / "VIDEO_TS").exists() else input_path
        if not video_ts.exists():
            log_emitter("  -> No VIDEO_TS folder found, skipping IFO parsing")
            context['ifo_data'] = ifo_data
            return True

        # Parse the IFO file for this title
        try:
            ifo_data = self._parse_vts_ifo(video_ts, title_num, log_emitter)

            # Calculate audio delays from PGC data if available
            if ifo_data.get('pgc_data'):
                self._calculate_audio_delays(ifo_data, log_emitter)

        except Exception as e:
            log_emitter(f"  -> Error parsing IFO: {e}")

        # Store IFO data in context
        context['ifo_data'] = ifo_data

        # Log what we found
        if ifo_data['audio_tracks']:
            log_emitter(f"  -> Found {len(ifo_data['audio_tracks'])} audio track descriptions from IFO")
            for track in ifo_data['audio_tracks']:
                delay_info = f" [delay: {track.get('delay_ms', 0)}ms]" if track.get('delay_ms') else ""
                log_emitter(f"     Audio {track['index']}: {track.get('name', 'Unknown')}{delay_info}")
        if ifo_data['subtitle_tracks']:
            log_emitter(f"  -> Found {len(ifo_data['subtitle_tracks'])} subtitle track descriptions from IFO")
        if ifo_data['aspect_ratio']:
            log_emitter(f"  -> Aspect ratio from IFO: {ifo_data['aspect_ratio']}")
        if ifo_data.get('palette'):
            log_emitter(f"  -> Found subtitle palette data")
        if ifo_data.get('cell_data'):
            log_emitter(f"  -> Found {len(ifo_data['cell_data'])} cells with timing data")

        return True

    def _parse_vts_ifo(self, video_ts: Path, title_num: int, log_emitter):
        """Parse VTS_XX_0.IFO file directly including PGC structure."""
        ifo_data = {
            'audio_tracks': [],
            'subtitle_tracks': [],
            'aspect_ratio': None,
            'palette': None,
            'chapters': [],
            'angles': 1,
            'audio_delays': {},
            'cell_data': [],
            'pgc_data': None,
        }

        # Find the IFO file for this title
        vts_ifo = video_ts / f"VTS_{title_num:02d}_0.IFO"
        if not vts_ifo.exists():
            log_emitter(f"  -> IFO file not found: {vts_ifo}")
            return ifo_data

        with open(vts_ifo, 'rb') as f:
            # Read IFO header (12 bytes)
            header = f.read(12)
            if header[:12] != b'DVDVIDEO-VTS':
                log_emitter("  -> Invalid IFO file header")
                return ifo_data

            # Read important sector pointers from header
            f.seek(0xC4)  # VTS_PTT_SRPT sector pointer
            vts_ptt_srpt_sector = struct.unpack('>I', f.read(4))[0]

            f.seek(0xCC)  # VTS_PGCIT sector pointer (Program Chain Info Table)
            vts_pgcit_sector = struct.unpack('>I', f.read(4))[0]

            f.seek(0xD0)  # VTSM_PGCI sector pointer (Menu PGC)
            vtsm_pgci_sector = struct.unpack('>I', f.read(4))[0]

            f.seek(0xD4)  # VTS_TMAPT sector pointer (Time Map Table)
            vts_tmapt_sector = struct.unpack('>I', f.read(4))[0]

            f.seek(0xD8)  # VTS_C_ADT sector pointer (Cell Address Table)
            vts_c_adt_sector = struct.unpack('>I', f.read(4))[0]

            f.seek(0xDC)  # VTS_VOBU_ADMAP sector pointer
            vts_vobu_admap_sector = struct.unpack('>I', f.read(4))[0]

            # Read VTSI_MAT (Video Title Set Information Management Table)
            f.seek(0x100)  # Start of video attributes
            video_attr = struct.unpack('>H', f.read(2))[0]

            # Parse video attributes (2 bytes)
            # Bits 14-13: Aspect ratio (00=4:3, 11=16:9)
            aspect_bits = (video_attr >> 13) & 0x03
            if aspect_bits == 0x03:
                ifo_data['aspect_ratio'] = '16:9'
            else:
                ifo_data['aspect_ratio'] = '4:3'

            # Bits 5-3: Video format (000=NTSC, 001=PAL)
            video_format = (video_attr >> 3) & 0x07
            ifo_data['video_format'] = 'PAL' if video_format == 1 else 'NTSC'

            # Read number of angles (at 0x102)
            f.seek(0x102)
            num_angles = struct.unpack('>B', f.read(1))[0]
            ifo_data['angles'] = num_angles if num_angles > 0 else 1

            # Read number of audio streams (at 0x103)
            f.seek(0x103)
            num_audio = struct.unpack('>B', f.read(1))[0] & 0x0F  # Lower 4 bits

            # Read audio attributes (starts at 0x104, 8 bytes each)
            f.seek(0x104)
            for i in range(min(num_audio, 8)):  # Max 8 audio streams
                audio_attr = f.read(8)
                if len(audio_attr) < 8:
                    break

                # Parse audio attributes
                audio_info = self._parse_audio_attributes(audio_attr, i)
                if audio_info:
                    ifo_data['audio_tracks'].append(audio_info)

            # Read multichannel extension info (at 0x204)
            f.seek(0x204)
            multichannel_ext = struct.unpack('>B', f.read(1))[0]

            # Read number of subtitle streams (at 0x255)
            f.seek(0x255)
            num_subs = struct.unpack('>B', f.read(1))[0] & 0x1F  # Lower 5 bits

            # Read subtitle attributes (starts at 0x256, 6 bytes each)
            f.seek(0x256)
            for i in range(min(num_subs, 32)):  # Max 32 subtitle streams
                sub_attr = f.read(6)
                if len(sub_attr) < 6:
                    break

                # Parse subtitle attributes
                sub_info = self._parse_subtitle_attributes(sub_attr, i)
                if sub_info:
                    ifo_data['subtitle_tracks'].append(sub_info)

            # Read palette (YCbCr values at 0x1B0, 16 colors * 4 bytes each)
            f.seek(0x1B0)
            palette_data = f.read(64)
            if len(palette_data) == 64:
                palette = []
                for i in range(16):
                    # Each color is 4 bytes: Y, Cb, Cr, 0
                    y, cb, cr, _ = struct.unpack('BBBB', palette_data[i*4:(i+1)*4])
                    # Convert YCbCr to RGB (ITU-R BT.601)
                    r = int(y + 1.402 * (cr - 128))
                    g = int(y - 0.344136 * (cb - 128) - 0.714136 * (cr - 128))
                    b = int(y + 1.772 * (cb - 128))
                    # Clamp to valid range
                    r = max(0, min(255, r))
                    g = max(0, min(255, g))
                    b = max(0, min(255, b))
                    palette.append(f"#{r:02x}{g:02x}{b:02x}")
                ifo_data['palette'] = palette

            # Parse PGC (Program Chain) data if available
            if vts_pgcit_sector > 0:
                pgc_data = self._parse_pgcit(f, vts_pgcit_sector * 0x800, log_emitter)
                if pgc_data:
                    ifo_data['pgc_data'] = pgc_data

                    # Extract cell and chapter data from PGC
                    if 'pgc_list' in pgc_data and pgc_data['pgc_list']:
                        first_pgc = pgc_data['pgc_list'][0]  # Usually we want the first PGC

                        # Get cell data with playback times
                        if 'cell_playback' in first_pgc:
                            ifo_data['cell_data'] = first_pgc['cell_playback']

                        # Get chapter/program map
                        if 'program_map' in first_pgc:
                            ifo_data['chapters'] = self._build_chapters_from_pgc(first_pgc)

            # Parse Cell Address Table for additional timing
            if vts_c_adt_sector > 0:
                cell_addresses = self._parse_cell_address_table(f, vts_c_adt_sector * 0x800)
                if cell_addresses:
                    ifo_data['cell_addresses'] = cell_addresses

        return ifo_data

    def _parse_pgcit(self, f, offset, log_emitter):
        """Parse the Program Chain Info Table (VTS_PGCIT)."""
        pgc_data = {'pgc_list': []}

        f.seek(offset)

        # Read PGCIT header
        num_pgcs = struct.unpack('>H', f.read(2))[0]
        f.read(2)  # Reserved
        last_byte = struct.unpack('>I', f.read(4))[0]

        log_emitter(f"  -> Found {num_pgcs} Program Chains")

        # Read PGC offset table
        pgc_offsets = []
        for i in range(num_pgcs):
            pgc_cat = struct.unpack('>B', f.read(1))[0]  # PGC category
            f.read(3)  # Reserved
            pgc_offset = struct.unpack('>I', f.read(4))[0]
            pgc_offsets.append((pgc_cat, pgc_offset))

        # Parse each PGC
        for idx, (cat, pgc_offset) in enumerate(pgc_offsets):
            pgc = self._parse_pgc(f, offset + pgc_offset)
            if pgc:
                pgc['category'] = cat
                pgc_data['pgc_list'].append(pgc)

        return pgc_data

    def _parse_pgc(self, f, offset):
        """Parse a single Program Chain."""
        pgc = {}
        f.seek(offset)

        # PGC header
        f.read(2)  # Reserved
        num_programs = struct.unpack('>B', f.read(1))[0]
        num_cells = struct.unpack('>B', f.read(1))[0]

        # Playback time (BCD format)
        playback_time_bcd = f.read(4)
        playback_time = self._bcd_to_time(playback_time_bcd)

        # Prohibited user operations
        prohibited_ops = struct.unpack('>I', f.read(4))[0]

        # Audio stream status (16 bytes, 8 streams * 2 bytes each)
        audio_status = []
        for i in range(8):
            status = struct.unpack('>H', f.read(2))[0]
            if status & 0x8000:  # Stream is present
                audio_status.append({
                    'stream_id': i,
                    'present': True,
                    'channels': (status >> 8) & 0x07,
                })

        # Subpicture stream status (32 bytes, 32 streams * 4 bytes each for widescreen + letterbox)
        f.read(32 * 4)  # Skip for now

        # Next PGC number and Previous PGC number
        next_pgc = struct.unpack('>H', f.read(2))[0]
        prev_pgc = struct.unpack('>H', f.read(2))[0]

        # GoUp PGC number
        goup_pgc = struct.unpack('>H', f.read(2))[0]

        # Still time and PGC playback mode
        still_time = struct.unpack('>B', f.read(1))[0]
        pgc_playback_mode = struct.unpack('>B', f.read(1))[0]

        # Color lookup table (palette) for this PGC
        palette = []
        for i in range(16):
            color = struct.unpack('>I', f.read(4))[0]
            palette.append(color)

        # Command table offset and program map offset
        cmd_table_offset = struct.unpack('>H', f.read(2))[0]
        pgc_program_map_offset = struct.unpack('>H', f.read(2))[0]
        cell_playback_offset = struct.unpack('>H', f.read(2))[0]
        cell_position_offset = struct.unpack('>H', f.read(2))[0]

        pgc['num_programs'] = num_programs
        pgc['num_cells'] = num_cells
        pgc['playback_time'] = playback_time
        pgc['audio_status'] = audio_status

        # Parse program map (chapters)
        if pgc_program_map_offset > 0:
            f.seek(offset + pgc_program_map_offset)
            program_map = []
            for i in range(num_programs):
                entry_cell = struct.unpack('>B', f.read(1))[0]
                program_map.append(entry_cell)
            pgc['program_map'] = program_map

        # Parse cell playback info (critical for timing)
        if cell_playback_offset > 0:
            f.seek(offset + cell_playback_offset)
            cell_playback = []
            for i in range(num_cells):
                cell_info = self._parse_cell_playback_info(f)
                cell_playback.append(cell_info)
            pgc['cell_playback'] = cell_playback

        return pgc

    def _parse_cell_playback_info(self, f):
        """Parse cell playback information (24 bytes)."""
        cell = {}

        # Cell category and restrictions
        block_mode = struct.unpack('>B', f.read(1))[0]
        block_type = struct.unpack('>B', f.read(1))[0]
        seamless_flags = struct.unpack('>B', f.read(1))[0]
        interleaved = struct.unpack('>B', f.read(1))[0]
        stc_discontinuity = struct.unpack('>B', f.read(1))[0]
        seamless_angle = struct.unpack('>B', f.read(1))[0]
        f.read(1)  # Reserved
        still_time = struct.unpack('>B', f.read(1))[0]

        # Cell command number
        cell_cmd = struct.unpack('>B', f.read(1))[0]

        # Playback time (BCD)
        playback_time_bcd = f.read(4)
        cell['playback_time'] = self._bcd_to_time(playback_time_bcd)

        # First and last VOBU start sector
        first_sector = struct.unpack('>I', f.read(4))[0]
        first_ilvu_end_sector = struct.unpack('>I', f.read(4))[0]
        last_vobu_start_sector = struct.unpack('>I', f.read(4))[0]
        last_sector = struct.unpack('>I', f.read(4))[0]

        cell['first_sector'] = first_sector
        cell['last_sector'] = last_sector
        cell['seamless'] = bool(seamless_flags)
        cell['still_time'] = still_time

        return cell

    def _parse_cell_address_table(self, f, offset):
        """Parse the Cell Address Table (VTS_C_ADT)."""
        f.seek(offset)

        num_vobs = struct.unpack('>H', f.read(2))[0]
        f.read(2)  # Reserved
        last_byte = struct.unpack('>I', f.read(4))[0]

        cell_addresses = []
        num_cells = (last_byte - 7) // 12  # Each entry is 12 bytes

        for i in range(num_cells):
            vob_id = struct.unpack('>H', f.read(2))[0]
            cell_id = struct.unpack('>B', f.read(1))[0]
            f.read(1)  # Reserved
            start_sector = struct.unpack('>I', f.read(4))[0]
            end_sector = struct.unpack('>I', f.read(4))[0]

            cell_addresses.append({
                'vob_id': vob_id,
                'cell_id': cell_id,
                'start_sector': start_sector,
                'end_sector': end_sector,
            })

        return cell_addresses

    def _bcd_to_time(self, bcd_bytes):
        """Convert BCD time format to seconds."""
        if len(bcd_bytes) < 4:
            return 0

        # Format: hours, minutes, seconds, frames/fps_flag
        hours = self._bcd_to_int(bcd_bytes[0])
        minutes = self._bcd_to_int(bcd_bytes[1])
        seconds = self._bcd_to_int(bcd_bytes[2])

        # Frame rate flag in high bit of frame byte
        frame_byte = bcd_bytes[3]
        fps = 30 if frame_byte & 0x80 else 25  # NTSC vs PAL
        frames = self._bcd_to_int(frame_byte & 0x7F)

        total_seconds = hours * 3600 + minutes * 60 + seconds + (frames / fps)
        return total_seconds

    def _bcd_to_int(self, bcd_byte):
        """Convert a BCD byte to integer."""
        return ((bcd_byte >> 4) & 0x0F) * 10 + (bcd_byte & 0x0F)

    def _build_chapters_from_pgc(self, pgc):
        """Build chapter list from PGC program map and cell timing."""
        chapters = []

        if 'program_map' not in pgc or 'cell_playback' not in pgc:
            return chapters

        program_map = pgc['program_map']
        cell_playback = pgc['cell_playback']

        # Calculate cumulative times for each cell
        cell_times = []
        cumulative_time = 0
        for cell in cell_playback:
            start_time = cumulative_time
            duration = cell.get('playback_time', 0)
            end_time = start_time + duration
            cell_times.append({
                'start': start_time,
                'end': end_time,
                'duration': duration
            })
            cumulative_time = end_time

        # Map programs (chapters) to cells
        for prog_idx, entry_cell in enumerate(program_map):
            if entry_cell > 0 and entry_cell <= len(cell_times):
                cell_idx = entry_cell - 1  # Cell numbers are 1-based
                chapters.append({
                    'number': prog_idx + 1,
                    'start_time': cell_times[cell_idx]['start'],
                    'end_time': cell_times[cell_idx]['end'],
                    'cell': entry_cell
                })

        return chapters

    def _calculate_audio_delays(self, ifo_data, log_emitter):
        """Calculate audio delays from PGC timing data."""
        # This is where we would calculate precise audio delays
        # based on the PGC structure and cell timing

        # For now, apply a default delay pattern common in DVDs
        # Real implementation would parse the NAV packets for accurate PTS offsets

        for i, audio_track in enumerate(ifo_data['audio_tracks']):
            # DVDs commonly have a small audio delay
            # This would be calculated from the actual PTS difference
            audio_track['delay_ms'] = 0  # Would calculate from PGC/NAV data

        log_emitter("  -> Audio delay calculation from PGC not yet fully implemented")

    def _parse_audio_attributes(self, attr_bytes, index):
        """Parse 8-byte audio attribute structure."""
        if len(attr_bytes) < 8:
            return None

        # First 2 bytes contain the main attributes
        attr1, attr2 = struct.unpack('>BB', attr_bytes[:2])

        # Audio coding mode (bits 7-5 of first byte)
        coding_mode = (attr1 >> 5) & 0x07
        format_map = {
            0: 'ac3',
            2: 'mpeg1',
            3: 'mpeg2',
            4: 'lpcm',
            6: 'dts',
        }
        audio_format = format_map.get(coding_mode, 'unknown')

        # Multichannel extension (bit 4)
        has_extension = bool(attr1 & 0x10)

        # Application mode (bits 3-2)
        app_mode = (attr1 >> 2) & 0x03

        # Quantization/DRC (bits 1-0)
        quantization = attr1 & 0x03

        # Number of channels (bits 2-0 of second byte)
        channels_code = attr2 & 0x07
        channels = channels_code + 1  # 0=1ch, 1=2ch, 2=3ch, etc.

        # Sample rate (bits 5-4 of second byte)
        sample_rate_code = (attr2 >> 4) & 0x03
        sample_rates = {0: 48000, 1: 96000, 2: 44100, 3: 32000}
        sample_rate = sample_rates.get(sample_rate_code, 48000)

        # Language code (bytes 2-3)
        lang_code = attr_bytes[2:4].decode('ascii', errors='ignore').lower()
        if not lang_code.isalpha():
            lang_code = 'und'

        # Language extension (byte 4)
        lang_ext = attr_bytes[4] if len(attr_bytes) > 4 else 0

        # Application info (byte 5) - indicates content type
        app_info = attr_bytes[5] if len(attr_bytes) > 5 else 0
        content = ''
        if app_info == 0:
            content = 'Normal'
        elif app_info == 1:
            content = 'Normal'
        elif app_info == 2:
            content = 'Visually Impaired'
        elif app_info == 3:
            content = "Director's Commentary"
        elif app_info == 4:
            content = 'Alternate Commentary'

        # Build track info
        audio_info = {
            'index': index,
            'language': lang_code,
            'format': audio_format,
            'channels': channels,
            'sample_rate': sample_rate,
            'content': content,
            'has_extension': has_extension,
            'quantization_drc': quantization,
        }

        # Build descriptive name
        name_parts = []
        if lang_code != 'und':
            name_parts.append(self._lang_code_to_name(lang_code))

        format_name = {
            'ac3': 'AC3',
            'dts': 'DTS',
            'mpeg1': 'MP2',
            'mpeg2': 'MP2',
            'lpcm': 'PCM',
        }.get(audio_format, audio_format.upper())

        ch_layout = self._get_channel_layout(channels)
        name_parts.append(f"{format_name} {ch_layout}")

        if content and content != 'Normal':
            name_parts.append(f"({content})")

        audio_info['name'] = ' '.join(name_parts)

        return audio_info

    def _parse_subtitle_attributes(self, attr_bytes, index):
        """Parse 6-byte subtitle attribute structure."""
        if len(attr_bytes) < 6:
            return None

        # Language code (bytes 0-1)
        lang_code = attr_bytes[0:2].decode('ascii', errors='ignore').lower()
        if not lang_code.isalpha():
            lang_code = 'und'

        # Language extension (byte 2)
        lang_ext = attr_bytes[2] if len(attr_bytes) > 2 else 0

        # Code extension (byte 3) - indicates content type
        code_ext = attr_bytes[3] if len(attr_bytes) > 3 else 0
        content = ''
        forced = False

        # Parse code extension for content type
        content_type = code_ext & 0x0F
        if content_type == 1:
            content = 'Normal'
        elif content_type == 2:
            content = 'Large'
        elif content_type == 3:
            content = 'Children'
        elif content_type == 5:
            content = 'Normal CC'
        elif content_type == 6:
            content = 'Large CC'
        elif content_type == 7:
            content = 'Children CC'
        elif content_type == 9:
            content = 'Forced'
            forced = True
        elif content_type == 13:
            content = "Director's Commentary"
        elif content_type == 14:
            content = "Large Director's Commentary"
        elif content_type == 15:
            content = "Children Director's Commentary"

        # Build track info
        sub_info = {
            'index': index,
            'language': lang_code,
            'content': content,
            'forced': forced,
            'code_extension': code_ext,
        }

        # Build descriptive name
        name_parts = []
        if lang_code != 'und':
            name_parts.append(self._lang_code_to_name(lang_code))

        if content and content != 'Normal':
            if 'CC' in content:
                name_parts.append('[CC]')
            if 'Large' in content:
                name_parts.append('[Large]')
            if 'Children' in content:
                name_parts.append('[Children]')
            if 'Commentary' in content:
                name_parts.append('[Commentary]')
            if forced:
                name_parts.append('[Forced]')

        sub_info['name'] = ' '.join(name_parts) if name_parts else f"Subtitle {index + 1}"

        return sub_info

    def _lang_code_to_name(self, code):
        """Convert ISO 639-1 language codes to names."""
        lang_map = {
            'en': 'English',
            'es': 'Spanish',
            'fr': 'French',
            'de': 'German',
            'it': 'Italian',
            'ja': 'Japanese',
            'zh': 'Chinese',
            'pt': 'Portuguese',
            'ru': 'Russian',
            'ko': 'Korean',
            'nl': 'Dutch',
            'sv': 'Swedish',
            'no': 'Norwegian',
            'da': 'Danish',
            'fi': 'Finnish',
            'pl': 'Polish',
            'cs': 'Czech',
            'hu': 'Hungarian',
            'tr': 'Turkish',
            'ar': 'Arabic',
            'he': 'Hebrew',
            'th': 'Thai',
            'vi': 'Vietnamese',
            'id': 'Indonesian',
            'ms': 'Malay',
            'hi': 'Hindi',
        }
        return lang_map.get(code, code.upper())

    def _get_channel_layout(self, channels):
        """Get channel layout string from channel count."""
        layouts = {
            1: 'Mono',
            2: 'Stereo',
            3: '2.1',
            4: '3.1',
            5: '4.1',
            6: '5.1',
            7: '6.1',
            8: '7.1',
        }
        return layouts.get(channels, f'{channels}ch')
