import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from bili_companion.media.capture import LiveRecorder, MediaCollector, MediaError
from bili_companion.platform.bilibili import MediaTarget

PROBE=dict(streams=[dict(codec_type="video",duration="29.8"),dict(codec_type="audio",duration="30")],format=dict(duration="30"))


class MediaTests(unittest.TestCase):
    def setUp(self):
        state=Path(__file__).resolve().parents[1]/"state"
        state.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=state)
        self.root=Path(self.temp.name)
        self.target=MediaTarget("https://x.bilivideo.com/v",{},"bvid","session",False,audio_url="https://x.bilivideo.com/a")

    def tearDown(self):
        self.temp.cleanup()

    def test_capture_audio_video_and_actual_coverage(self):
        proc=Mock(returncode=0);proc.poll.return_value=0
        with patch("bili_companion.media.capture.subprocess.Popen",return_value=proc) as spawn,patch("bili_companion.media.capture.subprocess.run",return_value=subprocess.CompletedProcess([],0,json.dumps(PROBE).encode())):
            chunk=MediaCollector(clock=lambda:100).capture(self.target,self.root,offset=15)
        self.assertAlmostEqual(chunk.end,44.8)
        args=spawn.call_args.args[0]
        self.assertEqual(args.count("-i"),2)
        self.assertIn("1:a:0",args)
        self.assertEqual(chunk.modality,"mixed")

    def test_missing_audio_never_claims_full_watch(self):
        proc=Mock(returncode=0);proc.poll.return_value=0
        probe=dict(streams=[dict(codec_type="video")],format=dict(duration="30"))
        with patch("bili_companion.media.capture.subprocess.Popen",return_value=proc),patch("bili_companion.media.capture.subprocess.run",return_value=subprocess.CompletedProcess([],0,json.dumps(probe).encode())):
            with self.assertRaises(MediaError):
                MediaCollector().capture(self.target,self.root)

    def test_cancel_terminates_and_cleans(self):
        proc=Mock();proc.poll.return_value=None
        event=threading.Event();event.set()
        with patch("bili_companion.media.capture.subprocess.Popen",return_value=proc):
            with self.assertRaisesRegex(MediaError,"cancelled"):
                MediaCollector().capture(self.target,self.root,cancel_event=event)
        proc.terminate.assert_called_once()

    def test_live_bounded_queue_records_skipped(self):
        recorder=LiveRecorder(MediaTarget(self.target.url,{},"room","live",True),self.root,max_queue=2)
        recorder.directory.mkdir()
        for index in range(5):
            (recorder.directory/f"part_{index:06d}.mp4").write_bytes(b"synthetic")
        recorder._proc=Mock();recorder._proc.poll.return_value=0
        recorder.started_at=100
        recorder._monitor()
        self.assertEqual(len(recorder._queue),2)
        self.assertEqual(len(recorder.skipped),3)
        with patch("bili_companion.media.capture.subprocess.run",return_value=subprocess.CompletedProcess([],0,json.dumps(PROBE).encode())):
            chunk=recorder.next_clip()
        self.assertEqual(chunk.start,120)
        self.assertEqual(len(recorder._closed_at),0)
        self.assertTrue(any(g['reason']=='understanding_pacing' for g in recorder.drain_skipped()))
        self.assertEqual(recorder.drain_skipped(),[])
        recorder.stop()
        self.assertFalse(list(recorder.directory.glob("*.mp4")))


if __name__ == "__main__":
    unittest.main()
