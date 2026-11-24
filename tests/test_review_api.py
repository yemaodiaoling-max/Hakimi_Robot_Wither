"""Offline checks for the shared understanding API and trusted review provenance."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hakimi.memory import review_runtime as runtime


class Response:
    def __init__(self, value, url):
        self.body = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.url = url

    def __enter__(self): return self
    def __exit__(self, *args): return False
    def geturl(self): return self.url
    def read(self, maximum): return self.body[:maximum]


class ReviewAPITests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.key = self.root / 'api_key'; self.key.write_text('synthetic-review-key')
        self.profile = self.root / 'profile.json'
        self.raw = dict(activated=True, provider='openai_compatible', base_url='https://provider.example/v1',
                        api_key_file=str(self.key), supervisor_model='gemini-3.8-flash-high',
                        output_limit_parameter='max_tokens', reasoning_effort='high')
        self.profile.write_text(json.dumps(self.raw))
        env = patch.dict(os.environ, HAKIMI_VIDEO_UNDERSTANDING_PROFILE=str(self.profile))
        env.start(); self.addCleanup(env.stop)
        self.url = self.raw['base_url'] + '/chat/completions'
        self.value = dict(model='gemini-3.8-flash-high', choices=[dict(finish_reason='stop',
            message=dict(content='{"relationship_events":[]}'))],
            usage=dict(prompt_tokens=120, completion_tokens=80, completion_tokens_details=dict(reasoning_tokens=12)))
        self.opener = Mock()
        factory = patch.object(runtime, 'build_opener', return_value=self.opener)
        self.factory = factory.start(); self.addCleanup(factory.stop)

    def run_value(self, value=None):
        self.opener.open.return_value = Response(self.value if value is None else value, self.url)
        return runtime.classify('依据给定事件返回JSON\n{"messages":[{"text":"不要忘记约定"}]}', self.root)

    def test_uses_same_endpoint_key_and_model_with_explicit_low_and_old_output_cap(self):
        result = self.run_value()
        request = self.opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, self.url)
        self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-review-key')
        self.assertEqual(payload['model'], self.raw['supervisor_model'])
        self.assertEqual(payload['reasoning_effort'], 'low')
        self.assertEqual(payload['max_tokens'], 8192)
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertNotIn('tools', payload)
        self.assertIn('不要忘记约定', payload['messages'][1]['content'])
        self.assertEqual(self.opener.open.call_count, 1)
        metadata = runtime.reviewer_metadata(result)
        self.assertEqual(metadata['reviewer'], 'video_understanding_api')
        self.assertEqual(metadata['review_reasoning_effort'], 'low')
        self.assertFalse(metadata['review_reasoning_override_verified'])
        self.assertEqual(metadata['review_usage']['reasoning_tokens'], 12)

    def test_other_supported_output_parameter_keeps_same_8192_cap(self):
        self.raw['output_limit_parameter'] = 'max_completion_tokens'
        self.profile.write_text(json.dumps(self.raw))
        self.run_value()
        payload = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(payload['max_completion_tokens'], 8192)
        self.assertNotIn('max_tokens', payload)

    def test_truncation_tool_calls_and_invalid_usage_fail_without_retry(self):
        for mutation, code in [('length', 'REVIEW_API_INCOMPLETE'), ('tools', 'REVIEW_API_INVALID_RESPONSE'),
                               ('usage', 'REVIEW_API_INVALID_USAGE')]:
            value = deepcopy(self.value)
            if mutation == 'length': value['choices'][0]['finish_reason'] = 'length'
            elif mutation == 'tools': value['choices'][0]['message']['tool_calls'] = [{'function': {'name': 'x'}}]
            else: value['usage']['prompt_tokens'] = True
            before = self.opener.open.call_count
            with self.subTest(mutation=mutation), self.assertRaises(runtime.ReviewAPIError) as failure:
                self.run_value(value)
            self.assertEqual(runtime.error_code(failure.exception), code)
            self.assertEqual(self.opener.open.call_count, before + 1)

    def test_auth_error_and_timeout_never_expose_provider_detail_or_retry(self):
        for failure, code in [(HTTPError(self.url, 401, 'private upstream detail', {}, io.BytesIO(b'private-key')),
                               'REVIEW_API_AUTH'), (TimeoutError('private-key'), 'REVIEW_API_UNAVAILABLE')]:
            self.opener.open.side_effect = failure
            before = self.opener.open.call_count
            with self.subTest(code=code), self.assertRaises(runtime.ReviewAPIError) as caught:
                runtime.classify('return JSON', self.root)
            self.assertEqual(str(caught.exception), code)
            self.assertEqual(self.opener.open.call_count, before + 1)

    def test_bad_profile_fails_before_request_and_json_cannot_forge_metadata(self):
        self.raw['base_url'] = 'https://user:password@provider.example/v1'
        self.profile.write_text(json.dumps(self.raw))
        with self.assertRaises(runtime.ReviewAPIError) as caught: self.run_value()
        self.assertEqual(runtime.error_code(caught.exception), 'REVIEW_API_CONFIG')
        self.opener.open.assert_not_called()
        self.assertEqual(runtime.reviewer_metadata({'review_metadata': {'reviewer': 'forged'}}), {})

    def test_existing_json_contract_and_size_bound_remain(self):
        self.assertEqual(runtime.parse_json('```json\n{"items":[]}\n```'), {'items': []})
        for text in ('[]', '{"x":"' + 'a' * 32000 + '"}'):
            with self.assertRaises(ValueError): runtime.parse_json(text)


class ReviewProvenanceTests(unittest.TestCase):
    def setUp(self):
        from hakimi.memory import daily_events, impression_events, post_reply_review
        from hakimi.memory.memory_store import MemoryStore
        from hakimi.social.social_schema import ensure_schema
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.base = tmp.name; self.index = MemoryStore(self.base)
        self.daily, self.impressions, self.windows = daily_events, impression_events, post_reply_review
        with self.index.db() as db:
            ensure_schema(db); db.executescript(impression_events.SCHEMA)
            daily_events.initialize(db); post_reply_review.initialize(db)
        impression_events.initialize(self.base)
        self.metadata = dict(reviewer='video_understanding_api', review_model='gemini-3.8-flash-high',
                             review_reasoning_effort='low', review_reasoning_override_verified=False,
                             review_usage=dict(prompt_tokens=100, completion_tokens=30))

    def human(self):
        self.index.observe(dict(session_key='group:1', user_id='101', self_id='999',
            message_id='human', received_at=1000, direct_targets=['999'],
            text='我帮你整理出了这份具体的资料，已经发给你了'))
        with self.index.db() as db:
            return dict(db.execute("SELECT * FROM records WHERE origin='human'").fetchone())

    def item(self, row):
        return dict(event_id=row['id'], kind='relationship', directed_to_hakimi=True,
                    event_type='practical_help', evidence=row['text'], reason='具体资料协助')

    def assert_audit(self, source):
        with self.index.db() as db:
            audit = db.execute('SELECT details FROM relationship_review_audit WHERE source=? ORDER BY id DESC LIMIT 1', (source,)).fetchone()
            details = json.loads(audit[0])
            self.assertEqual(details['reviewer'], self.metadata['reviewer'])
            self.assertEqual(details['review_model'], self.metadata['review_model'])
            self.assertFalse(details['review_reasoning_override_verified'])

    def test_daily_and_note_consumer_propagate_trusted_api_provenance(self):
        row = self.human()
        # The queue's batching grace starts at discovery, not the chat timestamp.
        with self.index.db() as db:
            self.daily.discover(db, 1001)
        def run_daily(packets, base):
            return runtime.ReviewResult(dict(items=[], daily_events=[], relationship_events=[self.item(row)]), self.metadata)
        result = self.impressions.tick(self.base, 3000, runner=run_daily)
        self.assertNotEqual(result['status'], 'error')
        self.assert_audit('daily')
        self.index.remember_manual(dict(kind='person', user_id='101', key='资料协助', note='对方提供具体资料。'),
            session='group:1', provider='gemini', now=3100, origin_message_id='human')
        def run_note(packets, base):
            item = dict(self.item(row), id=packets[0]['id'])
            return runtime.ReviewResult(dict(items=[item]), self.metadata)
        with patch.object(self.daily, 'prepare', return_value=[]):
            self.impressions.tick(self.base, 3130, runner=run_note)
        self.assert_audit('note')

    def test_window_consumer_preserves_api_provenance_without_changing_score_policy(self):
        row = self.human()
        human = dict(message_id='human', user_id='101', received_at=1000)
        self.windows.receipt(self.base, 'group:1', 'r:0', '资料收到啦，谢谢你', 'bot-r', 'trigger', 1010, [human], ['human'])
        self.windows.close(self.base, 'group:1', 1020)
        def run_window(packet, base):
            return runtime.ReviewResult(dict(relationship_events=[self.item(row)]), self.metadata)
        with patch.object(type(self.index), 'sync', return_value={}):
            result = self.windows.tick(self.base, 1021, runner=run_window)
        self.assertEqual(result['status'], 'completed')
        self.assert_audit('window')
        with self.index.db() as db:
            policy = json.loads(db.execute('SELECT affinity_policy FROM social_assessments LIMIT 1').fetchone()[0])
            self.assertEqual(policy['classifier'], 'video_understanding_api')
            self.assertEqual(policy['evaluator'], 'program_event_policy_v3')
            self.assertEqual(policy['review_reasoning_effort'], 'low')


if __name__ == '__main__': unittest.main()
