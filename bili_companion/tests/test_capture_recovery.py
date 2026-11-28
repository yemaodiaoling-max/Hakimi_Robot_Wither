import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bili_companion.media.capture import LiveRecorder, MediaCollector, MediaError, ffmpeg_reason
from bili_companion.platform.bilibili import MediaTarget
from bili_companion.runtime.watcher import error_details

import test_watcher
from test_watcher import FakeCollector

PROBE = dict(streams=[dict(codec_type="video", duration="29.8"), dict(codec_type="audio", duration="30")],
             format=dict(duration="30"))


class ReasonTests(unittest.TestCase):
    def test_codes_never_carry_urls_or_text(self):
        signed = "https://upos.example.bilivideo.com/x.m4s?deadline=1&upsig=secret"
        self.assertEqual(ffmpeg_reason(f"[https @ 0x1] HTTP error 403 Forbidden\n{signed}: Server returned 403 Forbidden"), "http_403")
        self.assertEqual(ffmpeg_reason("Server returned 404 Not Found"), "http_404")
        self.assertEqual(ffmpeg_reason("Connection to tcp://cdn.example.invalid:443 failed: Connection timed out"), "network_timeout")
        self.assertEqual(ffmpeg_reason("Invalid data found when processing input"), "invalid_data")
        self.assertEqual(ffmpeg_reason("something odd " + signed), "unknown")
        self.assertEqual(ffmpeg_reason(""), "no_output")

    def test_error_details_keep_only_codes(self):
        self.assertEqual(error_details(MediaError("capture_failed", "http_403")),
                         dict(error_type="MediaError", error_code="capture_failed", media_reason="http_403"))
        self.assertEqual(error_details(MediaError("capture_failed", "https://leak")),
                         dict(error_type="MediaError", error_code="capture_failed"))
        self.assertEqual(error_details(MediaError("live_stalled"))["error_code"], "live_stalled")
        self.assertEqual(error_details(TimeoutError("live_segment_timeout"))["error_code"], "live_segment_timeout")


class CaptureFailureTests(unittest.TestCase):
    def setUp(self):
        state = Path(__file__).resolve().parents[1] / "state"
        state.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=state)
        self.root = Path(self.temp.name)
        self.target = MediaTarget("https://x.bilivideo.com/v", {}, "bvid", "session", False,
                                  audio_url="https://x.bilivideo.com/a")

    def tearDown(self):
        self.temp.cleanup()

    def test_failed_capture_reports_reason_and_removes_its_log(self):
        proc = Mock(returncode=1); proc.poll.return_value = 1
        def spawn(args, stderr=None, **kwargs):
            stderr.write(b"[https @ 0x1] Server returned 403 Forbidden (access denied)\n")
            return proc
        with patch("bili_companion.media.capture.subprocess.Popen", side_effect=spawn):
            with self.assertRaises(MediaError) as caught:
                MediaCollector().capture(self.target, self.root)
        self.assertEqual(str(caught.exception), "capture_failed")
        self.assertEqual(caught.exception.reason, "http_403")
        self.assertEqual(list(self.root.iterdir()), [])


