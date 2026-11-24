from decimal import Decimal
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from bili_companion.cognition.gateway import GatewayClient, NATIVE_VIDEO_SOURCES
from bili_companion.cognition.live import BudgetPaused, CognitionEngine, OBSERVER
from bili_companion.cognition.routing import HydrogenRouteEngine, NATIVE_VIDEO_ROUTE, PRIMARY
from bili_companion.cognition.video_duty import VideoDuty
from bili_companion.domain import Scope
from bili_companion.memory import MemoryStore
from bili_companion.policy.budget import BudgetLedger
from bili_companion.runtime.storage import Database
from test_cognition_live import Client, response


class NativeVideoRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.database = Database(root / 'watcher.sqlite3')
        self.ledger = BudgetLedger(self.database, monthly='2', daily='1')
        self.memory = MemoryStore(self.database)
        self.clock = lambda: 1795196003.25
        self.native = GatewayClient(root / 'key', root / 'ca')
        self.native._request = Mock()
        self.transcript = Client([])
        self.legacy = Client([])
        self.route = self.build_route(self.ledger)
        path = root / 'synthetic.mp4'
        path.write_bytes(b'fixture MP4 placeholder')
        self.chunk = SimpleNamespace(path=path, start=0., end=8., received_at=self.clock(),
            modality='mixed', target_id='video', session_id='test', has_audio=True, has_video=True)
        self.scope = Scope('test', 'video', 'video')

    def build_route(self, ledger):
        options = dict(clock=self.clock, max_output_tokens=1024, single_pass=True,
            price_resolver=lambda model, now: (Decimal('1'), Decimal('2')))
        native = CognitionEngine(self.native, ledger, self.memory, observer_model=OBSERVER,
            call_provider='vertex', **options)
        transcript = CognitionEngine(self.transcript, ledger, self.memory, **options)
        return HydrogenRouteEngine(self.legacy, ledger, self.memory, observer_model=PRIMARY,
            video_routes={NATIVE_VIDEO_ROUTE: (native, VideoDuty(self.database, NATIVE_VIDEO_ROUTE, OBSERVER)),
                'video-observe': (transcript, VideoDuty(self.database, 'video-observe', 'transcript.json'))},
            **options)

    @staticmethod
    def observed():
        return {
            'events': [
                dict(text='实际听到短蜂鸣', start=1, end=2, modality='audio', confidence=.95),
                dict(text='蓝色背景', start=0, end=4, modality='video', confidence=.95),
            ],
            'missing_modalities': [], 'interesting': True,
            'video_feedback': dict(interest='neutral', next_action='continue', summary='蓝色背景出现蜂鸣',
                evidence_indices=[0, 1], comment=dict(speak=False, text='', evidence_indices=[])),
        }

    def test_native_sends_one_mp4_and_records_actual_source_and_times(self):
        self.native._request.side_effect = [{'totalTokens': 1000}, response(self.observed())]
        result = self.route.observe(self.chunk, self.scope, 0, allow_decision=False)
        self.assertEqual(len(result.observation.evidence), 2)
        sound, visual = result.observation.evidence
        self.assertEqual(sound.version, f'{OBSERVER}:{NATIVE_VIDEO_SOURCES["audio"]}:v1')
        self.assertEqual(visual.version, f'{OBSERVER}:{NATIVE_VIDEO_SOURCES["video"]}:v1')
        self.assertEqual((sound.media_start, sound.media_end), (1, 2))
        calls = self.native._request.call_args_list
        self.assertEqual([call.args[:2] for call in calls],
            [(OBSERVER, 'countTokens'), (OBSERVER, 'generateContent')])
        media, data = calls[1].args[2]['contents'][0]['parts']
        self.assertEqual(media['inlineData']['mimeType'], 'video/mp4')
        self.assertEqual(media['videoMetadata']['fps'], 1.)
        self.assertEqual(json.loads(data['text'])['duration'], 8.)
        self.assertEqual(self.transcript.requests, [])
        self.assertEqual(self.ledger.entries()[0]['state'], 'settled')

    def test_verified_hydrogen_mp4_precedes_vertex_and_falls_back_with_sound(self):
        from bili_companion.cognition.gateway import HYDROGEN

        relay, _ = self.route.video_routes['video-observe']
        relay.observer_model = HYDROGEN
        self.transcript.media_mode = 'mp4_image_url'
        good = dict(response(self.observed()), _media_sources=NATIVE_VIDEO_SOURCES,
                    _provider_response_metadata={'reported_model': HYDROGEN})
        self.transcript.replies.extend([good, dict(good, _provider_response_metadata={})])
        self.native._request.side_effect = [{'totalTokens': 1000}, response(self.observed())]
        primary = self.route.observe(self.chunk, self.scope, 0, allow_decision=False)
        self.assertIn(HYDROGEN + ':native-video-audio:', primary.observation.evidence[0].version)
        self.native._request.assert_not_called()
        fallback = self.route.observe(self.chunk, self.scope, 0, allow_decision=False)
        self.assertIn(OBSERVER + ':native-video-audio:', fallback.observation.evidence[0].version)
        self.assertEqual(len(self.transcript.requests), 2)
        self.assertEqual(len(self.route.role_events_for(fallback.request_ids)), 2)

    def test_qq_continuous_view_keeps_two_distinct_sound_and_picture_pairs(self):
        observed = self.observed()
        observed['events'] += [dict(event, start=4, end=6) for event in observed['events']]
        observed['video_feedback']['evidence_indices'] = [0, 1, 2, 3]
        self.native._request.side_effect = [{'totalTokens': 1000}, response(observed)]
        result = self.route.observe(self.chunk, self.scope, 0, allow_decision=False,
                                    video_context={'continuous_viewing': True})
        self.assertEqual([item.media_start for item in result.observation.evidence], [event['start'] for event in observed['events']])

    def test_wrong_native_model_preserves_accounting_and_uses_transcript(self):
        wrong_model = dict(response(self.observed()), modelVersion='different-model')
        self.native._request.side_effect = [{'totalTokens': 1000}, wrong_model]
        reply = response(self.observed())
        reply['_provider_response_metadata'] = {'reported_model': OBSERVER}
        reply['_media_sources'] = {'audio': 'local-wenet-transcript', 'video': 'local-timestamped-frames'}
        self.transcript.replies.append(reply)
        result = self.route.observe(self.chunk, self.scope, 0, allow_decision=False)
        sound = result.observation.evidence[0]
        self.assertIn(':local-wenet-transcript:', sound.version)
        self.assertEqual((sound.media_start, sound.media_end), (0, 8))
        self.assertEqual(len(self.route.role_events_for(result.request_ids)), 2)
        self.assertEqual(self.legacy.requests, [])
        self.assertEqual([row['state'] for row in self.ledger.entries()], ['settled', 'settled'])

    def test_native_budget_denial_does_not_try_another_provider(self):
        ledger = BudgetLedger(self.database, monthly='.001', daily='.001')
        self.native._request.return_value = {'totalTokens': 1000}
        with self.assertRaises(BudgetPaused):
            self.build_route(ledger).observe(self.chunk, self.scope, 0, allow_decision=False)
        self.assertEqual(self.native._request.call_count, 1)
        self.assertEqual(self.transcript.requests, [])

    def test_video_without_audio_keeps_visual_evidence_and_marks_sound_missing(self):
        self.chunk.has_audio = False
        value = self.observed()
        value['events'] = value['events'][1:]
        value['video_feedback'].update(summary='蓝色背景', evidence_indices=[0])
        self.native._request.side_effect = [{'totalTokens': 1000}, response(value)]
        result = self.route.observe(self.chunk, self.scope, 0, allow_decision=False)
        self.assertEqual(result.observation.missing_modalities, ('audio',))
        self.assertEqual([e.modality for e in result.observation.evidence], ['video'])


if __name__ == '__main__':
    unittest.main()
