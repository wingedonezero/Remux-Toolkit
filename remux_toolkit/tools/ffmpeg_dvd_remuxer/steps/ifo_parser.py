# remux_toolkit/tools/ffmpeg_dvd_remuxer/steps/ifo_parser.py
import struct
import json
from pathlib import Path
from ..utils.helpers import run_capture

class IfoParserStep:
    """Parse IFO files directly to extract DVD metadata."""

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
        except Exception as e:
            log_emitter(f"  -> Error parsing IFO: {e}")

        # Store IFO data in context
        context['ifo_data'] = ifo_data

        # Log what we found
        if ifo_data['audio_tracks']:
            log_emitter(f"  -> Found {len(ifo_data['audio_tracks'])} audio track descriptions from IFO")
            for track in ifo_data['audio_tracks']:
                log_emitter(f"     Audio {track['index']}: {track.get('name', 'Unknown')}")
        if ifo_data['subtitle_tracks']:
            log_emitter(f"  -> Found {len(ifo_data['subtitle_tracks'])} subtitle track descriptions from IFO")
        if ifo_data['aspect_ratio']:
            log_emitter(f"  -> Aspect ratio from IFO: {ifo_data['aspect_ratio']}")
        if ifo_data.get('palette'):
            log_emitter(f"  -> Found subtitle palette data")

        return True

    def _parse_vts_ifo(self, video_ts: Path, title_num: int, log_emitter):
        """Parse VTS_XX_0.IFO file directly."""
        ifo_data = {
            'audio_tracks': [],
            'subtitle_tracks': [],
            'aspect_ratio': None,
            'palette': None,
            'chapters': [],
            'angles': 1,
            'audio_delays': {},
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

            # Read VTS_PTT_SRPT pointer (sector pointer to Part_of_Title table)
            f.seek(0xC4)  # VTS_PTT_SRPT sector pointer
            vts_ptt_srpt = struct.unpack('>I', f.read(4))[0] * 0x800  # Convert to byte offset

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
                    # Convert YCbCr to RGB (simplified)
                    r = int(y + 1.402 * (cr - 128))
                    g = int(y - 0.344136 * (cb - 128) - 0.714136 * (cr - 128))
                    b = int(y + 1.772 * (cb - 128))
                    # Clamp to valid range
                    r = max(0, min(255, r))
                    g = max(0, min(255, g))
                    b = max(0, min(255, b))
                    palette.append(f"#{r:02x}{g:02x}{b:02x}")
                ifo_data['palette'] = palette

        return ifo_data

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

        # Number of channels (bits 2-0 of second byte)
        channels_code = attr2 & 0x07
        channels = channels_code + 1  # 0=1ch, 1=2ch, 2=3ch, etc.

        # Language code (bytes 2-3)
        lang_code = attr_bytes[2:4].decode('ascii', errors='ignore').lower()
        if not lang_code.isalpha():
            lang_code = 'und'

        # Application mode (byte 5) - can indicate commentary, etc.
        app_mode = attr_bytes[5] if len(attr_bytes) > 5 else 0
        content = ''
        if app_mode == 1:
            content = 'Normal'
        elif app_mode == 2:
            content = 'Visually Impaired'
        elif app_mode == 3:
            content = "Director's Commentary"
        elif app_mode == 4:
            content = 'Alternate Commentary'

        # Build track info
        audio_info = {
            'index': index,
            'language': lang_code,
            'format': audio_format,
            'channels': channels,
            'content': content,
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

        # Extension/content type (byte 2)
        extension = attr_bytes[2] if len(attr_bytes) > 2 else 0
        content = ''

        # Common extension codes
        if extension == 1:
            content = 'Normal'
        elif extension == 2:
            content = 'Large'
        elif extension == 3:
            content = 'Children'
        elif extension == 5:
            content = 'Normal CC'
        elif extension == 6:
            content = 'Large CC'
        elif extension == 7:
            content = 'Children CC'
        elif extension == 9:
            content = 'Forced'
        elif extension == 13:
            content = "Director's Commentary"
        elif extension == 14:
            content = "Large Director's Commentary"
        elif extension == 15:
            content = "Children Director's Commentary"

        # Build track info
        sub_info = {
            'index': index,
            'language': lang_code,
            'content': content,
            'forced': 'Forced' in content,
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
            if 'Forced' in content:
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
