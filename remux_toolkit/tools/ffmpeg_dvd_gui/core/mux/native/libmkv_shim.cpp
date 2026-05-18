/*
 *  libmkv_shim — implementation. Builds against libebml 1.4 + libmatroska 1.7
 *  (Debian-shipped versions).
 */

#include "libmkv_shim.h"

#include <ebml/StdIOCallback.h>
#include <ebml/EbmlHead.h>
#include <ebml/EbmlSubHead.h>
#include <ebml/EbmlVoid.h>
#include <ebml/EbmlString.h>
#include <ebml/EbmlUnicodeString.h>
#include <ebml/EbmlVersion.h>
#include <ebml/EbmlContexts.h>

#include <matroska/KaxSegment.h>
#include <matroska/KaxTracks.h>
#include <matroska/KaxTrackEntryData.h>
#include <matroska/KaxTrackAudio.h>
#include <matroska/KaxTrackVideo.h>
#include <matroska/KaxCluster.h>
#include <matroska/KaxClusterData.h>
#include <matroska/KaxSeekHead.h>
#include <matroska/KaxInfo.h>
#include <matroska/KaxInfoData.h>
#include <matroska/KaxBlock.h>
#include <matroska/KaxBlockData.h>
#include <matroska/KaxCues.h>
#include <matroska/KaxCuesData.h>
#include <matroska/KaxChapters.h>
#include <matroska/KaxAttachments.h>
#include <matroska/KaxAttached.h>
#include <matroska/KaxVersion.h>

#include <cstdio>
#include <cstring>
#include <ctime>
#include <memory>
#include <new>
#include <random>
#include <string>
#include <vector>

using namespace libebml;
using namespace libmatroska;


/* ------------------------------------------------------------------ */
/* Constants                                                           */
/* ------------------------------------------------------------------ */

static const int64_t DEFAULT_TIMECODE_SCALE = 1000000;   /* 1 ms */
static const int     SEGMENT_SIZE_BYTES     = 8;
static const int     CLUSTER_SIZE_BYTES     = 4;


/* ------------------------------------------------------------------ */
/* Thread-local error                                                  */
/* ------------------------------------------------------------------ */

static thread_local std::string g_last_error;

static void set_error(const std::string& msg) { g_last_error = msg; }

extern "C" const char* mkv_writer_last_error(void) {
    return g_last_error.empty() ? nullptr : g_last_error.c_str();
}

extern "C" const char* mkv_writer_version(void) {
    static const char* v = "libmkv_shim 0.1";
    return v;
}


/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

static UTFstring utf8_to_utf(const char* s) {
    UTFstring out;
    if (s != nullptr) out.SetUTF8(s);
    return out;
}

static KaxSeek* add_seek_entry(KaxSeekHead& seek_head, const EbmlId& id) {
    KaxSeek& new_seek = AddNewChild<KaxSeek>(seek_head);

    KaxSeekPosition& seek_pos = GetChild<KaxSeekPosition>(new_seek);
    seek_pos.SetDefaultSize(8);
    *static_cast<EbmlUInteger*>(&seek_pos) = 0;

    KaxSeekID& seek_id = GetChild<KaxSeekID>(new_seek);
    binary id_bytes[4];
    id.Fill(id_bytes);
    seek_id.CopyBuffer(id_bytes, EBML_ID_LENGTH(id));

    return &new_seek;
}

template <class T>
static KaxSeek* add_seek_for(KaxSeekHead& seek_head) {
    return add_seek_entry(seek_head, EBML_ID(T));
}

static void update_seek_entry(KaxSeek* seek_entry,
                              IOCallback& file,
                              const EbmlElement& elt,
                              const KaxSegment& parent_segment) {
    GetChild<KaxSeekPosition>(*seek_entry).SetValue(
        parent_segment.GetRelativePosition(elt));
    GetChild<KaxSeekPosition>(*seek_entry).OverwriteData(file, true);
}

static void render_void_over(EbmlMaster* element, IOCallback& file) {
    EbmlVoid v;
    v.Overwrite(*element, file);

    uint64 cur = file.getFilePointer();
    file.setFilePointer(v.GetElementPosition() + v.HeadSize());
    v.RenderData(file, true);
    file.setFilePointer(cur);
}


/* ------------------------------------------------------------------ */
/* Internal types                                                      */
/* ------------------------------------------------------------------ */

