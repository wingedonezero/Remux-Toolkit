"""
Multi-fd ffmpeg muxer.

Spawns a single ffmpeg subprocess with one input pipe per elementary stream
(plus an optional ffmetadata input for chapters), running with `-c copy` so
ffmpeg's only job is to take the clean ES bytes and write a streaming MKV.

Two things matter here:

  1. **fd inheritance for the input pipes.** We use `pass_fds` so the child
     ffmpeg sees the read ends at the same fd numbers; we reference them in
     the command line as `pipe:<n>`. The parent closes the read ends after
     spawn so EOF propagates correctly when we close the write ends.

  2. **A writer thread per stream.** Each pipe has a ~64 KB kernel buffer;
     if we wrote all video, then all audio, ffmpeg would block reading the
     audio pipe and the video write would block waiting for the audio side
     to drain — classic pipe deadlock. The per-stream queue+thread design
     drains every pipe in parallel.

The output MKV is written incrementally to disk by ffmpeg (Matroska's
SeekHead is fixed up at finalization), so this is genuinely streaming —
working memory stays in the KBs.
"""
from __future__ import annotations

import os
import queue
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class StreamSpec:
    """One elementary stream to feed into the muxer."""
    codec_id: str          # ffmpeg `-f` value: "mpeg2video", "ac3", "dts", "pcm_s16be", "dvd_subtitle"
    language: str = ""     # ISO 639-2 code (e.g. "eng"), used for MKV metadata
    title: str = ""        # optional human label
    extra_input_args: list[str] = field(default_factory=list)
                            # extra args BEFORE `-i pipe:N` (e.g. "-ar", "48000")
    extra_output_args: list[str] = field(default_factory=list)
                            # extra args after this stream's `-map` (e.g. codec opts)


