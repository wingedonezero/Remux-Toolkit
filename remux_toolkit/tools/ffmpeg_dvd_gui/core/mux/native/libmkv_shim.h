/*
 *  libmkv_shim — C wrapper around libebml + libmatroska for ctypes use.
 *
 *  Orchestration (cluster boundaries, lacing decisions, AUTO_DURATION
 *  fill-ins, reference-block bookkeeping) lives in Python. This shim is
 *  just the libmatroska FFI bridge — each function maps to a small
 *  libmatroska operation. Per-frame Python callback overhead is fine:
 *  typical DVD title has ≤30k frames and each call is a microsecond-class
 *  round trip.
 *
 *  Build:    see build.sh
 *  Loader:   see __init__.py
 *
 *  Lifecycle:
 *      open
 *      set_timestamp_scale  (optional, override default 1_000_000)
 *      set_title            (optional)
 *      add_track × N
 *      write_headers
 *      loop:
 *          start_cluster
 *          add_simple_block × N
 *          end_cluster
 *      finalize
 *      close
 *
 *  All functions return 0 on success and non-zero on error. Pointer
 *  functions return NULL on error. On error, mkv_writer_last_error returns
 *  a thread-local diagnostic string.
 */

#ifndef LIBMKV_SHIM_H
#define LIBMKV_SHIM_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* opaque handles */
typedef struct mkv_writer_st mkv_writer_t;
typedef struct mkv_track_st  mkv_track_t;

/* track types — mirror MkvTrackType in core/mux/types.py */
typedef enum {
    MKV_SHIM_TRACK_UNKNOWN  = 0,
    MKV_SHIM_TRACK_VIDEO    = 1,
    MKV_SHIM_TRACK_AUDIO    = 2,
    MKV_SHIM_TRACK_SUBTITLE = 3
} mkv_shim_track_type;

/* per-frame flags — mirror MkvChunkFlags */
typedef enum {
    MKV_SHIM_FRAME_NONE          = 0,
    MKV_SHIM_FRAME_KEYFRAME      = 1,
    MKV_SHIM_FRAME_CLUSTER_START = 2,
    MKV_SHIM_FRAME_CHAPTER_MARK  = 4,
    MKV_SHIM_FRAME_DISCARDABLE   = 8,
    MKV_SHIM_FRAME_OLD_BLOCK     = 16,
    MKV_SHIM_FRAME_AUTO_DURATION = 32
} mkv_shim_frame_flags;

/* per-track flags — mirror MkvTrackFlags */
typedef enum {
    MKV_SHIM_TRACK_FLAG_DEFAULT = 1,
    MKV_SHIM_TRACK_FLAG_FORCED  = 2,
    MKV_SHIM_TRACK_FLAG_LACING  = 128
} mkv_shim_track_flags;

/* track metadata input — caller-allocated; not retained beyond add_track */
typedef struct {
    const char*    codec_id;              /* required, e.g. "V_MPEG2" */
    const char*    codec_subid;           /* optional */
    const char*    lang;                  /* ISO 639, e.g. "eng" or "und" */
    const char*    name;                  /* optional UTF-8 track name */

    const uint8_t* codec_private;         /* optional; may be NULL */
    uint32_t       codec_private_size;

    uint32_t       mkv_flags;             /* mkv_shim_track_flags bits */
    int64_t        default_duration_ns;
    uint32_t       min_cache;

    /* video-only (ignored for non-video) */
    int            pixel_h, pixel_v;
    int            display_h, display_v;
    int            stereo_mode;

    /* video colour metadata (KaxVideoColour). Integer codes per H.273:
     *   primaries / transfer / matrix: 6=smpte170m, 5=bt470bg, 1=bt709, etc.
     *   color_range: 1=broadcast (tv 16-235), 2=full (0-255).
     * 0 = "unspecified" → shim omits the element. */
    int            color_primaries;
    int            color_transfer;
    int            color_matrix;
    int            color_range;

    /* audio-only */
    int            sample_rate;
    int            channels_count;
    int            bits_per_sample;

    /* subtitle-only */
    uint8_t        offset_sequence_id_ref;
} mkv_shim_track_info;