struct mkv_track_st {
    KaxTrackEntry*       entry         = nullptr;
    uint32_t             track_number  = 0;
    mkv_shim_track_type  type          = MKV_SHIM_TRACK_UNKNOWN;
    /* Per-track stats fed via mkv_writer_set_track_stats; emitted as
     * KaxTags at finalize. ``has_stats`` distinguishes "not provided"
     * (skip tag) from "provided but zero". */
    bool                 has_stats     = false;
    uint64_t             stat_bytes    = 0;
    uint64_t             stat_frames   = 0;
    int64_t              stat_duration_ns = 0;
};

struct pending_chapter_t {
    int64_t     time_start_ns;
    int64_t     time_end_ns;       /* -1 ⇒ open-ended */
    std::string name;
    std::string lang;
};

struct pending_attachment_t {
    std::string name;
    std::string mime;
    std::vector<uint8_t> data;
};

struct mkv_writer_st {
    std::unique_ptr<StdIOCallback> file;
    std::string                    writing_app;
    std::string                    title;

    int64_t                        timestamp_scale = DEFAULT_TIMECODE_SCALE;

    KaxSegment                     segment;
    KaxSeekHead*                   seek_head     = nullptr;
    KaxSeek*                       seek_info     = nullptr;
    KaxSeek*                       seek_tracks   = nullptr;
    KaxSeek*                       seek_cues     = nullptr;
    KaxSeek*                       seek_chapters = nullptr;
    KaxSeek*                       seek_attach   = nullptr;
    KaxSeek*                       seek_tags     = nullptr;

    KaxInfo*                       info          = nullptr;
    KaxTracks*                     tracks        = nullptr;

    std::vector<std::unique_ptr<mkv_track_st>> track_handles;

    KaxCluster*                    current_cluster    = nullptr;
    KaxCluster*                    prev_cluster       = nullptr;
    bool                           current_cluster_has_cue = false;

    /* Built up during the mux; rendered at finalize. */
    KaxCues                        cues;
    std::vector<pending_chapter_t>    chapters;
    std::vector<pending_attachment_t> attachments;

    bool                           headers_written = false;
    bool                           finalized       = false;
};

static int64_t scale_ns(mkv_writer_st* w, int64_t ns) {
    /* Round to nearest (half-tick up) — matches MakeMKV / ffmpeg behavior. */
    if (w->timestamp_scale <= 1) return ns;
    return (ns + (w->timestamp_scale / 2)) / w->timestamp_scale;
}


/* ------------------------------------------------------------------ */
/* Open / close                                                        */
/* ------------------------------------------------------------------ */

extern "C" mkv_writer_t* mkv_writer_open(const char* path,
                                         const char* writing_app) {
    if (path == nullptr) {
        set_error("mkv_writer_open: null path");
        return nullptr;
    }
    g_last_error.clear();

    auto* w = new (std::nothrow) mkv_writer_st();
    if (w == nullptr) {
        set_error("mkv_writer_open: oom");
        return nullptr;
    }

    try {
        w->file = std::make_unique<StdIOCallback>(path, MODE_CREATE);
    } catch (const std::exception& e) {
        set_error(std::string("mkv_writer_open: ") + e.what());
        delete w;
        return nullptr;
    }

    if (writing_app != nullptr) w->writing_app = writing_app;

    /* EBML head */
    EbmlHead file_head;
    *static_cast<EbmlString*>(&GetChild<EDocType>(file_head)) = "matroska";
    *static_cast<EbmlUInteger*>(&GetChild<EDocTypeVersion>(file_head)) =
        LIBMATROSKA_VERSION;
    *static_cast<EbmlUInteger*>(&GetChild<EDocTypeReadVersion>(file_head)) = 2;
    file_head.Render(*w->file, true);

    /* Segment header with 8-byte size placeholder */
    w->segment.WriteHead(*w->file, SEGMENT_SIZE_BYTES);

    /* Seek head with 6 placeholders */
    w->seek_head = &AddNewChild<KaxSeekHead>(w->segment);
    w->seek_info     = add_seek_for<KaxInfo>(*w->seek_head);
    w->seek_tracks   = add_seek_for<KaxTracks>(*w->seek_head);
    w->seek_cues     = add_seek_for<KaxCues>(*w->seek_head);
    w->seek_chapters = add_seek_for<KaxChapters>(*w->seek_head);
    w->seek_attach   = add_seek_for<KaxAttachments>(*w->seek_head);
    w->seek_tags     = add_seek_for<KaxTags>(*w->seek_head);

    w->seek_head->Render(*w->file);

    return w;
}