class FFmpegMuxer:
    """Drives `ffmpeg -c copy` with multiple piped ES inputs.

    Usage:

        m = FFmpegMuxer(Path("out.mkv"))
        v_idx = m.add_stream(StreamSpec("mpeg2video"))
        a_idx = m.add_stream(StreamSpec("ac3", language="eng"))
        m.set_chapters(my_chapters)  # optional
        m.start()
        for pkt in demuxed:
            m.write(v_idx if pkt.is_video else a_idx, pkt.bytes)
        rc = m.finish()  # waits for ffmpeg; returns its exit code
    """

    def __init__(self, output_path: Path,
                 *, ffmpeg_bin: str = "ffmpeg",
                 log_line_callback: Optional[Callable[[str, str], None]] = None,
                 queue_max: int = 64):
        self.output_path = Path(output_path)
        self.ffmpeg_bin = ffmpeg_bin
        self._log = log_line_callback
        self._queue_max = queue_max
        self._streams: list[StreamSpec] = []
        # pipe + queue + thread per stream
        self._pipe_read: list[int] = []
        self._pipe_write: list[int] = []
        self._queues: list[queue.Queue] = []
        self._threads: list[threading.Thread] = []
        self._chapters_ffmetadata: Optional[str] = None
        self._metadata_path: Optional[Path] = None
        self._proc: Optional[subprocess.Popen] = None
        self._stderr_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ setup

    def add_stream(self, spec: StreamSpec) -> int:
        if self._proc is not None:
            raise RuntimeError("Cannot add streams after start()")
        idx = len(self._streams)
        self._streams.append(spec)
        return idx

    def set_chapters_ffmetadata(self, ffmetadata_text: str) -> None:
        """Provide chapters as an FFmetadata1 document (see
        demux.chapters.chapters_to_ffmetadata)."""
        if self._proc is not None:
            raise RuntimeError("Cannot set chapters after start()")
        self._chapters_ffmetadata = ffmetadata_text

    # ------------------------------------------------------------------ run

    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("Already started")
        if not self._streams:
            raise RuntimeError("No streams added")

        # 1) Optional ffmetadata file (chapters)
        meta_input_count = 0
        cmd: list[str] = [self.ffmpeg_bin, "-hide_banner", "-y"]
        if self._chapters_ffmetadata is not None:
            fd, name = tempfile.mkstemp(suffix=".ffmetadata", prefix="dvdrip-")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(self._chapters_ffmetadata)
            except Exception:
                os.close(fd)
                raise
            self._metadata_path = Path(name)
            cmd += ["-i", str(self._metadata_path)]
            meta_input_count = 1

        # 2) One -f CODEC -i pipe:N per stream
        for spec in self._streams:
            r, w = os.pipe()
            self._pipe_read.append(r)
            self._pipe_write.append(w)
            self._queues.append(queue.Queue(maxsize=self._queue_max))
            cmd += list(spec.extra_input_args)
            cmd += ["-f", spec.codec_id, "-i", f"pipe:{r}"]

        # 3) -map for each stream input (offset by metadata input if present)
        for i, spec in enumerate(self._streams):
            input_index = i + meta_input_count
            cmd += ["-map", f"{input_index}:0"]
            if spec.language:
                cmd += [f"-metadata:s:{i}", f"language={spec.language}"]
            if spec.title:
                cmd += [f"-metadata:s:{i}", f"title={spec.title}"]
            cmd += list(spec.extra_output_args)

        # 4) Chapters mapping
        if self._chapters_ffmetadata is not None:
            cmd += ["-map_chapters", "0"]

        # 5) Stream copy + output
        cmd += ["-c", "copy", str(self.output_path)]

        if self._log:
            self._log("info", "ffmpeg cmd: " + " ".join(cmd))

        # Inherit read ends; non-inheritable in parent so we can close them
        # without affecting the child once it has duped them.
        for r in self._pipe_read:
            os.set_inheritable(r, True)

        self._proc = subprocess.Popen(
            cmd,
            pass_fds=tuple(self._pipe_read),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Close read ends in parent — child already has them
        for r in self._pipe_read:
            os.close(r)
        self._pipe_read = []

        # Start a writer thread per stream
        for idx in range(len(self._streams)):
            t = threading.Thread(
                target=self._writer_loop,
                args=(idx, self._pipe_write[idx], self._queues[idx]),
                daemon=True,
                name=f"ffmpeg-writer-{idx}",
            )
            t.start()
            self._threads.append(t)

        # Drain ffmpeg stderr (which is where ffmpeg logs status + errors)
        if self._proc.stderr is not None:
            self._stderr_thread = threading.Thread(
                target=self._stderr_loop,
                args=(self._proc.stderr,),
                daemon=True,
                name="ffmpeg-stderr",
            )
            self._stderr_thread.start()

    def write(self, stream_idx: int, data: bytes) -> None:
        if not data:
            return
        if self._proc is None:
            raise RuntimeError("Muxer not started")
        # Fail fast if ffmpeg already exited — without this, queue.put() blocks
        # forever once the queue fills (writer thread has died on broken pipe).
        if self._proc.poll() is not None:
            raise BrokenPipeError(
                f"ffmpeg exited (rc={self._proc.returncode}) before all packets written"
            )
        try:
            self._queues[stream_idx].put(data, timeout=10.0)
        except queue.Full as e:
            # If a queue stays full this long, either ffmpeg crashed without
            # us noticing or we have a real backpressure problem worth surfacing.
            if self._proc.poll() is not None:
                raise BrokenPipeError(
                    f"ffmpeg exited (rc={self._proc.returncode}) while waiting on stream {stream_idx}"
                ) from e
            raise

    def finish(self, timeout: Optional[float] = None) -> int:
        """Signal EOF on every stream, wait for ffmpeg, return its exit code."""
        if self._proc is None:
            raise RuntimeError("Not started")
        # Sentinel = stop writer threads (which will close their pipes)
        for q in self._queues:
            q.put(None)
        for t in self._threads:
            t.join(timeout=timeout)
        rc = self._proc.wait(timeout=timeout)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)
        if self._metadata_path is not None:
            try:
                self._metadata_path.unlink()
            except OSError:
                pass
            self._metadata_path = None
        return rc

    def abort(self) -> None:
        """Force-kill ffmpeg and close all pipes. Used on user-cancel."""
        if self._proc is None:
            return
        for q in self._queues:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
        for w in self._pipe_write:
            try:
                os.close(w)
            except OSError:
                pass
        self._pipe_write = []
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        if self._metadata_path is not None:
            try:
                self._metadata_path.unlink()
            except OSError:
                pass
            self._metadata_path = None

    # ------------------------------------------------------------------ private

    def _writer_loop(self, idx: int, w_fd: int, q: queue.Queue) -> None:
        try:
            while True:
                data = q.get()
                if data is None:
                    break
                try:
                    written = 0
                    while written < len(data):
                        n = os.write(w_fd, data[written:])
                        if n <= 0:
                            return
                        written += n
                except BrokenPipeError:
                    return
                except OSError:
                    return
        finally:
            try:
                os.close(w_fd)
            except OSError:
                pass

    def _stderr_loop(self, stderr) -> None:
        try:
            for raw in stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if self._log:
                    sev = "error" if "Error" in line or "error" in line[:30] else "info"
                    self._log(sev, line)
        except Exception:
            pass
