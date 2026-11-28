import asyncio
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from bili_companion.media.capture import MediaChunk
from bili_companion.media.scene_gate import StaticSceneGate
from bili_companion.runtime.watcher import Watcher
import test_watcher as tw

HAVE_FFMPEG = bool(shutil.which('ffmpeg'))
ENCODE = ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '30', '-c:a', 'aac', '-ac', '1',
          '-ar', '16000', '-b:a', '32k', '-t', '30', '-y']
VIDEO = {
    'still': ('color=c=0x336699:s=426x240:r=2', 'noise=alls=6:allf=t'),
    'other_still': ('color=c=0x993333:s=426x240:r=2', None),
    'moving': ('testsrc=s=426x240:r=2', None),
}
AUDIO = {
    'silence': 'anullsrc=r=16000:cl=mono',
    'tone': 'sine=f=300:r=16000,volume=0.25',
}


def make_clip(path, video, audio):
    source, vf = VIDEO[video]
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', source, '-f', 'lavfi', '-i', AUDIO[audio]]
    if vf:
        command += ['-vf', vf]
    subprocess.run(command + ENCODE + [str(path)], check=True, timeout=60)


def chunk_for(path, session='live-session'):
    return MediaChunk(Path(path), 0, 30, 1_800_000_000, 'mixed', '8729095131', session)


@unittest.skipUnless(HAVE_FFMPEG, 'ffmpeg required for synthetic clips')
class SceneGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.clips = {}
        for name, (video, audio) in dict(still_silent=('still', 'silence'), still_silent_b=('still', 'silence'),
                still_tone=('still', 'tone'), moving_silent=('moving', 'silence'),
                other_silent=('other_still', 'silence')).items():
            cls.clips[name] = Path(cls.temp.name) / (name + '.mp4')
            make_clip(cls.clips[name], video, audio)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def decide(self, gate, name, now, live=True, key=('live_room', 'r', 's')):
        decision, sig = gate.check(chunk_for(self.clips[name]), key, live, now)
        return decision, sig

    def baseline(self, gate, now=1000.):
        decision, sig = self.decide(gate, 'still_silent', now)
        self.assertEqual(decision.reason, 'no_baseline')
        gate.commit(('live_room', 'r', 's'), sig, now)

    def test_signature_is_cheap(self):
        import time
        gate = StaticSceneGate()
        gate.signature(self.clips['still_silent'], 30)
        started = time.monotonic()
        sig = gate.signature(self.clips['moving_silent'], 30)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(sig.frames), 4)

    def test_static_color_and_silence_is_skipped(self):
        gate = StaticSceneGate()
        self.baseline(gate)
        decision, _ = self.decide(gate, 'still_silent_b', 1100.)
        self.assertTrue(decision.skip)
        self.assertEqual(decision.reason, 'static_silent')

    def test_static_with_audible_audio_is_observed(self):
        gate = StaticSceneGate()
        self.baseline(gate)
        decision, _ = self.decide(gate, 'still_tone', 1100.)
        self.assertFalse(decision.skip)
        self.assertEqual(decision.reason, 'audible')

    def test_moving_video_is_observed(self):
        gate = StaticSceneGate()
        self.baseline(gate)
        decision, _ = self.decide(gate, 'moving_silent', 1100.)
        self.assertFalse(decision.skip)
        self.assertEqual(decision.reason, 'motion_in_chunk')

    def test_different_still_scene_is_observed(self):
        gate = StaticSceneGate()
        self.baseline(gate)
        decision, _ = self.decide(gate, 'other_silent', 1100.)
        self.assertFalse(decision.skip)
        self.assertEqual(decision.reason, 'scene_changed')

    def test_cap_forces_observation_live_and_video(self):
        gate = StaticSceneGate()
        self.baseline(gate)
        self.assertTrue(self.decide(gate, 'still_silent_b', 1000. + 239)[0].skip)
        self.assertEqual(self.decide(gate, 'still_silent_b', 1000. + 241)[0].reason, 'cap_reached')
        # Video allows a longer quiet stretch than live.
        self.assertTrue(self.decide(gate, 'still_silent_b', 1000. + 300, live=False)[0].skip)
        self.assertEqual(self.decide(gate, 'still_silent_b', 1000. + 361, live=False)[0].reason, 'cap_reached')

    def test_new_session_has_no_baseline(self):
        gate = StaticSceneGate()
        self.baseline(gate)
        decision, _ = self.decide(gate, 'still_silent_b', 1100., key=('live_room', 'r', 'new-session'))
        self.assertEqual(decision.reason, 'no_baseline')

    def test_gate_errors_fail_open(self):
        gate = StaticSceneGate()
        self.baseline(gate)
        broken = Path(self.temp.name) / 'broken.mp4'
        broken.write_bytes(b'not media')
        decision, sig = gate.check(chunk_for(broken), ('live_room', 'r', 's'), True, 1100.)
        self.assertFalse(decision.skip)
        self.assertEqual(decision.reason, 'gate_error')
        self.assertIsNone(sig)
        missing_binary = StaticSceneGate(ffmpeg='definitely-not-ffmpeg')
        self.assertFalse(missing_binary.check(chunk_for(self.clips['still_silent']), ('k',), True, 1.)[0].skip)