extern "C" int mkv_writer_close(mkv_writer_t* w) {
    if (w == nullptr) return 0;
    if (w->current_cluster != nullptr && !w->finalized) {
        try { (void)mkv_writer_end_cluster(w); } catch (...) {}
    }
    if (w->file) {
        try { w->file->close(); } catch (...) {}
        w->file.reset();
    }
    delete w;
    return 0;
}

extern "C" int mkv_writer_set_title(mkv_writer_t* w, const char* title) {
    if (w == nullptr) { set_error("set_title: null writer"); return 1; }
    if (w->headers_written) {
        set_error("set_title: too late, headers already written");
        return 1;
    }
    if (title != nullptr) w->title = title;
    return 0;
}

extern "C" int mkv_writer_set_timestamp_scale(mkv_writer_t* w, int64_t scale) {
    if (w == nullptr) { set_error("set_timestamp_scale: null writer"); return 1; }
    if (w->headers_written) {
        set_error("set_timestamp_scale: too late, headers already written");
        return 1;
    }
    if (scale < 1) { set_error("set_timestamp_scale: must be >= 1"); return 1; }
    w->timestamp_scale = scale;
    return 0;
}


/* ------------------------------------------------------------------ */
/* Tracks                                                              */
/* ------------------------------------------------------------------ */

extern "C" mkv_track_t* mkv_writer_add_track(mkv_writer_t* w,
                                              mkv_shim_track_type type,
                                              const mkv_shim_track_info* info) {
    if (w == nullptr || info == nullptr) {
        set_error("add_track: null arg");
        return nullptr;
    }
    if (w->headers_written) {
        set_error("add_track: too late, headers already written");
        return nullptr;
    }
    if (info->codec_id == nullptr || info->codec_id[0] == 0) {
        set_error("add_track: codec_id required");
        return nullptr;
    }

    if (w->tracks == nullptr) {
        w->tracks = &AddNewChild<KaxTracks>(w->segment);
    }

    auto handle = std::make_unique<mkv_track_st>();
    handle->type = type;
    handle->track_number =
        static_cast<uint32_t>(w->track_handles.size() + 1);

    KaxTrackEntry& te = AddNewChild<KaxTrackEntry>(*w->tracks);
    te.SetGlobalTimecodeScale(w->timestamp_scale);

    GetChild<KaxTrackNumber>(te).SetValue(handle->track_number);
    GetChild<KaxTrackUID>(te).SetValue(handle->track_number);

    uint64 track_type_value = track_subtitle;
    if (type == MKV_SHIM_TRACK_VIDEO) track_type_value = track_video;
    else if (type == MKV_SHIM_TRACK_AUDIO) track_type_value = track_audio;
    GetChild<KaxTrackType>(te).SetValue(track_type_value);

    GetChild<KaxTrackFlagDefault>(te).SetValue(
        (info->mkv_flags & MKV_SHIM_TRACK_FLAG_DEFAULT) ? 1 : 0);
    if (info->mkv_flags & MKV_SHIM_TRACK_FLAG_FORCED) {
        GetChild<KaxTrackFlagForced>(te).SetValue(1);
    }
    GetChild<KaxTrackFlagLacing>(te).SetValue(
        (info->mkv_flags & MKV_SHIM_TRACK_FLAG_LACING) ? 1 : 0);

    if (info->lang != nullptr && info->lang[0] != 0) {
        *static_cast<EbmlString*>(&GetChild<KaxTrackLanguage>(te)) = info->lang;
    }

    *static_cast<EbmlString*>(&GetChild<KaxCodecID>(te)) = info->codec_id;

    if (info->codec_private != nullptr && info->codec_private_size > 0) {
        GetChild<KaxCodecPrivate>(te).CopyBuffer(
            info->codec_private, info->codec_private_size);
    }

    if (info->default_duration_ns > 0) {
        GetChild<KaxTrackDefaultDuration>(te).SetValue(info->default_duration_ns);
    }
    if (info->min_cache > 0) {
        GetChild<KaxTrackMinCache>(te).SetValue(info->min_cache);
    }
    if (info->name != nullptr && info->name[0] != 0) {
        *static_cast<EbmlUnicodeString*>(&GetChild<KaxTrackName>(te)) =
            utf8_to_utf(info->name);
    }

    if (type == MKV_SHIM_TRACK_VIDEO) {
        KaxTrackVideo& vt = GetChild<KaxTrackVideo>(te);
        if (info->pixel_h > 0) GetChild<KaxVideoPixelWidth>(vt).SetValue(info->pixel_h);
        if (info->pixel_v > 0) GetChild<KaxVideoPixelHeight>(vt).SetValue(info->pixel_v);
        if (info->display_h > 0) {
            GetChild<KaxVideoDisplayWidth>(vt).SetValue(info->display_h);
            GetChild<KaxVideoDisplayHeight>(vt).SetValue(info->display_v);
            GetChild<KaxVideoDisplayUnit>(vt).SetValue(0);
        }
        if (info->stereo_mode != 0) {
            GetChild<KaxVideoStereoMode>(vt).SetValue(info->stereo_mode);
        }
        /* Colour metadata — KaxVideoColour container with H.273 integer
         * codes. Each child is only written when the corresponding field
         * is non-zero (0 means "unspecified" in our wire format). */
        if (info->color_primaries > 0 || info->color_transfer > 0 ||
            info->color_matrix > 0 || info->color_range > 0) {
            KaxVideoColour& vc = GetChild<KaxVideoColour>(vt);
            if (info->color_primaries > 0)
                GetChild<KaxVideoColourPrimaries>(vc).SetValue(info->color_primaries);
            if (info->color_transfer > 0)
                GetChild<KaxVideoColourTransferCharacter>(vc).SetValue(info->color_transfer);
            if (info->color_matrix > 0)
                GetChild<KaxVideoColourMatrix>(vc).SetValue(info->color_matrix);
            if (info->color_range > 0)
                GetChild<KaxVideoColourRange>(vc).SetValue(info->color_range);
        }
    } else if (type == MKV_SHIM_TRACK_AUDIO) {
        KaxTrackAudio& at = GetChild<KaxTrackAudio>(te);
        if (info->sample_rate > 0)
            GetChild<KaxAudioSamplingFreq>(at).SetValue((double)info->sample_rate);
        if (info->channels_count > 0)
            GetChild<KaxAudioChannels>(at).SetValue(info->channels_count);
        if (info->bits_per_sample > 0)
            GetChild<KaxAudioBitDepth>(at).SetValue(info->bits_per_sample);
    }

    handle->entry = &te;
    mkv_track_st* raw = handle.get();
    w->track_handles.push_back(std::move(handle));
    return raw;
}


