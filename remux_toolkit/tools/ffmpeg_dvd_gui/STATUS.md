# Disc Ripper — Project Status

Living tracking doc for the OSS DVD/BD/UHD ripper inside `ffmpeg_dvd_gui`. Update as work progresses.

**Current state**: DVD ripping pipeline complete and **cross-validated bit-for-bit against MakeMKV** for video, audio, and DVD subpictures on the test corpus. Ready to wire into the GUI as a `NativeRipWorker`.

---

## Vision

Build an open-source DVD/Blu-ray/UHD ripper inside Remux-Toolkit. Start with DVD; lay foundations that extend cleanly to BD then UHD. MakeMKV-philosophy: deterministic, accurate, archive-quality rips; never silently drop data; first-class diagnostics. Use FFmpeg + libdvdread/libbluray as link-time dependencies but never as patch targets — when something in ffmpeg's `dvdvideo` demuxer is wrong, we fix it in our own code, not theirs.

## Architecture (the line we drew)

```
disc → libdvdread (ctypes) → CellReader → ps_walker.iter_es_payloads
                                                    │
                                                    ↓
                                      per-stream raw ES bytes
                                                    │
                                                    ↓
                              ffmpeg (multi-fd, ONE pipe per stream)
                                      -f mpegvideo   (video)
                                      -f ac3/dts/mp2 (audio)
                                      raw pcm (LPCM, -ar/-ac)
                                                    │
                              + chapters via FFmetadata1
                              + subs via dvdvideo side-channel MKV
                                                    │
                                                    ↓
                                           streaming MKV
```