class LiveStallTests(unittest.TestCase):
    def setUp(self):
        state = Path(__file__).resolve().parents[1] / "state"
        state.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=state)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def recorder(self, **kwargs):
        recorder = LiveRecorder(MediaTarget("https://x.live/v", {}, "room", "live", True), self.root, **kwargs)
        recorder.directory.mkdir()
        recorder._proc = Mock(); recorder._proc.poll.return_value = None
        return recorder

    def test_stalled_stream_is_reported_before_the_timeout(self):
        recorder = self.recorder(stall_seconds=.3)
        recorder._grew_at = time.monotonic()
        started = time.monotonic()
        with self.assertRaisesRegex(MediaError, "live_stalled"):
            recorder.next_clip(timeout=5)
        self.assertLess(time.monotonic() - started, 2)

    def test_no_data_yet_waits_for_the_normal_timeout(self):
        recorder = self.recorder(stall_seconds=.1)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            recorder.next_clip(timeout=.4)
        self.assertGreaterEqual(time.monotonic() - started, .35)

    def test_slow_but_growing_stream_gets_extra_time(self):
        recorder = self.recorder(stall_seconds=1)
        recorder._grew_at = time.monotonic()
        stop = threading.Event()
        def trickle():
            while not stop.wait(.05):
                recorder._grew_at = time.monotonic()
        def deliver():
            time.sleep(.7)
            path = recorder.directory / "part_000000.mp4"
            path.write_bytes(b"synthetic")
            with recorder._condition:
                recorder._queue.append((0, path))
                recorder._condition.notify_all()
        threads = [threading.Thread(target=trickle), threading.Thread(target=deliver)]
        for thread in threads: thread.start()
        try:
            with patch("bili_companion.media.capture.subprocess.run",
                       return_value=subprocess.CompletedProcess([], 0, json.dumps(PROBE).encode())):
                chunk = recorder.next_clip(timeout=.5)
        finally:
            stop.set()
            for thread in threads: thread.join()
        self.assertEqual(chunk.start, 0)

    def test_ended_stream_carries_ffmpeg_reason(self):
        recorder = self.recorder()
        recorder._log.write_bytes(b"Server returned 404 Not Found\n")
        recorder._proc.poll.return_value = 1
        recorder._proc.returncode = 1
        recorder._monitor()
        with self.assertRaises(MediaError) as caught:
            recorder.next_clip(timeout=1)
        self.assertEqual((str(caught.exception), caught.exception.reason), ("live_stream_ended", "http_404"))
        recorder.stop()
        self.assertFalse(recorder.directory.exists())


class VideoTargetRefreshTests(test_watcher.WatcherTests):
    """Reuses the watcher fixtures; only the tests below are new."""

    def setUp(self):
        test = self._testMethodName
        if not test.startswith('test_refresh_'):
            self.skipTest('inherited watcher test runs in test_watcher')

    def counting_resolve(self):
        calls = []
        original = self.platform.resolve_video
        def resolve(bvid):
            calls.append(bvid)
            return original(bvid)
        self.platform.resolve_video = resolve
        return calls

    def events(self, kind):
        with self.watcher.database.connect() as db:
            return [json.loads(d) for (d,) in db.execute(
                "SELECT details FROM watcher_events WHERE kind=?", (kind,))]

    async def test_refresh_failed_capture_retries_with_fresh_url(self):
        await self.watcher.step()
        calls = self.counting_resolve()
        attempts = []
        class ExpiringCollector(FakeCollector):
            def capture(inner, *args, **kwargs):
                attempts.append(1)
                if len(attempts) == 1:
                    raise MediaError("capture_failed", "http_403")
                return FakeCollector.capture(inner, *args, **kwargs)
        self.watcher.collector = ExpiringCollector()
        self.assertEqual(await self.watcher.step(), 'observed')
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(calls), 2)  # first resolve of the target, then the refresh
        self.assertEqual(self.events('video_target_refreshed')[0]['reason'], 'http_403')
        self.assertEqual(self.watcher.state.snapshot()['video'].get('failures', 0), 0)

    async def test_refresh_aged_url_before_capture_keeps_comment_state(self):
        await self.watcher.step()
        self.assertEqual(await self.watcher.step(), 'observed')
        calls = self.counting_resolve()
        self.watcher.state.change(lambda s: s['video'].update(comment_coverage=[[0, 30]], comment_chunks=1))
        self.now += 900
        self.watcher.state.change(lambda s: s.update(next_observation_at=0))
        await self.watcher.step()
        self.assertEqual(calls, ['BVsynthetic1'])
        self.assertEqual(self.events('video_target_refreshed')[0]['reason'], 'aged')
        self.assertIn([0, 30], self.watcher.state.snapshot()['video']['comment_coverage'])

    async def test_refresh_second_failure_still_counts(self):
        await self.watcher.step()
        class BrokenCollector(FakeCollector):
            def capture(inner, *args, **kwargs):
                raise MediaError("capture_failed", "http_404")
        self.watcher.collector = BrokenCollector()
        self.assertEqual(await self.watcher.step(), 'error')
        self.assertEqual(self.watcher.state.snapshot()['video']['failures'], 1)
        logged = self.events('watch_error')[-1]
        self.assertEqual((logged['error_code'], logged['media_reason']), ('capture_failed', 'http_404'))


if __name__ == "__main__":
    unittest.main()