/* ------------------------------------------------------------------ */
/* Headers                                                             */
/* ------------------------------------------------------------------ */

extern "C" int mkv_writer_write_headers(mkv_writer_t* w) {
    if (w == nullptr) { set_error("write_headers: null"); return 1; }
    if (w->headers_written) { set_error("write_headers: already written"); return 1; }

    w->info = &GetChild<KaxInfo>(w->segment);

    GetChild<KaxTimecodeScale>(*w->info).SetValue(w->timestamp_scale);
    GetChild<KaxDuration>(*w->info).SetValue(0.0);

    *static_cast<EbmlUnicodeString*>(&GetChild<KaxMuxingApp>(*w->info)) =
        utf8_to_utf("libmkv_shim 0.1");
    *static_cast<EbmlUnicodeString*>(&GetChild<KaxWritingApp>(*w->info)) =
        utf8_to_utf(w->writing_app.c_str());

    if (!w->title.empty()) {
        *static_cast<EbmlUnicodeString*>(&GetChild<KaxTitle>(*w->info)) =
            utf8_to_utf(w->title.c_str());
    }

    GetChild<KaxDateUTC>(*w->info).SetEpochDate(std::time(nullptr));

    binary seg_uid[16];
    std::random_device rd;
    std::mt19937_64 gen(rd());
    for (int i = 0; i < 16; ++i) seg_uid[i] = (binary)(gen() & 0xff);
    GetChild<KaxSegmentUID>(*w->info).CopyBuffer(seg_uid, 16);

    w->info->Render(*w->file, true);

    if (w->tracks == nullptr) {
        set_error("write_headers: no tracks added");
        return 1;
    }
    w->tracks->Render(*w->file);

    update_seek_entry(w->seek_info,   *w->file, *w->info,   w->segment);
    update_seek_entry(w->seek_tracks, *w->file, *w->tracks, w->segment);

    w->headers_written = true;
    return 0;
}


/* ------------------------------------------------------------------ */
/* Clusters                                                            */
/* ------------------------------------------------------------------ */

