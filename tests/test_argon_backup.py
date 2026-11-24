import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import Mock, patch
from hakimi.models.argon_session import ArgonSession, NoAvailableAccounts, messages, wire_name
from hakimi.models import vertex_route as vr


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); (self.root/'state').mkdir()
        self.config = {'enabled': True, 'model': 'gemini-4-argon-high', 'profile': str(self.root/'profile.json')}
        (self.root/'state/argon_backup.json').write_text(json.dumps(self.config))
        self.route = vr.Route(self.root, enabled=True, model='gemini-4-argon')

    def test_budget_only_and_month_recovery(self):
        budget = {'blocked': True, 'block_reason': 'vertex_monthly_budget_exhausted'}
        with patch.object(self.route, 'budget', return_value=budget), patch.object(vr, 'build_status', side_effect=lambda *a, **kw: dict(kw['budget'])), patch.object(vr, 'release_primary_gate') as release, patch.object(vr, 'apply_primary_gate') as apply:
            self.assertEqual(self.route.check(write_status=False)['active_provider'], 'argon_backup'); release.assert_called_once()
            budget['block_reason'] = 'manual_pause'
            self.assertTrue(self.route.check(write_status=False)['blocked']); apply.assert_called_once()
            budget.update(blocked=False, block_reason=None)
            self.assertEqual(self.route.check(write_status=False)['active_provider'], 'vertex')

    def test_tool_and_image_roundtrip(self):
        payload = {'contents': [{'role': 'model', 'parts': [{'functionCall': {'name': 'image.read', 'args': {'path': 'x'}, 'id': 'a'}}]}, {'role': 'user', 'parts': [{'functionResponse': {'name': 'image.read', 'id': 'a', 'response': {'ok': True}, 'parts': [{'inlineData': {'mimeType': 'image/png', 'data': 'abc'}}]}}]}]}
        rows = messages(payload)
        self.assertEqual(rows[0]['tool_calls'][0]['function']['name'], wire_name('image.read'))
        self.assertEqual(rows[1]['tool_call_id'], 'a')
        self.assertEqual(rows[2]['content'][0]['image_url']['url'], 'data:image/png;base64,abc')

    def test_stream_usage_and_progress(self):
        (self.root/'key').write_text('test')
        (self.root/'profile.json').write_text(json.dumps({'activated': True, 'provider': 'openai_compatible', 'base_url': 'https://example.invalid/v1', 'api_key_file': str(self.root/'key'), 'output_limit_parameter': 'max_tokens'}))
        events = [{'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'a', 'function': {'name': wire_name('image.read'), 'arguments': '{"path":"x"}'}}]}, 'finish_reason': 'tool_calls'}]}, {'choices': [], 'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30, 'completion_tokens_details': {'reasoning_tokens': 4}, 'prompt_tokens_details': {'cached_tokens': 3}}}]
        response = io.BytesIO(('\n'.join('data: '+json.dumps(e)+'\n' for e in events)+'data: [DONE]\n').encode())
        session = ArgonSession([], self.root, {}); session._progress = Mock()
        payload = {'contents': [], 'tools': [{'functionDeclarations': [{'name': 'image.read'}]}]}
        with patch('hakimi.models.argon_session.build_opener') as opener:
            opener.return_value.open.return_value = response
            result = session._backup_request(payload, 10, self.config, self.root)
        self.assertEqual(result['candidates'][0]['content']['parts'][0]['functionCall']['name'], 'image.read')
        self.assertEqual(result['usageMetadata']['candidatesTokenCount'], 6)
        self.assertEqual(result['usageMetadata']['thoughtsTokenCount'], 4)
        self.assertEqual(result['usageMetadata']['cachedContentTokenCount'], 3)
        session._progress.assert_called_once()
        self.assertTrue((self.root/'state/argon_backup_usage.jsonl').exists())

    def test_only_exact_monthly_http_error_switches(self):
        session = ArgonSession([], self.root, {})
        def rejected(*a):
            session.last_error = {'http_status': 429, 'message': 'vertex_monthly_budget_exhausted'}
            raise RuntimeError('vertex_quota')
        self.route.check = Mock(return_value={'blocked': False, 'active_provider': 'vertex'})
        with patch.object(vr, 'load', return_value=self.route), patch('hakimi.models.vertex_session.VertexSession._request', side_effect=rejected), patch.object(session, '_backup_request', return_value='backup') as backup:
            self.assertEqual(session._request({}, 10), 'backup'); backup.assert_called_once()
            self.assertEqual(session._request({}, 10), 'backup')
            self.assertEqual(backup.call_count, 2)
        session._using_backup = False  # Start a new QQ event.
        def unavailable(*a):
            session.last_error = {'http_status': 429, 'message': 'rate_limited'}
            raise RuntimeError('vertex_quota')
        with patch.object(vr, 'load', return_value=self.route), patch('hakimi.models.vertex_session.VertexSession._request', side_effect=unavailable), patch.object(session, '_backup_request') as backup:
            with self.assertRaises(RuntimeError): session._request({}, 10)
            backup.assert_not_called()



    def provider_profile(self):
        (self.root/'key').write_text('test')
        (self.root/'profile.json').write_text(json.dumps({'activated': True, 'provider': 'openai_compatible', 'base_url': 'https://example.invalid/v1', 'api_key_file': str(self.root/'key')}))
        self.config['fallback_model'] = 'gemini-3.8-flash-high'

    def test_vertex_flash_outage_uses_shared_flash_for_remainder_of_turn(self):
        self.provider_profile()
        (self.root/'state/argon_backup.json').write_text(json.dumps(self.config))
        self.route.check = Mock(return_value={'active_provider': 'vertex'})
        for error in ({'http_status': 502, 'message': 'upstream_auth_or_transport_failed'},
                      {'http_status': 429, 'status':'RESOURCE_EXHAUSTED', 'message':'resource exhausted'},
                      {'message': 'vertex_timeout_before_headers'}):
            session = ArgonSession([], self.root, {'AGY_LLM_GATEWAY_MODELS':'gemini-3.8-flash'})
            session.last_error = error
            with patch.object(vr, 'load', return_value=self.route), patch.object(session, '_vertex_request', side_effect=RuntimeError) as vertex, patch.object(session, '_provider_request', return_value='success') as shared:
                for _ in range(2):
                    self.assertEqual(session._request({},10), 'success')
                vertex.assert_called_once()
                self.assertEqual([call.args[-1] for call in shared.call_args_list], ['gemini-3.8-flash-high']*2)

    def test_flash_local_limits_and_lost_turn_do_not_call_shared_api(self):
        self.provider_profile()
        (self.root/'state/argon_backup.json').write_text(json.dumps(self.config))
        self.route.check = Mock(return_value={'active_provider': 'vertex'})
        for message in ['daily_request_limit', 'gateway_busy', 'vertex_budget_paused', 'upstream_auth_or_transport_failed']:
            session = ArgonSession([], self.root, {'AGY_LLM_GATEWAY_MODELS':'gemini-3.8-flash'})
            session.last_error = {'http_status':502 if message.startswith('upstream') else 429,
                                  'status':'RESOURCE_EXHAUSTED','message':message}
            session._progress = lambda: False if message.startswith('upstream') else True
            with patch.object(vr, 'load', return_value=self.route), patch.object(session, '_vertex_request', side_effect=RuntimeError), patch.object(session, '_provider_request') as shared:
                with self.assertRaises(RuntimeError):session._request({},10)
                shared.assert_not_called()

    def test_no_accounts_switches_flash_and_records_actual_model(self):
        self.provider_profile()
        session = ArgonSession([], self.root, {})
        rejected = HTTPError('https://example.invalid', 503, 'Unavailable', {}, io.BytesIO(json.dumps({'error': {'message': 'No available accounts: no available accounts'}}).encode()))
        success = io.BytesIO(b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n')
        with patch('hakimi.models.argon_session.build_opener') as opener:
            opener.return_value.open.side_effect = [rejected, success]
            result = session._backup_request({'contents': []}, 10, self.config, self.root)
            requests = opener.return_value.open.call_args_list
        self.assertEqual([json.loads(c.args[0].data)['model'] for c in requests], ['gemini-4-argon-high', 'gemini-3.8-flash-high'])
        self.assertEqual(result['modelVersion'], 'gemini-3.8-flash-high')
        ledger = [json.loads(line) for line in (self.root/'state/argon_backup_usage.jsonl').read_text().splitlines()]
        self.assertEqual([r['model'] for r in ledger], ['gemini-4-argon-high', 'gemini-3.8-flash-high'])
        self.assertEqual(ledger[0]['error_code'], 'no_available_accounts')
        self.assertIsNone(session.last_error)

    def test_other_503_never_switches(self):
        self.provider_profile()
        session = ArgonSession([], self.root, {})
        rejected = HTTPError('https://example.invalid', 503, 'Unavailable', {}, io.BytesIO(b'{"error":{"message":"temporary upstream failure"}}'))
        with patch('hakimi.models.argon_session.build_opener') as opener:
            opener.return_value.open.side_effect = rejected
            with self.assertRaises(OSError): session._backup_request({'contents': []}, 10, self.config, self.root)
            self.assertEqual(opener.return_value.open.call_count, 1)
        self.assertEqual(session.last_error['http_status'], 503)
        self.assertEqual(session.last_error['message'], 'argon_backup_failed')

    def test_flash_sticky_deadline_and_next_turn_reset(self):
        self.config['fallback_model'] = 'gemini-3.8-flash-high'
        session = ArgonSession([], self.root, {})
        with patch.object(session, '_provider_request', side_effect=[NoAvailableAccounts('no_available_accounts'), 'first', 'second']) as request, patch('hakimi.models.argon_session.time.monotonic', side_effect=[100, 104, 105]):
            self.assertEqual(session._backup_request({}, 10, self.config, self.root), 'first')
            self.assertEqual(session._backup_request({}, 5, self.config, self.root), 'second')
            calls = request.call_args_list
            self.assertEqual(calls[1].args[1], 6)
            self.assertEqual([c.args[4] for c in calls], ['gemini-4-argon-high', 'gemini-3.8-flash-high', 'gemini-3.8-flash-high'])
        with patch('hakimi.models.vertex_session.VertexSession.generate', return_value='new'):
            self.assertEqual(session.generate('prompt'), 'new')
            self.assertIsNone(session._backup_model)
        with patch.object(session, '_provider_request', side_effect=NoAvailableAccounts('no_available_accounts')) as request:
            with self.assertRaises(NoAvailableAccounts): session._backup_request({}, 10, self.config, self.root)
            self.assertEqual(request.call_count, 2)  # No recursive third provider attempt.


if __name__ == '__main__': unittest.main()