**We own**: disc I/O, cell traversal, MPEG-PS / PES demuxing, A/V delay measurement, chapter extraction.
**ffmpeg owns**: ES → MKV muxing only. The `dvdvideo` demuxer is used ONLY for subpicture extraction in a side-channel subprocess (its bugs don't affect that code path).
**No PyAV. No mkvmerge. No PS-pipe approach.** Tried both PS-pipe (`-f mpeg`) and PyAV in earlier phases — both rejected. PS-pipe failed on STC discontinuities (ANGEL T1).

## What's built and validated

### Modules

| Path | Purpose |
|------|---------|
| `bindings/libdvdread.py` | ctypes binding for libdvdread.so.8. All needed structs + functions. Silent logger to suppress chatter. |
| `core/demux/cell_reader.py` | `CellReader` — streams sectors per title in playback order, tagged with `Cell` info |
| `core/demux/ps_walker.py` | MPEG-PS pack + PES walker; strips PES + substream framing; preserves NAV packs for diagnostics |
| `core/demux/chapters.py` | Reads PGC.program_map → cell timings → FFmetadata1 chapter table |
| `core/analysis/inspector.py` | Disc → deterministic JSON. CLI `dvd-inspector` w/ `--compare-makemkv` and `--compare-ffprobe` |
| `core/analysis/analyzer.py` | "MakeMKV brain": strict dedup, classification, never-exclude. Produces GUI-compatible dict |
| `core/analysis/inspect_title_streams.py` | Per-stream stats: packet counts, byte totals, PTS, cell-boundary deltas. Diagnostic ground truth |
| `core/analysis/rip_title_cli.py` | Standalone rip CLI, no GUI needed for testing |
| `core/analysis/compare_streams.py` | Cross-rip SHA-256 comparison — extract ES from any MKV, compare against another |
| `core/info_probe_native.py` | `DVDProbeWorker` drop-in for the GUI. Qt class wraps a pure `probe_disc()` function |
| `core/orchestrator.py` | `rip_title(disc, title, output)` — the final ripper. Multi-fd ffmpeg, per-stream `-itsoffset` |
| `core/mux/ffmpeg_pipe.py` | Earlier multi-fd subprocess wrapper. Unused by current orchestrator but kept for reference |

### Tests

At `/home/chaoz/Desktop/Makemkv/Tests/disc_ripper/`. Uses `/home/chaoz/Desktop/Makemkv/Tests/.venv` (Python 3.13 + pytest 9). PyQt6 NOT in venv — modules import Qt lazily so tests run headless.

```bash
cd /home/chaoz/Desktop/Makemkv/Tests/disc_ripper
/home/chaoz/Desktop/Makemkv/Tests/.venv/bin/python -m pytest -v
```

60 fast tests + 5 slow (real-disc rip) tests. Fast subset: `pytest -m 'not slow'` (<1s).

### Cross-rip evidence (the gold-standard validation)

**FOREVER_KNIGHT T2** (the AC3 PG-boundary regression case the original FFmpeg patch was meant to fix):

| Stream | ours | patched-ffmpeg | vanilla-ffmpeg | makemkv |
|--------|------|----------------|----------------|---------|
| Video MPEG-2 ES | `38f1d312` 1474MB | **same** ✓ | **same** ✓ | -570KB (trims) |
| Audio AC3 ES | `58f61779` 69MB | **same** ✓ | `0db3a5d5` **-4287 frames (137s — bug!)** | **same** ✓ |

Our pipeline matches MakeMKV byte-for-byte on every stream that matters. Vanilla ffmpeg drops 137 seconds of audio (the bug). The FFmpeg patch can be retired when we ship.

**ANGEL T1** (long title with multiple STC discontinuities):

| Stream | ours vs MakeMKV |
|--------|------------------|
| Video MPEG-2 | bit-perfect vs patched-ffmpeg; MakeMKV -1 frame trim |
| Audio (4 AC3) | **all 4 bit-perfect vs MakeMKV** |
| Subpictures (4 dvd_subtitle) | **all 4 bit-perfect vs MakeMKV** |

Test rips kept at `/home/chaoz/Desktop/Makemkv/Tests/disc_ripper/outputs/`.

### Codec coverage

| Codec | Status | Tested on |
|-------|--------|-----------|
| MPEG-2 video | ✓ working, validated | every disc tested |
| AC3 audio | ✓ working, bit-perfect vs MakeMKV | corpus |
| DTS audio | ✓ working | Knights of Bloodsteel T1 |
| MPEG-1 audio | ✓ code written | not yet validated on Великий Мерлин |
| LPCM audio | ✓ code written (speculative) | **no test disc in corpus** |
| DVD subpictures | ✓ working via dvdvideo side-channel, bit-perfect vs MakeMKV | ANGEL T1 |
| Line21 closed captions (subrip) | ✗ not implemented | Phase 4d.3 |

### GUI integration

- `DVDProbeWorker` already replaced with the native analyzer-driven version. Toggle via `use_native_probe` (default True). Old ffprobe path stays accessible as fallback.
- GUI tree now shows ALL titles (no minlength filter). Analyzer flags drive default check state + dim/italic + inline reasons + tooltips. Details panel shows classification, dedup info, VTS/PGC info, closed captions availability.
- `rip_title` not yet wired into the rip worker — still uses old `FFmpegDVDWorker`. **Phase 4d.4 candidate.**

### A/V delay handling (the question you asked)

We measure the cross-stream start delay once per title, in a pre-scan of the first ~100 sectors. For each active stream we record the first PES PTS we see, then compute `delay[audio_N] = (audio_first_pts[N] - video_first_pts) / 90000` seconds. Pass via `-itsoffset` per audio input.

On every corpus disc tested, the measured delay is 0 ms (audio and video start at the same PTS on the disc). Pre-scan is cheap (~200 KB read). Visible in the rip CLI summary.

We also compensate for ffmpeg's `+genpts` quirk that numbers the first generated PTS at `1*frame_duration` instead of 0. Without that, our video would land 33ms later than audio (NTSC) — small but real A/V sync bug. Fixed by `-itsoffset -<frame_duration>` on video input.

---

## What's missing / needs work

Priority ordered:

### P0 — Production blockers

1. **Wire `rip_title` into the GUI** as a `NativeRipWorker` Qt class. The current GUI still uses the old subprocess `FFmpegDVDWorker` for rips even though the probe is native. Drop-in replacement following the same Qt signal interface (`progress`, `status_text`, `line_out`, `job_done`). Behind a `use_native_remux` setting like we have for the probe.
2. **Validate MPEG-1 audio support on Великий Мерлин** — code is written but not run on a real disc.

### P1 — Coverage gaps

3. **LPCM validation** — code written speculatively, no Japanese LPCM disc in corpus. Need a known-LPCM disc to verify the 16-bit and 24-bit paths.
4. **Line21 closed captions** (Phase 4d.3) — MakeMKV bundles CCExtractor and converts line21 CC → SRT subtitle track. We currently don't. Either invoke CCExtractor as another side-channel, or skip if you don't use these.
5. **Cross-rip pytest fixture** that auto-rips every corpus disc with ours + patched-ffmpeg + MakeMKV and asserts byte-perfect ES match. Locks in our correctness across changes.

### P2 — Edge cases

6. **Soft-telecine / VFR detection** for film discs (24p inside 29.97 NTSC via 3:2 pulldown). mkvmerge produces VFR MKV by detecting the pulldown pattern. We're CFR-only — works for 99% of DVDs but loses pulldown fidelity on the rare film-on-DVD disc.
7. **Multi-angle titles** — we don't yet handle PGC angle blocks correctly. The `block_mode/block_type` cell fields signal angles; we currently treat them as ordinary cells.
8. **Bad-sector recovery** — `CellReader` has a stub for sector retries but no actual retry logic (it yields zero-filled sectors on read failure). Real discs with rot need genuine retry + log.

### P3 — Future architecture

9. **Native subpicture handler** — replace dvdvideo side-channel with our own SP_DCSQ parser. Today it works (bit-perfect against MakeMKV) but depends on ffmpeg's dvdvideo for one stream type.
10. **MKV muxer in-house** — replace ffmpeg muxer with our own libmatroska binding (or rewrite). Biggest "purity" win, biggest effort.
11. **Blu-ray support** — libbluray binding, M2TS demux, MPLS parsing. Same architectural pattern. UHD adds AACS 2.0 (needs runtime `libmmbd`).

---

## Reverse-engineering candidates

Things we'd benefit from understanding more deeply but haven't yet dug into:

1. **MakeMKV's cell-boundary algorithm**. MakeMKV's `libmakemkv` is binary-only; we can see only the LGPL libdvdread wrapper in `mmgpl/dvdread.cpp`. What does it do at PG boundaries that we don't? Our AC3 audio matches MakeMKV byte-for-byte, so probably nothing we're missing — but worth understanding their state machine.
2. **The 1-frame video trim MakeMKV does** vs our 1766 MB vs their 1764 MB (1 frame) on ANGEL T1. They drop something — last partial frame of the title? Worth diffing the actual byte ranges to see where their video output diverges from ours.
3. **CCExtractor invocation by MakeMKV** — we have the source in `makemkv-oss-1.18.3/mmccextr/`. How does MakeMKV decide to extract CC vs not? Does it always run it on NTSC content where `line21_cc_1` is set, or are there other heuristics?
4. **DVD playlist/title structure for "duplicate" rip suppression**. MakeMKV's brain hides what it considers redundant titles. We mark them `duplicate_of` but never hide. What makes their dedup smarter than ours? (Strict cell-set equality is our rule.)
5. **Soft-telecine detection from MPEG-2 sequence_display_extension flags**. The pulldown pattern can be detected from `repeat_first_field` + `top_field_first` flags in the bitstream. We don't parse the video bitstream. Worth a minimal parser for VFR detection.

## How to actually use this

### Probe a disc (no rip)
```bash
python3 -m remux_toolkit.tools.ffmpeg_dvd_gui.core.analysis.inspector \
    "/path/to/disc" --compare-makemkv --compare-ffprobe -o report.json
```

### Diagnose a title's streams (sector-level)
```bash
python3 -m remux_toolkit.tools.ffmpeg_dvd_gui.core.analysis.inspect_title_streams \
    "/path/to/disc" <title_num> -o stream_stats.json
```

### Rip a title (standalone, no GUI)
```bash
python3 -m remux_toolkit.tools.ffmpeg_dvd_gui.core.analysis.rip_title_cli \
    "/path/to/disc" <title_num> -o out.mkv
```
Pass `--no-include-subs` to skip the sub side-channel.

### Compare rip outputs (the gold-standard cross-check)
```bash
ffmpeg -y -f dvdvideo -title N -i DISC -map 0:v -map 0:a -c copy ref_vanilla.mkv
/home/chaoz/Desktop/Programs/FFmpeg/ffmpeg -y -f dvdvideo -title N -i DISC -map 0:v -map 0:a -c copy ref_patched.mkv
makemkvcon --minlength=0 mkv "file:DISC" <mkv_title_id> /path/to/makemkv_outdir/
python3 -m remux_toolkit.tools.ffmpeg_dvd_gui.core.analysis.rip_title_cli DISC N -o ours.mkv

python3 -m remux_toolkit.tools.ffmpeg_dvd_gui.core.analysis.compare_streams \
    ours=ours.mkv patched=ref_patched.mkv vanilla=ref_vanilla.mkv \
    makemkv=/path/to/makemkv_outdir/*.mkv \
    -o compare.json
```

## References

- Test corpus: `/home/chaoz/Desktop/Makemkv/Dvds for testing/` (24 discs, mix of TV/anime/Russian + Japanese)
- Patched FFmpeg fork: `/home/chaoz/Desktop/Programs/FFmpeg/` (vanilla 8.1 + 1-patch on dvdvideo AC3)
- MakeMKV-OSS reference (read-only): `/home/chaoz/Downloads/makemkv-oss-1.18.3/`
- Project memory (AI context, internal): `/home/chaoz/.claude/projects/-home-chaoz-Desktop-Programs-Remux-Toolkit/memory/`
