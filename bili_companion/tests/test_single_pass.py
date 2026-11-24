"""One media generation must contain perception and grounded understanding."""
from decimal import Decimal
import json
import unittest

from bili_companion.cognition.live import CognitionEngine
from bili_companion.domain import Scope
import test_cognition_live as helpers
from test_cognition_live import observed, response
from test_cognition_video import video_reply


class SinglePassTests(unittest.TestCase):
    setUp = helpers.CognitionTests.setUp
    engine = helpers.CognitionTests.engine

    def unified(self, replies, **kwargs):
        model = 'gemini-3.8-flash-high'
        return self.engine(replies, single_pass=True, observer_model=model, decider_model=model,
                           price_resolver=lambda *args: (Decimal('.075'), Decimal('4')), **kwargs)

    def live_response(self, **extra):
        return response(dict(observed(), live_feedback=dict(speak=True, text='这段钢琴好听', evidence_indices=[0]), **extra))

    def test_live_observes_understands_and_expresses_in_one_media_request(self):
        engine, client = self.unified([self.live_response()])
        result = engine.observe(self.chunk, self.scope, 3)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.requests[0][0], 'gemini-3.8-flash-high')
        payload = client.requests[0][1]
        self.assertIn('inlineData', payload['contents'][0]['parts'][0])
        self.assertEqual(payload['generationConfig']['thinkingConfig']['thinkingLevel'], 'LOW')
        self.assertIn('live_feedback', payload['systemInstruction']['parts'][0]['text'])
        self.assertEqual(result.candidate.text, '这段钢琴好听')
        self.assertEqual(result.candidate.watch_epoch, 3)
        self.assertEqual(result.live_decision_status, 'ready')
        self.assertEqual(len(engine.ledger.entries()), 1)

    def test_spurious_child_request_is_inert_and_facts_survive_bad_feedback(self):
        request = dict(role='memory', reason='request second call', evidence_indices=[0])
        engine, client = self.unified([self.live_response(child_request=request)])
        result = engine.observe(self.chunk, self.scope, 3)
        self.assertEqual(result.child_status, 'disabled_single_pass')
        self.assertEqual(len(client.requests), 1)
        self.assertTrue(result.observation.evidence)
        self.assertFalse(hasattr(engine, 'dispatcher'))
        bad = dict(observed(), live_feedback=dict(speak=True, text='unbound', evidence_indices=[99]))
        engine, client = self.unified([response(bad)])
        result = engine.observe(self.chunk, self.scope, 3)
        self.assertTrue(result.observation.evidence)
        self.assertIsNone(result.candidate)
        self.assertEqual(result.live_decision_status, 'cooldown')
        self.assertEqual(len(client.requests), 1)

    def test_expression_disabled_and_cooldown_never_trigger_another_model(self):
        engine, client = self.unified([self.live_response(), self.live_response(), self.live_response()])
        first = engine.observe(self.chunk, self.scope, 3)
        second = engine.observe(self.chunk, self.scope, 3)
        disabled = engine.observe(self.chunk, self.scope, 3, allow_decision=False)
        self.assertIsNotNone(first.candidate)
        self.assertIsNone(second.candidate)
        self.assertIsNone(disabled.candidate)
        self.assertEqual(second.live_decision_status, 'cooldown')
        self.assertEqual(disabled.live_decision_status, 'disabled')
        self.assertEqual(len(client.requests), 3)
        data = json.loads(client.requests[1][1]['contents'][0]['parts'][1]['text'])
        self.assertFalse(data['expression_enabled'])

    def test_video_reuses_same_generation_for_preference_summary_and_comment(self):
        self.chunk.target_id = 'BV1234567890'
        scope = Scope('a', 'video', self.chunk.target_id)
        value = video_reply()
        content = json.loads(value['candidates'][0]['content']['parts'][0]['text'])
        content['child_request'] = dict(role='expression', reason='another expression', evidence_indices=[0])
        engine, client = self.unified([response(content)])
        result = engine.observe(self.chunk, scope, 4)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(result.interest, 'high')
        self.assertEqual(result.summary_evidence_ids, (result.observation.evidence[0].id,))
        self.assertEqual(result.comment_status, 'ready')
        self.assertEqual(result.child_status, 'disabled_single_pass')

    def test_recent_public_chat_can_be_replied_to_in_same_request(self):
        content = dict(observed(), live_feedback=dict(speak=True, text='这个节奏确实舒服',
                       evidence_indices=[], chat_indices=[0]))
        engine, client = self.unified([response(content)])
        result = engine.observe(self.chunk, self.scope, 3,
            live_context=[dict(text='节奏好听', observed_at=self.now-5)])
        self.assertEqual(len(client.requests), 1)
        self.assertIsNotNone(result.candidate)
        evidence = result.observation.evidence[-1]
        self.assertEqual(evidence.modality, 'danmaku')
        self.assertEqual(evidence.observed_at, self.now-5)
        self.assertEqual(result.candidate.evidence_ids, (evidence.id,))
        self.assertEqual(len(result.observation.coverage), 1)

    def test_fabricated_chat_reference_cannot_authorize_reply(self):
        content = dict(observed(), live_feedback=dict(speak=True, text='我也喜欢',
                       evidence_indices=[], chat_indices=[99]))
        engine, _ = self.unified([response(content)])
        result = engine.observe(self.chunk, self.scope, 3,
            live_context=[dict(text='hello', observed_at=self.now-5)])
        self.assertIsNone(result.candidate)

    def test_absent_public_history_allows_only_empty_optional_chat_references(self):
        for indices,status in (([],'ready'),([0],'invalid'),([99],'invalid')):
            with self.subTest(indices=indices):
                content=dict(observed(),live_feedback=dict(speak=True,text='这段钢琴好听',
                    evidence_indices=[0],chat_indices=indices))
                engine, _ = self.unified([response(content)])
                result=engine.observe(self.chunk,self.scope,3)
                self.assertEqual(result.live_decision_status,status)
                with self.db.connect(write=True) as db:db.execute('DELETE FROM cognition_decisions')

    def test_single_pass_cannot_enable_child_calls(self):
        with self.assertRaises(ValueError):
            self.unified([], child_roles_enabled=True)

    def test_failed_generation_does_not_consume_expression_interval(self):
        engine, client = self.unified([TimeoutError(), self.live_response()])
        with self.assertRaises(helpers.CognitionUnavailable):
            engine.observe(self.chunk, self.scope, 3)
        result = engine.observe(self.chunk, self.scope, 3)
        self.assertEqual(result.live_decision_status, 'ready')
        self.assertEqual(len(client.requests), 2)

    def test_invalid_expired_and_low_confidence_decisions_allow_next_observation(self):
        cases = [
            ('invalid', dict(observed(), live_feedback=dict(speak=True, text='x', evidence_indices=[99]))),
            ('expired', dict(observed(), live_feedback=dict(speak=True, text='x', evidence_indices=[0]))),
            ('low_confidence', dict(observed(), live_feedback=dict(speak=True, text='x', evidence_indices=[0]))),
        ]
        for status, content in cases:
            with self.subTest(status=status):
                if status == 'low_confidence': content['events'][0]['confidence'] = .79
                engine, client = self.unified([response(content), self.live_response()])
                self.chunk.received_at = self.now-90 if status == 'expired' else self.now
                first = engine.observe(self.chunk, self.scope, 3)
                self.assertEqual(first.live_decision_status, status)
                self.assertIsNone(first.candidate)
                self.chunk.received_at = self.now
                second = engine.observe(self.chunk, self.scope, 3)
                self.assertEqual(second.live_decision_status, 'ready')
                # Isolate persistent decision throttles between subtests.
                with self.db.connect(write=True) as db: db.execute('DELETE FROM cognition_decisions')

    def test_valid_model_silence_consumes_expression_interval(self):
        silent = dict(observed(), live_feedback=dict(speak=False, text='', evidence_indices=[]))
        engine, _ = self.unified([response(silent), self.live_response()])
        self.assertEqual(engine.observe(self.chunk, self.scope, 3).live_decision_status, 'model_silent')
        self.assertEqual(engine.observe(self.chunk, self.scope, 3).live_decision_status, 'cooldown')

    def test_expression_interval_is_scoped_to_live_session(self):
        engine, _ = self.unified([self.live_response(), self.live_response()])
        self.assertEqual(engine.observe(self.chunk, self.scope, 3).live_decision_status, 'ready')
        self.chunk.session_id='next-session'
        self.assertEqual(engine.observe(self.chunk, self.scope, 4).live_decision_status, 'ready')

    def test_retired_result_does_not_consume_expression_interval(self):
        current=[False]
        engine, _ = self.unified([self.live_response(), self.live_response()],
            epoch_is_current=lambda *args:current[0])
        retired=engine.observe(self.chunk, self.scope, 3)
        self.assertIsNone(retired.candidate)
        self.assertEqual(retired.live_decision_status, 'stale_epoch')
        current[0]=True
        self.assertEqual(engine.observe(self.chunk, self.scope, 4).live_decision_status, 'ready')


if __name__ == '__main__':
    unittest.main()
