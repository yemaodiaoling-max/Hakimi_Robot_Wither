"""Cheap "nothing changed" gate: skip model calls for static, silent chunks.

One ffmpeg pass per chunk extracts a few 32x18 grayscale thumbnails and the
audio peak/mean level. Every failure path returns "observe" (fail open).
"""
from dataclasses import dataclass
import math
import re
import subprocess
import time

THUMB_W, THUMB_H = 32, 18
THUMB_BYTES = THUMB_W * THUMB_H
THUMBS = 4

# Calibrated on synthetic crf30/2fps clips (see tests/test_scene_gate.py).
# Static+heavy sensor noise measures <=0.9; a 24px box crossing the frame measures 2.3.
MEAN_DIFF_MAX = 1.5          # mean |pixel delta| on 0..255 below which two thumbnails are "the same"
CHANGED_PIXEL_DELTA = 24     # a pixel counts as changed above this delta
CHANGED_FRACTION_MAX = 0.01  # ...and at most 1% of pixels may have changed
SILENCE_MAX_DB = -50.0       # chunk peak below this: silent / no speech
SILENCE_MEAN_DB = -58.0      # chunk mean below this as well
LIVE_CAP_SECONDS = 240
VIDEO_CAP_SECONDS = 360

_LEVEL = re.compile(rb'(mean|max)_volume:\s*(-?inf|-?[0-9.]+)\s*dB')


@dataclass(frozen=True)
class Signature:
    frames: tuple
    mean_db: float
    max_db: float


@dataclass(frozen=True)
class GateDecision:
    skip: bool
    reason: str
    details: dict


def _level(raw):
    return -math.inf if raw.endswith(b'inf') else float(raw)


def frame_motion(a, b):
    """(mean abs delta, fraction of pixels above CHANGED_PIXEL_DELTA) for two thumbnails."""
    total = changed = 0
    for x, y in zip(a, b):
        d = x - y if x > y else y - x
        total += d
        if d > CHANGED_PIXEL_DELTA:
            changed += 1
    n = max(1, min(len(a), len(b)))
    return total / n, changed / n


def frames_moving(first, second, mean_max=MEAN_DIFF_MAX, fraction_max=CHANGED_FRACTION_MAX):
    mean, fraction = frame_motion(first, second)
    return mean > mean_max or fraction > fraction_max


class StaticSceneGate:
    def __init__(self, ffmpeg='ffmpeg', *, mean_diff=MEAN_DIFF_MAX, changed_fraction=CHANGED_FRACTION_MAX,
                 silence_max_db=SILENCE_MAX_DB, silence_mean_db=SILENCE_MEAN_DB,
                 live_cap=LIVE_CAP_SECONDS, video_cap=VIDEO_CAP_SECONDS, timeout=10, clock=time.time):
        self.ffmpeg, self.mean_diff, self.changed_fraction = str(ffmpeg), mean_diff, changed_fraction
        self.silence_max_db, self.silence_mean_db = silence_max_db, silence_mean_db
        self.live_cap, self.video_cap, self.timeout, self.clock = live_cap, video_cap, timeout, clock
        self._key = self._baseline = None
        self._baseline_at = 0.
        self.skips_in_row = 0

    def signature(self, path, duration):
        duration = duration if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0 else 30.
        rate = THUMBS / max(.5, duration)
        args = [self.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'info', '-threads', '1', '-i', str(path),
                '-map', '0:v:0', '-vf', f'fps={rate:.6f},scale={THUMB_W}:{THUMB_H}:flags=area,format=gray',
                '-frames:v', str(THUMBS), '-f', 'rawvideo', 'pipe:1',
                '-map', '0:a:0', '-vn', '-af', 'volumedetect', '-f', 'null', '-']
        proc = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=self.timeout, check=True,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        raw = proc.stdout
        count = len(raw) // THUMB_BYTES
        if count < 2 or len(raw) % THUMB_BYTES:
            raise ValueError('thumbnails unavailable')
        levels = {k.decode(): _level(v) for k, v in _LEVEL.findall(proc.stderr)}
        if 'mean' not in levels or 'max' not in levels:
            raise ValueError('audio level unavailable')
        frames = tuple(raw[i * THUMB_BYTES:(i + 1) * THUMB_BYTES] for i in range(count))
        return Signature(frames, levels['mean'], levels['max'])

    def silent(self, sig):
        return sig.max_db < self.silence_max_db and sig.mean_db < self.silence_mean_db

    def _moving(self, first, second):
        return frames_moving(first, second, self.mean_diff, self.changed_fraction)

    def evaluate(self, key, live, sig, now):
        """Decide only; call ``commit`` when the chunk is actually sent to the model."""
        cap = self.live_cap if live else self.video_cap
        if self._key != key or self._baseline is None:
            return GateDecision(False, 'no_baseline', {})
        waited = now - self._baseline_at
        details = dict(max_db=round(sig.max_db, 1), mean_db=round(sig.mean_db, 1),
                       since_observed=round(waited, 1), skips_in_row=self.skips_in_row)
        if waited >= cap or waited < 0:
            return GateDecision(False, 'cap_reached', details)
        if not self.silent(sig):
            return GateDecision(False, 'audible', details)
        if any(self._moving(a, b) for a, b in zip(sig.frames, sig.frames[1:])):
            return GateDecision(False, 'motion_in_chunk', details)
        pairs = [frame_motion(a, b) for a, b in zip(sig.frames, self._baseline.frames)]
        mean, fraction = max(p[0] for p in pairs), max(p[1] for p in pairs)
        details.update(diff=round(mean, 2), changed=round(fraction, 4))
        if mean > self.mean_diff or fraction > self.changed_fraction:
            return GateDecision(False, 'scene_changed', details)
        return GateDecision(True, 'static_silent', details)

    def commit(self, key, sig, now):
        self._key, self._baseline, self._baseline_at, self.skips_in_row = key, sig, now, 0

    def note_skip(self):
        self.skips_in_row += 1
        return self.skips_in_row

    def check(self, chunk, key, live, now):
        """Fail-open wrapper. Returns (decision, signature); signature is None on gate failure."""
        try:
            sig = self.signature(chunk.path, chunk.end - chunk.start)
            return self.evaluate(key, live, sig, now), sig
        except Exception:
            return GateDecision(False, 'gate_error', {}), None
