"""Offline checks for the shared understanding API and trusted review provenance."""
from copy import deepcopy
import io
import json
import time
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'src'))
for root in (project, project.parent):
    companion_source = root / 'bili_companion' / 'src'
    if companion_source.is_dir():
        sys.path.insert(0, str(companion_source))
        break

from hakimi.curation import review_runtime as runtime, review_sol_transport as sol_transport
from bili_companion.cognition import companion_text
from bili_companion.cognition.openai_provider import OpenAICompatibleClient
from bili_companion.cognition.video_budget import video_budget


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
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.key = self.root / 'api_key'
        self.key.write_text('synthetic-review-key')
        self.flash = 'gemini-3.8-flash'
        self.sol = 'gpt-6.2-sol'
        self.base_url = 'https://blameupstream.com/v1'
        self.url = self.base_url + '/responses'
        self.config = dict(state_dir=str(self.root), qq_flash_low_profile=str(self.root / 'flash.json'),
                           qq_review_sol_profile=str(self.root / 'sol.json'))
        self.aux = self.root / 'aux_config.json'
        self.set_review_mode('companion_duty')
        self.write_profile(self.config['qq_flash_low_profile'], self.flash, 'openai', 'max_tokens')
        self.write_profile(self.config['qq_review_sol_profile'], self.sol, 'responses', 'max_output_tokens')
        self.value = dict(status='completed', model=self.sol + '-2026-11-20',
                          output=[dict(type='message', role='assistant', status='completed',
                                       content=[dict(type='output_text', text='{"items":[]}')])],
                          usage=dict(input_tokens=120, output_tokens=80, actual_cost_usd='0.00042',
                                     output_tokens_details=dict(reasoning_tokens=12)))
        self.sol_opener, self.flash_opener = Mock(), Mock()
        self.sol_opener.open.return_value = Response(self.value, self.url)
        primary_error = HTTPError(
            self.base_url + '/chat/completions', 429, 'private upstream detail', {}, io.BytesIO(b''))
        self.addCleanup(primary_error.close)
        self.flash_opener.open.side_effect = primary_error
        patches = (
            patch.object(runtime.video_hydrogen, 'review_backend', return_value=(companion_text, self.config)),
            patch.object(companion_text, 'build_opener', return_value=self.flash_opener),
            patch.object(sol_transport, 'build_opener', return_value=self.sol_opener),
            patch.object(OpenAICompatibleClient, 'usage_snapshot', return_value=None),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def set_review_mode(self, mode, *, enabled=True):
        self.aux.write_text(json.dumps({'review': {'mode': mode, 'enabled': enabled}}))

    def write_profile(self, path, model, protocol, output_field, *, key=None):
        verification = dict(structured_output=True, input_bound=True, output_token_cap=True, usage=True,
                            base_url=self.base_url, output_limit_parameter=output_field, media_mode='text_only',
                            max_output_tokens=8192, verified_at=time.time())
        price = dict(billing_currency='USD', input_usd_per_million='3.6', output_usd_per_million='12',
                     source='test tariff', input_modalities=['text'],
                     source_price=dict(input_per_million='2', output_per_million='10'))
        raw = dict(version=1, activated=True, provider='openai_compatible', ledger_currency='USD',
                   base_url=self.base_url, api_key_file=str(key or self.key), wire_protocol=protocol,
                   observer_model=model, supervisor_model=model, decider_model=model, media_mode='text_only',
                   input_token_upper_bound=180000, output_limit_parameter=output_field, max_output_tokens=8192,
                   reasoning_effort='low', single_pass=True, child_roles_enabled=False,
                   verification={model: verification}, price_source='operator_tariff', prices={model: price})
        Path(path).write_text(json.dumps(raw))

    def test_primary_429_falls_back_to_sol_on_same_account_and_settles_fee(self):
        result = runtime.classify('依据给定事件返回JSON\n{"text":"不要忘记约定"}', self.root)
        self.assertEqual(result, {'items': []})
        self.flash_opener.open.assert_called_once()
        self.sol_opener.open.assert_called_once()
        flash_request = self.flash_opener.open.call_args.args[0]
        request = self.sol_opener.open.call_args.args[0]
        self.assertEqual(request.full_url, self.url)
        self.assertEqual(request.get_header('Authorization'), flash_request.get_header('Authorization'))
        payload = json.loads(request.data)
        self.assertEqual(payload['model'], self.sol)
        self.assertEqual(payload['reasoning']['effort'], 'low')
        self.assertEqual(payload['max_output_tokens'], 8192)
        self.assertEqual(payload['text']['format'], {'type': 'json_object'})
        self.assertNotIn('tools', payload)
        self.assertIn('不要忘记约定', payload['input'][-1]['content'][0]['text'])
        metadata = runtime.reviewer_metadata(result)
        self.assertEqual(metadata['review_account'], 'original')
        self.assertEqual(metadata['review_model'], self.sol)
        self.assertEqual(metadata['review_reported_model'], self.value['model'])
        self.assertEqual(metadata['review_usage']['reasoning_tokens'], 12)
        self.assertEqual(metadata['review_billing_state'], 'settled')
        rows = {row['id']: row for row in video_budget(self.config, lane='review').entries()}
        settled = rows.pop(metadata['review_request_id'])
        self.assertEqual((settled['lane'], settled['state'], settled['actual']), ('review', 'settled', 420))
        self.assertEqual([row['state'] for row in rows.values()], ['unknown'])

    def test_explicit_sol_only_supports_configured_and_legacy_companion(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                if legacy:
                    self.aux.unlink()
                self.assertTrue(runtime.sol_backfill_enabled(self.root))
                self.assertEqual(runtime.classify_sol('return JSON', self.root), {'items': []})
        self.flash_opener.open.assert_not_called()
        self.assertEqual(self.sol_opener.open.call_count, 2)

    def test_disabled_and_non_companion_do_not_call_sol(self):
        for mode, enabled, code in (
            ('companion_duty', False, 'REVIEW_DISABLED'),
            ('disabled', True, 'REVIEW_DISABLED'),
            ('custom_api', True, 'REVIEW_API_CONFIG'),
            ('main_gateway', True, 'REVIEW_API_CONFIG'),
        ):
            with self.subTest(mode=mode, enabled=enabled):
                self.set_review_mode(mode, enabled=enabled)
                self.assertFalse(runtime.sol_backfill_enabled(self.root))
                self.assert_sol_error(code)
        self.sol_opener.open.assert_not_called()
        self.flash_opener.open.assert_not_called()

    def test_different_account_fails_before_request(self):
        other_key = self.root / 'other-key'
        other_key.write_text('different-synthetic-key')
        self.write_profile(self.config['qq_review_sol_profile'], self.sol, 'responses',
                           'max_output_tokens', key=other_key)
        self.assertFalse(runtime.sol_backfill_enabled(self.root))
        self.assert_sol_error('REVIEW_API_CONFIG')
        self.sol_opener.open.assert_not_called()

    def test_application_budget_denial_prevents_external_calls(self):
        self.config['daily_usd'] = '.000001'
        self.assert_sol_error('REVIEW_BUDGET_PAUSED')
        with self.assertRaisesRegex(runtime.ReviewAPIError, '^REVIEW_API_QUOTA$'):
            runtime.classify('return JSON', self.root)
        self.flash_opener.open.assert_not_called()
        self.sol_opener.open.assert_not_called()

    def assert_sol_error(self, code):
        with self.assertRaisesRegex(runtime.ReviewAPIError, '^' + code + '$'):
            runtime.classify_sol('return JSON', self.root)

    def test_sol_rejects_truncation_tools_refusals_and_invalid_usage(self):
        for mutation, code in (
            ('truncated', 'REVIEW_API_INCOMPLETE'), ('tools', 'REVIEW_API_INVALID_RESPONSE'),
            ('refusal', 'REVIEW_API_INVALID_RESPONSE'), ('usage', 'REVIEW_API_INVALID_USAGE'),
            ('model', 'REVIEW_API_INVALID_RESPONSE'),
        ):
            with self.subTest(mutation=mutation):
                value = deepcopy(self.value)
                if mutation == 'truncated':
                    value['status'] = 'incomplete'
                elif mutation == 'tools':
                    value['output'].append({'type': 'function_call'})
                elif mutation == 'refusal':
                    value['output'][0]['content'].append({'type': 'refusal', 'refusal': 'test'})
                elif mutation == 'usage':
                    value['usage']['input_tokens'] = True
                else:
                    value['model'] = 'other-model'
                before = self.sol_opener.open.call_count
                self.sol_opener.open.return_value = Response(value, self.url)
                self.assert_sol_error(code)
                self.assertEqual(self.sol_opener.open.call_count, before + 1)

    def test_sol_http_errors_are_sanitized_and_remain_unsettled(self):
        for error, code in (
            (HTTPError(self.url, 401, 'private upstream detail', {}, io.BytesIO(b'private-key')),
             'REVIEW_API_AUTH'),
            (TimeoutError('private-key'), 'REVIEW_API_UNAVAILABLE'),
        ):
            with self.subTest(code=code):
                before = self.sol_opener.open.call_count
                self.sol_opener.open.side_effect = error
                self.assert_sol_error(code)
                self.assertEqual(self.sol_opener.open.call_count, before + 1)
        entries = video_budget(self.config, lane='review').entries()
        self.assertEqual([row['state'] for row in entries], ['unknown', 'unknown'])

    def test_existing_json_contract_and_size_bound_remain(self):
        self.assertEqual(runtime.parse_json('```json\n{"items":[]}\n```'), {'items': []})
        for text in ('[]', '{"x":"' + 'a' * 32000 + '"}', '{"x":NaN}'):
            with self.assertRaises(ValueError):
                runtime.parse_json(text, strict=True)
        self.assertEqual(runtime.reviewer_metadata({'review_metadata': {'reviewer': 'forged'}}), {})


class ReviewProvenanceTests(unittest.TestCase):
    def setUp(self):
        from hakimi.memory import daily_events
        from hakimi.curation import impression_events, post_reply_review
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
