"""Bounded audio/video chunks; actual streams and duration checked with ffprobe."""
from dataclasses import dataclass
import json
import math
from pathlib import Path
import subprocess
import threading
import time
import uuid


class MediaError(Exception):
    pass


@dataclass(frozen=True)
class MediaChunk:
    path: Path
    start: float
    end: float
    received_at: float
    modality: str
    target_id: str
    session_id: str
    has_audio: bool = True
    has_video: bool = True


class MediaCollector:
    def __init__(self, ffmpeg="ffmpeg", ffprobe="ffprobe", *, clock=time.time):
        self.ffmpeg, self.ffprobe, self.clock = str(ffmpeg), str(ffprobe), clock

    def capture(self, target, output_dir, duration=30, offset=0, cancel_event=None):
        if (not math.isfinite(duration) or not 1 <= duration <= 30
                or not math.isfinite(offset) or offset < 0):
            raise MediaError("invalid_interval")
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / ("chunk_"+uuid.uuid4().hex+".mp4")
        headers = "".join(k+": "+v+"\r\n" for k, v in target.headers.items()
                          if k in {"Referer", "User-Agent"} and "\r" not in v and "\n" not in v)
        args = [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1"]
        for url in ([target.url, target.audio_url] if target.audio_url else [target.url]):
            args += ["-rw_timeout", "12000000", "-headers", headers]
            if not target.is_live:
                args += ["-ss", str(offset)]
            args += ["-i", url]
        args += ["-map", "0:v:0", "-map", "1:a:0" if target.audio_url else "0:a:0",
                 "-t", str(duration), "-vf", "scale=426:-2,fps=2", "-c:v", "libx264",
                 "-preset", "ultrafast", "-crf", "30", "-threads", "1", "-c:a", "aac", "-ac", "1",
                 "-ar", "16000", "-b:a", "32k", "-movflags", "+faststart", "-y", str(output)]
        started = self.clock()
        proc = None
        try:
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            deadline = time.monotonic()+duration+35
            while proc.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    raise MediaError("cancelled")
                if time.monotonic() >= deadline:
                    raise MediaError("capture_timeout")
                time.sleep(.1)
            if proc.returncode:
                raise MediaError("capture_failed")
            probe = subprocess.run([self.ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,duration",
                                    "-of", "json", str(output)], capture_output=True, timeout=10,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if probe.returncode:
                raise MediaError("probe_failed")
            info = json.loads(probe.stdout)
            streams = info.get("streams", [])
            if not {"video", "audio"} <= {s.get("codec_type") for s in streams}:
                raise MediaError("missing_audio_video")
            lengths = [float(s["duration"]) for s in streams if s.get("codec_type") in {"audio", "video"} and s.get("duration")]
            actual = min(lengths) if len(lengths) >= 2 else float(info["format"]["duration"])
            if not math.isfinite(actual) or actual < .5 or actual > duration+1:
                raise MediaError("invalid_media_duration")
            return MediaChunk(output, offset, offset+min(actual, duration), self.clock(), "mixed", target.identity, target.session_id)
        except MediaError:
            output.unlink(missing_ok=True)
            raise
        except Exception:
            output.unlink(missing_ok=True)
            raise MediaError("capture_or_probe_failed") from None
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)


class LiveRecorder:
    """Continuous segment capture. Only closed files enter the bounded queue.

    start/end are this recorder's media timeline, never claimed broadcast PTS.
    Dropped queued intervals are explicitly exposed through ``skipped``.
    """
    def __init__(self, target, output_dir, *, ffmpeg="ffmpeg", ffprobe="ffprobe",
                 duration=30, max_queue=3, clock=time.time):
        if not target.is_live or not 1 <= duration <= 30 or not 1 <= max_queue <= 10:
            raise MediaError("invalid_live_recorder")
        self.target, self.directory = target, Path(output_dir) / ("live_"+uuid.uuid4().hex)
        self.ffmpeg, self.ffprobe = str(ffmpeg), str(ffprobe)
        self.duration, self.max_queue, self.clock = duration, max_queue, clock
        self.skipped = []
        self._queue, self._last_seen, self._error = [], -1, None
        self._closed_at = {}
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._proc = self._thread = None

    def start(self):
        if self._proc is not None:
            raise MediaError("already_started")
        self.directory.mkdir(parents=True, exist_ok=True)
        headers = "".join(k+": "+v+"\r\n" for k,v in self.target.headers.items()
                          if k in {"Referer", "User-Agent"} and "\r" not in v and "\n" not in v)
        args = [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1"]
        for url in ([self.target.url, self.target.audio_url] if self.target.audio_url else [self.target.url]):
            args += ["-rw_timeout", "12000000", "-headers", headers, "-i", url]
        args += ["-map", "0:v:0", "-map", "1:a:0" if self.target.audio_url else "0:a:0",
                 "-vf", "setpts=PTS-STARTPTS,scale=426:-2,fps=2", "-af", "asetpts=PTS-STARTPTS",
                 "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                 "-threads", "1", "-g", str(int(self.duration*2)), "-keyint_min", str(int(self.duration*2)), "-sc_threshold", "0",
                 "-force_key_frames", f"expr:gte(t,n_forced*{self.duration})",
                 "-c:a", "aac", "-ac", "1", "-ar", "16000", "-b:a", "32k",
                 "-f", "segment", "-segment_time", str(self.duration), "-segment_time_delta", "0.1", "-reset_timestamps", "1",
                 "-segment_format", "mp4", str(self.directory / "part_%06d.mp4")]
        self.started_at = self.clock()
        try:
            self._proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            raise MediaError("live_start_failed") from None
        self._thread = threading.Thread(target=self._monitor, name="live-segments", daemon=True)
        self._thread.start()
        return self

    def _monitor(self):
        try:
            while not self._stop.is_set():
                paths = sorted(self.directory.glob("part_*.mp4"))
                ended = self._proc.poll() is not None
                closed = paths if ended else paths[:-1]
                with self._condition:
                    for path in closed:
                        index = int(path.stem.split("_")[-1])
                        if index > self._last_seen:
                            self._last_seen = index
                            self._closed_at[index] = self.clock()
                            self._queue.append((index, path))
                    while len(self._queue) > self.max_queue:
                        index, path = self._queue.pop(0)
                        self._closed_at.pop(index, None)
                        self.skipped.append(dict(start=index*self.duration, end=(index+1)*self.duration, reason="queue_overrun"))
                        # Bounded gap diagnostics; callers persist their own coverage log.
                        self.skipped[:] = self.skipped[-1000:]
                        path.unlink(missing_ok=True)
                    self._condition.notify_all()
                for path in closed:
                    try:
                        if self.clock()-path.stat().st_mtime > 600:
                            path.unlink(missing_ok=True)
                    except FileNotFoundError:
                        pass
                if ended:
                    self._error = "live_stream_ended"
                    break
                self._stop.wait(.2)
        except Exception:
            self._error = "live_monitor_failed"
        finally:
            with self._condition:
                self._condition.notify_all()

    def drain_skipped(self):
        with self._condition:
            gaps, self.skipped = self.skipped, []
            return gaps

    def next_clip(self, timeout=45, *, latest=True):
        deadline = time.monotonic()+timeout
        with self._condition:
            while not self._queue:
                if self._error or self._stop.is_set():
                    raise MediaError(self._error or "stopped")
                left = deadline-time.monotonic()
                if left <= 0:
                    raise TimeoutError("live_segment_timeout")
                self._condition.wait(min(left, 1))
            while latest and len(self._queue) > 1:
                index, path = self._queue.pop(0)
                self._closed_at.pop(index, None)
                path.unlink(missing_ok=True)
                self.skipped.append(dict(start=index*self.duration,end=(index+1)*self.duration,reason="understanding_pacing"))
                self.skipped[:] = self.skipped[-1000:]
            index, path = self._queue.pop(0)
            received = self._closed_at.pop(index, self.clock())
        try:
            probe = subprocess.run([self.ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,duration",
                                    "-of", "json", str(path)], capture_output=True, timeout=10,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            info = json.loads(probe.stdout)
            streams = info.get("streams", [])
            if probe.returncode or not {"audio", "video"} <= {s.get("codec_type") for s in streams}:
                raise MediaError("missing_audio_video")
            lengths = [float(s["duration"]) for s in streams if s.get("duration")]
            actual = min(lengths) if len(lengths) >= 2 else float(info["format"]["duration"])
            if not math.isfinite(actual) or not .5 <= actual <= self.duration+1:
                raise MediaError("invalid_media_duration")
            return MediaChunk(path, index*self.duration, index*self.duration+min(actual,self.duration),
                              received, "mixed", self.target.identity, self.target.session_id)
        except Exception as exc:
            path.unlink(missing_ok=True)
            with self._condition:
                self.skipped.append(dict(start=index*self.duration,end=(index+1)*self.duration,reason="invalid_segment"))
                self.skipped[:] = self.skipped[-1000:]
            raise MediaError("invalid_live_segment:" + (str(exc) if isinstance(exc, MediaError) else type(exc).__name__)) from None

    def stop(self):
        self._stop.set()
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=3)
        if self._thread is not None:
            self._thread.join(timeout=3)
        with self._condition:
            self._condition.notify_all()
        # Only recorder-owned artifacts; returned clips must already be consumed.
        for path in self.directory.glob("part_*.mp4"):
            path.unlink(missing_ok=True)
        try:
            self.directory.rmdir()
        except OSError:
            pass