extern "C" int mkv_writer_start_cluster(mkv_writer_t* w, int64_t timecode_ns) {
    if (w == nullptr || !w->headers_written) {
        set_error("start_cluster: headers not written");
        return 1;
    }
    if (w->current_cluster != nullptr) {
        set_error("start_cluster: cluster already open");
        return 1;
    }

    w->current_cluster = &AddNewChild<KaxCluster>(w->segment);
    w->current_cluster->SetSizeInfinite();
    w->current_cluster->SetParent(w->segment);
    w->current_cluster->WriteHead(*w->file, CLUSTER_SIZE_BYTES);

    int64_t scaled = scale_ns(w, timecode_ns);
    w->current_cluster->InitTimecode(scaled, w->timestamp_scale);

    GetChild<KaxClusterTimecode>(*w->current_cluster).SetValue(scaled);
    GetChild<KaxClusterTimecode>(*w->current_cluster).Render(*w->file);

    if (w->prev_cluster != nullptr) {
        GetChild<KaxClusterPrevSize>(*w->current_cluster).SetValue(
            w->current_cluster->GetElementPosition() -
            w->prev_cluster->GetElementPosition());
        GetChild<KaxClusterPrevSize>(*w->current_cluster).Render(*w->file);
    }
    w->current_cluster_has_cue = false;
    return 0;
}

extern "C" int mkv_writer_add_simple_block(mkv_writer_t* w,
                                            mkv_track_t* track,
                                            const uint8_t* data,
                                            uint32_t size,
                                            int64_t timecode_ns,
                                            uint32_t flags) {
    if (w == nullptr || track == nullptr || data == nullptr) {
        set_error("add_simple_block: null"); return 1;
    }
    if (w->current_cluster == nullptr) {
        set_error("add_simple_block: no open cluster"); return 1;
    }
    if (size == 0) { set_error("add_simple_block: zero size"); return 1; }

    auto* blk = new KaxSimpleBlock();
    blk->SetParent(*w->current_cluster);
    blk->SetKeyframe((flags & MKV_SHIM_FRAME_KEYFRAME) != 0);
    blk->SetDiscardable((flags & MKV_SHIM_FRAME_DISCARDABLE) != 0);

    /* libmatroska free callback: we own the buffer, so don't free it */
    auto no_free = [](const DataBuffer&) -> bool { return false; };
    DataBuffer* buf = new DataBuffer(
        const_cast<binary*>(static_cast<const binary*>(data)),
        (uint32)size,
        no_free);

    /* libmatroska's KaxBlock::AddFrame expects timecode in NS (it does
     * the integer division by TimecodeScale itself, internally subtracting
     * the cluster's NS-anchored MinTimecode). MakeMKV pre-adds
     * TIMECODE_SCALE/2 for half-tick rounding of that division; we do
     * the same. Passing scale_units here (as we did before) silently
     * underflowed delta == 0 at 1ms scale (broken playback) and
     * overflowed the int16 assertion at 1µs (Python abort).
     */
    int64_t tc_for_addframe = timecode_ns + (w->timestamp_scale / 2);
    blk->AddFrame(*track->entry, tc_for_addframe, *buf, LACING_NONE);
    blk->Render(*w->file);

    int64_t scaled = scale_ns(w, timecode_ns);
    /* Emit a cue point for the FIRST keyframe in each cluster.
     * MakeMKV does the same in libmkv.cpp (one cue per cluster). */
    if (!w->current_cluster_has_cue && (flags & MKV_SHIM_FRAME_KEYFRAME) != 0) {
        KaxCuePoint& cp = AddNewChild<KaxCuePoint>(w->cues);
        GetChild<KaxCueTime>(cp).SetValue(scaled);
        KaxCueTrackPositions& cpt = GetChild<KaxCueTrackPositions>(cp);
        GetChild<KaxCueTrack>(cpt).SetValue(track->track_number);
        GetChild<KaxCueClusterPosition>(cpt).SetValue(
            w->current_cluster->GetElementPosition() -
            (w->segment.GetElementPosition() + w->segment.HeadSize()));
        w->current_cluster_has_cue = true;
    }

    delete blk;
    return 0;
}

