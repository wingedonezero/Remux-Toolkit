"""
Codec-aware picture splitters.

A "picturizer" takes a stream of accumulated video PES bytes plus a base
PTS and yields one MkvChunk per coded picture. This is where codec-
specific picture-boundary detection and coding-type classification lives;
the rest of the demux pipeline is codec-agnostic.

Current implementations:
- ``mpeg2`` — MPEG-1/MPEG-2 video (DVD-Video). Picture splits at
  ``picture_start_code`` (0x000001 00); I/P/B/D classification from the
  3-bit ``picture_coding_type`` in the picture_header.

Future:
- ``h264`` — H.264/AVC (Blu-ray). Picture boundaries at NAL access-unit
  delimiters with start_code_emulation prevention. Slice_header carries
  slice_type → I/P/B mapping.
- ``hevc`` — H.265/HEVC (UHD-BD). NAL-unit-based like h264 but with
  different NAL type IDs.
"""