class ClipRecorder:
    def __init__(self, target, output_dir, source):
        self.target, self.output_dir, self.source, self.skipped = target, output_dir, source, []

    def start(self):
        return self

    def next_clip(self, timeout=45):
        path = Path(self.output_dir) / ('clip-' + uuid.uuid4().hex + '.mp4')
        shutil.copy(self.source(), path)
        return MediaChunk(path, 0, 30, 1_799_999_999, 'mixed', self.target.identity, self.target.session_id)

    def stop(self):
        pass


class ClipCollector:
    def __init__(self, source):
        self.source = source

    def capture(self, target, output_dir, duration=30, offset=0, cancel_event=None):
        path = Path(output_dir) / ('clip-' + uuid.uuid4().hex + '.mp4')
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.source(), path)
        return MediaChunk(path, offset, offset + duration, 1_799_999_999, 'mixed', target.identity, target.session_id)


@unittest.skipUnless(HAVE_FFMPEG, 'ffmpeg required for synthetic clips')
class WatcherGateTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.clips = {}
        for name, (video, audio) in dict(still_silent=('still', 'silence'), still_tone=('still', 'tone'),
                moving_silent=('moving', 'silence')).items():
            cls.clips[name] = Path(cls.temp.name) / (name + '.mp4')
            make_clip(cls.clips[name], video, audio)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    async def asyncSetUp(self):
        self.work = tempfile.TemporaryDirectory()
        root = Path(self.work.name)
        self.clip = 'still_silent'
        self.now = 1_800_000_000
        self.platform = tw.FakePlatform()
        self.engine = tw.FakeEngine()
        self.calls = 0
        original = self.engine.observe

        def observe(*args, **kwargs):
            self.calls += 1
            return original(*args, **kwargs)
        self.engine.observe = observe
        config = dict(account_id='hakimi-bilibili', priority_room='8729095131', state_dir=str(root / 'watch'),
                      native_database=str(root / 'native.sqlite3'), send_enabled=True)
        source = lambda: self.clips[self.clip]
        self.watcher = Watcher(config, self.platform, ClipCollector(source), self.engine,
                               recorder_factory=lambda target, out, **kw: ClipRecorder(target, out, source),
                               clock=lambda: self.now)

    async def asyncTearDown(self):
        await self.watcher.close_recorder()
        self.work.cleanup()

    def go_live(self):
        self.watcher.apply_status(dict(is_live=True, live_session_id='live-session', checked_at=1_800_000_000))

    def events(self, kind):
        with self.watcher.database.connect() as db:
            return db.execute('SELECT details FROM watcher_events WHERE kind=?', (kind,)).fetchall()

    async def test_static_silent_live_chunks_skip_until_cap_then_observe(self):
        self.go_live()
        self.assertEqual(await self.watcher.step(), 'observed')
        due = self.watcher.state.snapshot()['next_observation_at']
        self.now += 100
        self.assertEqual(await self.watcher.step(), 'static_skip')
        self.now += 100
        self.assertEqual(await self.watcher.step(), 'static_skip')
        self.assertEqual(self.calls, 1)
        state = self.watcher.state.snapshot()
        self.assertEqual(state['static_skips'], 2)
        self.assertEqual(state['next_observation_at'], due)
        self.assertEqual(state['understanding_state'], 'SAMPLED')
        self.assertEqual(len(self.events('static_skip')), 2)
        self.assertEqual(len(self.events('observed')), 1)
        self.now += 100  # 300 s since the last real observation > 240 s cap
        self.assertEqual(await self.watcher.step(), 'observed')
        self.assertEqual(self.calls, 2)
        self.assertEqual(self.watcher.scene_gate.skips_in_row, 0)

    async def test_audible_static_live_chunk_is_observed(self):
        self.go_live()
        await self.watcher.step()
        self.clip = 'still_tone'
        self.now += 100
        self.assertEqual(await self.watcher.step(), 'observed')
        self.assertEqual(self.calls, 2)

    async def test_moving_live_chunk_is_observed(self):
        self.go_live()
        await self.watcher.step()
        self.clip = 'moving_silent'
        self.now += 100
        self.assertEqual(await self.watcher.step(), 'observed')
        self.assertEqual(self.calls, 2)

    async def test_gate_failure_observes_normally(self):
        self.go_live()
        await self.watcher.step()
        self.now += 100
        with patch.object(self.watcher.scene_gate, 'signature', side_effect=RuntimeError('boom')):
            self.assertEqual(await self.watcher.step(), 'observed')
        self.assertEqual(self.calls, 2)

    async def test_gate_can_be_disabled(self):
        self.watcher.scene_gate = None
        self.go_live()
        await self.watcher.step()
        self.now += 100
        self.assertEqual(await self.watcher.step(), 'observed')

    async def test_video_skip_advances_position_and_cap_observes(self):
        self.assertEqual(await self.watcher.step(), 'selecting')
        self.assertEqual(await self.watcher.step(), 'observed')
        self.assertEqual(self.watcher.state.snapshot()['video']['position'], 30)
        self.now += 200
        self.assertEqual(await self.watcher.step(), 'static_skip')
        video = self.watcher.state.snapshot()['video']
        self.assertEqual(video['position'], 60)
        self.assertEqual(video['observations'], 1)
        self.assertEqual(self.calls, 1)
        self.now += 200  # 400 s since last observation > 360 s video cap
        self.assertEqual(await self.watcher.step(), 'observed')
        self.assertEqual(self.calls, 2)


if __name__ == '__main__':
    unittest.main()