extern "C" int mkv_writer_add_block_group(mkv_writer_t* w,
                                          mkv_track_t* track,
                                          const uint8_t* data,
                                          uint32_t size,
                                          int64_t timecode_ns,
                                          int64_t duration_ns,
                                          uint32_t flags) {
    if (w == nullptr || track == nullptr || data == nullptr) {
        set_error("add_block_group: null"); return 1;
    }
    if (w->current_cluster == nullptr) {
        set_error("add_block_group: no open cluster"); return 1;
    }
    if (size == 0) { set_error("add_block_group: zero size"); return 1; }
    if (duration_ns <= 0) {
        set_error("add_block_group: duration_ns must be positive");
        return 1;
    }

    auto* blkg = new KaxBlockGroup();
    blkg->SetParent(*w->current_cluster);

    /* libmatroska free callback: we own the buffer, so don't free it */
    auto no_free = [](const DataBuffer&) -> bool { return false; };
    DataBuffer* buf = new DataBuffer(
        const_cast<binary*>(static_cast<const binary*>(data)),
        (uint32)size,
        no_free);

    int64_t scaled_tc = scale_ns(w, timecode_ns);
    int64_t scaled_dur = scale_ns(w, duration_ns);
    if (scaled_dur < 1) scaled_dur = 1;  /* never emit zero duration */

    /* See add_simple_block for the unit rationale: AddFrame takes NS
     * with half-tick rounding pre-applied. */
    int64_t tc_for_addframe = timecode_ns + (w->timestamp_scale / 2);
    blkg->AddFrame(*track->entry, tc_for_addframe, *buf, LACING_NONE);
    /* KaxBlockDuration is stored in TimecodeScale units. */
    blkg->SetBlockDuration(static_cast<uint64>(scaled_dur));
    blkg->Render(*w->file);

    /* Cue-point emission policy matches add_simple_block: one cue per
     * cluster on the first keyframe. Subtitle blocks are always treated
     * as keyframes for cue purposes if MKV_SHIM_FRAME_KEYFRAME is set. */
    if (!w->current_cluster_has_cue && (flags & MKV_SHIM_FRAME_KEYFRAME) != 0) {
        KaxCuePoint& cp = AddNewChild<KaxCuePoint>(w->cues);
        GetChild<KaxCueTime>(cp).SetValue(scaled_tc);
        KaxCueTrackPositions& cpt = GetChild<KaxCueTrackPositions>(cp);
        GetChild<KaxCueTrack>(cpt).SetValue(track->track_number);
        GetChild<KaxCueClusterPosition>(cpt).SetValue(
            w->current_cluster->GetElementPosition() -
            (w->segment.GetElementPosition() + w->segment.HeadSize()));
        w->current_cluster_has_cue = true;
    }

    delete blkg;
    return 0;
}

extern "C" int mkv_writer_add_chapter(mkv_writer_t* w,
                                       int64_t time_start_ns,
                                       int64_t time_end_ns,
                                       const char* name,
                                       const char* lang) {
    if (w == nullptr) { set_error("add_chapter: null"); return 1; }
    if (w->finalized) { set_error("add_chapter: finalized"); return 1; }
    pending_chapter_t pc;
    pc.time_start_ns = time_start_ns;
    pc.time_end_ns   = time_end_ns;
    pc.name = name != nullptr ? name : "";
    pc.lang = (lang != nullptr && lang[0] != 0) ? lang : "und";
    w->chapters.push_back(std::move(pc));
    return 0;
}

extern "C" int mkv_writer_set_track_stats(mkv_writer_t* w,
                                          mkv_track_t* track,
                                          uint64_t total_bytes,
                                          uint64_t num_frames,
                                          int64_t total_duration_ns) {
    if (w == nullptr || track == nullptr) {
        set_error("set_track_stats: null"); return 1;
    }
    if (w->finalized) {
        set_error("set_track_stats: finalized"); return 1;
    }
    track->has_stats = true;
    track->stat_bytes = total_bytes;
    track->stat_frames = num_frames;
    track->stat_duration_ns = total_duration_ns;
    return 0;
}

extern "C" int mkv_writer_add_attachment(mkv_writer_t* w,
                                          const char* name,
                                          const char* mime_type,
                                          const uint8_t* data,
                                          uint32_t size) {
    if (w == nullptr || data == nullptr) {
        set_error("add_attachment: null"); return 1;
    }
    if (w->finalized) { set_error("add_attachment: finalized"); return 1; }
    pending_attachment_t pa;
    pa.name = name != nullptr ? name : "";
    pa.mime = mime_type != nullptr ? mime_type : "application/octet-stream";
    pa.data.assign(data, data + size);
    w->attachments.push_back(std::move(pa));
    return 0;
}