/* ---- lifecycle ---- */

mkv_writer_t* mkv_writer_open(const char* path, const char* writing_app);

/* Override the default TimecodeScale (1_000_000 ns = 1 ms). Must be called
 * before write_headers. Setting to 1 gives ns precision (no rounding). */
int mkv_writer_set_timestamp_scale(mkv_writer_t* w, int64_t scale_ns);

int mkv_writer_set_title(mkv_writer_t* w, const char* title);

mkv_track_t* mkv_writer_add_track(mkv_writer_t* w,
                                  mkv_shim_track_type type,
                                  const mkv_shim_track_info* info);

int mkv_writer_write_headers(mkv_writer_t* w);

/* ---- cluster writing ---- */

int mkv_writer_start_cluster(mkv_writer_t* w, int64_t timecode_ns);

int mkv_writer_add_simple_block(mkv_writer_t* w,
                                mkv_track_t* track,
                                const uint8_t* data,
                                uint32_t size,
                                int64_t timecode_ns,
                                uint32_t flags);

/* Write a KaxBlockGroup with an explicit BlockDuration. Use this when a
 * frame's duration is not the track's DefaultDuration — most commonly
 * subtitle events whose display time is computed from SP_DCSQ or
 * lookahead to the next event. ``duration_ns`` is required (>0); for
 * unknown durations the caller can pass a placeholder, though real
 * AUTO_DURATION retro-fixup isn't yet wired through this shim.
 *
 * Cue-point emission follows the same "one cue per cluster, first
 * keyframe" policy as add_simple_block. */
int mkv_writer_add_block_group(mkv_writer_t* w,
                               mkv_track_t* track,
                               const uint8_t* data,
                               uint32_t size,
                               int64_t timecode_ns,
                               int64_t duration_ns,
                               uint32_t flags);

int mkv_writer_end_cluster(mkv_writer_t* w);

/* ---- chapters / attachments (call between write_headers and finalize) ---- */

/* Add a chapter atom. ``time_end_ns`` may be -1 to leave the chapter end
 * open (the last chapter's end is implicit at finalize via max_duration_ns).
 * ``name`` must be UTF-8; ``lang`` is ISO 639 (3-letter code or empty). */
int mkv_writer_add_chapter(mkv_writer_t* w,
                           int64_t time_start_ns,
                           int64_t time_end_ns,
                           const char* name,
                           const char* lang);

/* Attach a file blob. Caller retains ownership of ``data``; the shim copies it. */
int mkv_writer_add_attachment(mkv_writer_t* w,
                              const char* name,
                              const char* mime_type,
                              const uint8_t* data,
                              uint32_t size);

/* Per-track statistics. Call after all blocks for the track are added
 * but before finalize. Emitted as KaxTags at finalize time using the
 * MakeMKV / mkvmerge convention:
 *   BPS                       (bits per second)
 *   DURATION                  (HH:MM:SS.mmm)
 *   NUMBER_OF_FRAMES
 *   NUMBER_OF_BYTES
 *   _STATISTICS_WRITING_APP   (from writing_app passed at open)
 *   _STATISTICS_WRITING_DATE_UTC
 *   _STATISTICS_TAGS          (list of which stats are present)
 *
 * Calling this is optional — tracks without stats produce no KaxTag
 * entry. Calling twice on the same track replaces the prior stats. */
int mkv_writer_set_track_stats(mkv_writer_t* w,
                               mkv_track_t* track,
                               uint64_t total_bytes,
                               uint64_t num_frames,
                               int64_t total_duration_ns);

/* ---- finalize ---- */

int mkv_writer_finalize(mkv_writer_t* w, int64_t max_duration_ns);
int mkv_writer_close(mkv_writer_t* w);

/* ---- diagnostics ---- */

const char* mkv_writer_last_error(void);
const char* mkv_writer_version(void);

#ifdef __cplusplus
}
#endif

#endif /* LIBMKV_SHIM_H */
