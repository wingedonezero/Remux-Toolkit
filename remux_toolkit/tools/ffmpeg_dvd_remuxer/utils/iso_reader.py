# remux_toolkit/tools/ffmpeg_dvd_remuxer/utils/iso_reader.py
import io
import struct
from pathlib import Path
try:
    import pycdlib
    HAS_PYCDLIB = True
except ImportError:
    HAS_PYCDLIB = False

class ISOReader:
    """Read IFO files directly from ISO without mounting."""

    def __init__(self, iso_path: Path):
        if not HAS_PYCDLIB:
            raise ImportError("pycdlib is required for ISO support. Install with: pip install pycdlib")

        self.iso_path = iso_path
        self.iso = None

    def __enter__(self):
        self.iso = pycdlib.PyCdlib()
        self.iso.open(str(self.iso_path))
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.iso:
            self.iso.close()

    def read_ifo(self, title_num: int) -> bytes:
        """Read a VTS IFO file from the ISO."""
        ifo_name = f"/VIDEO_TS/VTS_{title_num:02d}_0.IFO;1"

        try:
            # Get file info
            entry = self.iso.get_entry(ifo_name)

            # Read the file
            data = io.BytesIO()
            self.iso.get_file_from_iso_fp(data, iso_path=ifo_name)

            return data.getvalue()
        except pycdlib.pycdlibexception.PyCdlibInvalidISO:
            raise ValueError(f"IFO file not found: {ifo_name}")

    def list_titles(self) -> list[int]:
        """List all available titles in the ISO."""
        titles = []

        for entry in self.iso.list_children(iso_path='/VIDEO_TS'):
            name = entry.file_identifier().decode('utf-8', errors='ignore')
            # Look for VTS_XX_0.IFO files
            if name.startswith('VTS_') and name.endswith('_0.IFO;1'):
                try:
                    title_num = int(name[4:6])
                    titles.append(title_num)
                except ValueError:
                    pass

        return sorted(titles)