extern "C" int mkv_writer_end_cluster(mkv_writer_t* w) {
    if (w == nullptr || w->current_cluster == nullptr) {
        set_error("end_cluster: nothing to close"); return 1;
    }

    GetChild<KaxClusterPosition>(*w->current_cluster).SetValue(
        w->current_cluster->GetElementPosition() -
        (w->segment.GetElementPosition() + w->segment.HeadSize()));
    GetChild<KaxClusterPosition>(*w->current_cluster).Render(*w->file);

    w->current_cluster->ForceSize(
        w->file->getFilePointer() -
        (w->current_cluster->GetElementPosition() +
         w->current_cluster->HeadSize()));
    w->current_cluster->OverwriteHead(*w->file);

    w->prev_cluster = w->current_cluster;
    w->current_cluster = nullptr;
    return 0;
}


/* ------------------------------------------------------------------ */
/* Finalize                                                            */
/* ------------------------------------------------------------------ */

extern "C" int mkv_writer_finalize(mkv_writer_t* w, int64_t max_duration_ns) {
    if (w == nullptr) { set_error("finalize: null"); return 1; }
    if (w->finalized) { set_error("finalize: already finalized"); return 1; }
    if (!w->headers_written) {
        set_error("finalize: headers not written"); return 1;
    }

    if (w->current_cluster != nullptr) {
        if (mkv_writer_end_cluster(w) != 0) return 1;
    }

    GetChild<KaxDuration>(*w->info).SetValue(
        (double)scale_ns(w, max_duration_ns));
    GetChild<KaxDuration>(*w->info).OverwriteData(*w->file, true);

    /* Cues: render if we have any, else void the seek slot. */
    if (w->cues.ListSize() > 0) {
        w->cues.SetGlobalTimecodeScale(w->timestamp_scale);
        w->cues.Render(*w->file);
        update_seek_entry(w->seek_cues, *w->file, w->cues, w->segment);
    } else {
        render_void_over(w->seek_cues, *w->file);
    }

    /* Chapters: build KaxChapters from pending list, render, update seek. */
    if (!w->chapters.empty()) {
        KaxChapters& chapters = AddNewChild<KaxChapters>(w->segment);
        KaxEditionEntry& edition = AddNewChild<KaxEditionEntry>(chapters);
        GetChild<KaxEditionFlagDefault>(edition).SetValue(1);
        /* Random EditionUID */
        std::random_device rd;
        std::mt19937_64 gen(rd());
        GetChild<KaxEditionUID>(edition).SetValue(gen());

        for (size_t i = 0; i < w->chapters.size(); ++i) {
            const pending_chapter_t& pc = w->chapters[i];
            KaxChapterAtom& atom = AddNewChild<KaxChapterAtom>(edition);
            GetChild<KaxChapterUID>(atom).SetValue(gen());
            GetChild<KaxChapterTimeStart>(atom).SetValue(pc.time_start_ns);
            int64_t end = pc.time_end_ns;
            if (end < 0) {
                /* Open-ended: use next chapter's start or finalize duration */
                if (i + 1 < w->chapters.size()) {
                    end = w->chapters[i + 1].time_start_ns;
                } else {
                    end = max_duration_ns;
                }
            }
            GetChild<KaxChapterTimeEnd>(atom).SetValue(end);
            if (!pc.name.empty()) {
                KaxChapterDisplay& disp = AddNewChild<KaxChapterDisplay>(atom);
                *static_cast<EbmlString*>(&GetChild<KaxChapterLanguage>(disp)) =
                    pc.lang.c_str();
                *static_cast<EbmlUnicodeString*>(&GetChild<KaxChapterString>(disp)) =
                    utf8_to_utf(pc.name.c_str());
            }
        }
        chapters.Render(*w->file, true);
        update_seek_entry(w->seek_chapters, *w->file, chapters, w->segment);
    } else {
        render_void_over(w->seek_chapters, *w->file);
    }

    /* Attachments: render if any. */
    if (!w->attachments.empty()) {
        KaxAttachments& attachments = AddNewChild<KaxAttachments>(w->segment);
        std::random_device rd;
        std::mt19937_64 gen(rd());
        for (size_t i = 0; i < w->attachments.size(); ++i) {
            const pending_attachment_t& pa = w->attachments[i];
            KaxAttached& att = AddNewChild<KaxAttached>(attachments);
            *static_cast<EbmlUnicodeString*>(&GetChild<KaxFileName>(att)) =
                utf8_to_utf(pa.name.c_str());
            *static_cast<EbmlString*>(&GetChild<KaxMimeType>(att)) =
                pa.mime.c_str();
            GetChild<KaxFileUID>(att).SetValue(gen());
            GetChild<KaxFileData>(att).CopyBuffer(
                pa.data.data(),
                static_cast<uint32>(pa.data.size()));
        }
        attachments.Render(*w->file);
        update_seek_entry(w->seek_attach, *w->file, attachments, w->segment);
    } else {
        render_void_over(w->seek_attach, *w->file);
    }

    /* Per-track statistics tags — emitted in the MakeMKV / mkvmerge
     * convention. Tracks without stats are skipped; if no track had
     * stats, the seek-head slot is voided. */
    bool any_stats = false;
    for (auto& t : w->track_handles) {
        if (t && t->has_stats) { any_stats = true; break; }
    }
    if (any_stats) {
        KaxTags& tags_el = AddNewChild<KaxTags>(w->segment);
        /* mkvmerge writes the WritingApp + UTC stamp once per track tag. */
        time_t now = time(nullptr);
        char date_buf[32];
        struct tm utc;
        gmtime_r(&now, &utc);
        strftime(date_buf, sizeof(date_buf), "%Y-%m-%d %H:%M:%S", &utc);
        std::string date_str = date_buf;

        auto add_simple = [](KaxTag& parent, const char* name, const std::string& value) {
            KaxTagSimple& s = AddNewChild<KaxTagSimple>(parent);
            *static_cast<EbmlUnicodeString*>(&GetChild<KaxTagName>(s)) =
                utf8_to_utf(name);
            *static_cast<EbmlUnicodeString*>(&GetChild<KaxTagString>(s)) =
                utf8_to_utf(value.c_str());
        };
        for (auto& t : w->track_handles) {
            if (!t || !t->has_stats) continue;
            KaxTag& tag = AddNewChild<KaxTag>(tags_el);
            KaxTagTargets& targets = GetChild<KaxTagTargets>(tag);
            GetChild<KaxTagTrackUID>(targets).SetValue(t->track_number);
            GetChild<KaxTagTargetTypeValue>(targets).SetValue(50);  // TRACK
            /* BPS = bytes * 8 * 1e9 / duration_ns */
            uint64_t bps = 0;
            if (t->stat_duration_ns > 0) {
                bps = static_cast<uint64_t>(
                    (static_cast<double>(t->stat_bytes) * 8.0 * 1e9) /
                    static_cast<double>(t->stat_duration_ns));
            }
            /* DURATION as HH:MM:SS.mmm */
            int64_t total_ms = t->stat_duration_ns / 1000000;
            int hours = (int)(total_ms / 3600000);
            int mins = (int)((total_ms / 60000) % 60);
            int secs = (int)((total_ms / 1000) % 60);
            int ms = (int)(total_ms % 1000);
            char dur_buf[64];
            snprintf(dur_buf, sizeof(dur_buf),
                     "%02d:%02d:%02d.%03d", hours, mins, secs, ms);

            char num_buf[32];
            snprintf(num_buf, sizeof(num_buf), "%llu", (unsigned long long)bps);
            add_simple(tag, "BPS", num_buf);
            add_simple(tag, "DURATION", dur_buf);
            snprintf(num_buf, sizeof(num_buf), "%llu",
                     (unsigned long long)t->stat_frames);
            add_simple(tag, "NUMBER_OF_FRAMES", num_buf);
            snprintf(num_buf, sizeof(num_buf), "%llu",
                     (unsigned long long)t->stat_bytes);
            add_simple(tag, "NUMBER_OF_BYTES", num_buf);
            add_simple(tag, "_STATISTICS_WRITING_APP",
                       w->writing_app.empty() ? "remux-toolkit" : w->writing_app);
            add_simple(tag, "_STATISTICS_WRITING_DATE_UTC", date_str);
            add_simple(tag, "_STATISTICS_TAGS",
                       "BPS DURATION NUMBER_OF_FRAMES NUMBER_OF_BYTES");
        }
        tags_el.Render(*w->file);
        update_seek_entry(w->seek_tags, *w->file, tags_el, w->segment);
    } else {
        render_void_over(w->seek_tags, *w->file);
    }

    w->segment.ForceSize(
        w->file->getFilePointer() -
        (w->segment.GetElementPosition() + w->segment.HeadSize()));
    w->segment.OverwriteHead(*w->file);

    w->finalized = true;
    return 0;
}
