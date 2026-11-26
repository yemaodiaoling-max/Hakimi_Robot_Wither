import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from hakimi.models import vertex_route
from hakimi.models.argon_session import ArgonSession
from hakimi.models.carbon_session import encode_request
from hakimi.models.tool_history import compact_contents
from hakimi.models.vertex_session import VertexSession
from hakimi.prompt.prompt_budget import estimate_tokens


_TEXT = 'Synthetic passage with punctuation and numbering. ' * 72
_ROWS = [{'message_id': f'msg-{i}', 'text_untrusted': f'Synthetic message {i}: ' + _TEXT[:570]}
         for i in range(6)]
_REF = 'identical_value_in_this_request'
_ENV = {'AGY_LLM_GATEWAY_URL': 'https://fixture.invalid', 'AGY_LLM_GATEWAY_MODELS': 'gemini-4-hydrogen',
        'AGY_LLM_GATEWAY_CA_CERT': 'unused', 'AGY_LLM_GATEWAY_API_KEY': 'fixture-only'}


def fixture():
    return [{'role': 'user', 'parts': [{'text': 'Synthetic evidence request.'}]}]


def add_batch(contents, *receipts):
    calls = []
    results = []
    for name, ident, body in receipts:
        call = {'name': name, 'args': {}}
        receipt = {'name': name, 'response': body}
        if ident is not None:
            call['id'] = ident
            receipt['id'] = ident
        calls.append({'functionCall': call, 'thoughtSignature': 'fixture-signature'})
        results.append({'functionResponse': receipt})
    contents.append({'role': 'model', 'parts': calls})
    contents.append({'role': 'user', 'parts': results})
    return results


def web_result(label, text=_TEXT):
    return {'ok': True, 'stdout': {'ok': True, 'tool': 'web.read', 'result': {
        'url': f'https://fixture.invalid/{label}', 'source_id': label,
        'offset': 0, 'total_chars': len(text), 'text': text}}, 'stderr': ''}


def text_field(contents, content_index, part_index=0):
    return contents[content_index]['parts'][part_index]['functionResponse']['response']['stdout']['result']['text']


def stream(part):
    event = {'candidates': [{'finishReason': 'STOP', 'content': {'role': 'model', 'parts': [part]}}]}
    response = io.BytesIO(('data: ' + json.dumps(event) + '\n\n').encode())
    response.headers = {'X-Gateway-Request-Id': 'fixture-request'}
    return response


class VertexToolHistoryTests(unittest.TestCase):
    def test_reference_restores_exact_field_and_keeps_latest_batch_and_unique_evidence(self):
        contents = fixture()
        add_batch(contents, ('web.read', 'read-1', web_result('W1')),
                  ('web.read', 'different-page', web_result('W3', _TEXT + ' unique ending')),
                  ('context.read', None, {'stdout': {'result': {'data': {'messages': _ROWS, 'counts': {'total': 10}}}}}))
        add_batch(contents, ('web.read', 'read-2', web_result('W2')),
                  ('context.read', None, {'stdout': {'result': {'data': {'messages': copy.deepcopy(_ROWS), 'counts': {'total': 12}}}}}))
        add_batch(contents, ('web.read', 'read-3', web_result('W4', _TEXT.replace('numbering', 'dates'))))
        before = copy.deepcopy(contents)
        wire = compact_contents(contents)
        self.assertEqual(contents, before)
        self.assertEqual(wire[-1], before[-1])
        self.assertEqual(wire[1::2], before[1::2])
        self.assertEqual(wire[2]['parts'][1], before[2]['parts'][1])
        self.assertEqual(wire[4], before[4])

        old_receipts = [part['functionResponse'] for part in wire[2]['parts']]
        self.assertEqual(old_receipts[0]['id'], 'read-1')
        self.assertEqual(old_receipts[0]['response']['stdout']['result']['source_id'], 'W1')
        self.assertEqual(old_receipts[2]['response']['stdout']['result']['data']['counts'], {'total': 10})
        body = json.dumps(wire)
        baseline = json.dumps(before)
        self.assertGreater(len(baseline) - len(body), 5000)
        self.assertGreater(estimate_tokens(baseline) - estimate_tokens(body), 1000)

        reconstructed = copy.deepcopy(wire)
        for part_index, path, target_id in ((0, 'stdout/result/text', 'read-2'),
                                             (2, 'stdout/result/data/messages', None)):
            data = reconstructed[2]['parts'][part_index]['functionResponse']['response']
            parents, field = path.rsplit('/', 1)
            for parent in parents.split('/'):
                data = data[parent]
            location = data[field][_REF]
            self.assertEqual(location['response_field'], '/' + path)
            self.assertEqual(location.get('call_id'), target_id)
            target = wire[location['content_number'] - 1]['parts'][location['part_number'] - 1]['functionResponse']
            self.assertEqual(target['name'], location['tool'])
            value = target['response']
            for key in path.split('/'):
                value = value[key]
            data[field] = value
        self.assertEqual(reconstructed, before)

    def test_single_batch_and_small_fields_have_zero_wire_changes(self):
        single = fixture()
        add_batch(single, ('web.read', 'read-1', web_result('W1')),
                  ('web.read', 'read-2', web_result('W2')))
        self.assertIs(compact_contents(single), single)
        small = fixture()
        for ident in ('read-1', 'read-2'):
            add_batch(small, ('web.read', ident, web_result(ident, 'short text')),
                      ('context.read', None, {'messages': ['简短记录' * 30]}))
        self.assertIs(compact_contents(small), small)

    def test_reference_anchor_stays_stable_as_new_duplicates_arrive(self):
        contents = fixture()
        for number in (1, 2):
            add_batch(contents, ('web.read', f'read-{number}', web_result(f'W{number}')))
        after_two = compact_contents(contents)
        self.assertEqual(text_field(after_two, 2)[_REF]['call_id'], 'read-2')
        self.assertEqual(text_field(after_two, 4), _TEXT)
        add_batch(contents, ('web.read', 'read-3', web_result('W3')))
        after_three = compact_contents(contents)
        self.assertEqual(after_three[:5], after_two)
        self.assertEqual(text_field(after_three, 6), _TEXT)
        add_batch(contents, ('web.read', 'read-4', web_result('W4')))
        wire = compact_contents(contents)
        self.assertEqual(wire[:5], after_two)
        self.assertEqual(text_field(wire, 6)[_REF]['call_id'], 'read-2')
        self.assertEqual(text_field(wire, 8), _TEXT)

    def test_no_matches_across_tools_paths_or_partial_receipts(self):
        contents = fixture()
        old = add_batch(contents, ('diary.read', 'other-tool', web_result('W1')),
                        ('web.read', 'other-path', {'text': _TEXT}),
                        ('web.read', 'error-result', dict(web_result('W1'), ok=False)),
                        ('web.read', 'media-result', web_result('W1')))
        old[-1]['functionResponse']['parts'] = [{'inlineData': {'mimeType': 'image/png', 'data': 'pixels'}}]
        add_batch(contents, ('web.read', 'read-2', web_result('W2')))
        self.assertIs(compact_contents(contents), contents)

    def test_native_session_compresses_wire_history_and_keeps_full_retry(self):
        def call(name, ident, **args):
            return {'functionCall': {'name': name, 'args': args, 'id': ident}, 'thoughtSignature': 'synthetic'}

        reads = [call('web.read', f'read-{i}', url=f'https://fixture.invalid/W{i}') for i in (1, 2)]
        requests = []
        canonical = []

        def open_fixture(request, **kwargs):
            requests.append(json.loads(request.data))
            canonical.append(copy.deepcopy(session.contents))
            if len(requests) <= 2:
                return stream(reads[len(requests) - 1])
            if len(requests) == 3:
                error = io.BytesIO(b'{"error":{"message":"fixture temporary error"}}')
                raise HTTPError('https://fixture.invalid', 503, 'Fixture unavailable', {}, error)
            return stream(call('reply.send', 'reply-1', text='finished'))

        def run_tool(name, args, claim, timeout):
            if name == 'web.read':
                result = web_result(args['url'].rsplit('/', 1)[1])
                result['stdout'] = json.dumps(result['stdout'])
                return result
            return {'ok': True}

        with tempfile.TemporaryDirectory() as cwd:
            session = VertexSession([], cwd, _ENV, tool_runner=Mock(side_effect=run_tool), image_loader=lambda _: [])
            with (patch('hakimi.models.vertex_session.ssl.create_default_context'),
                  patch('hakimi.models.vertex_session.build_opener') as opener,
                  patch('hakimi.models.vertex_session.time.sleep'),
                  patch('hakimi.prompt.tool_selection.select_names', return_value={'web.read', 'reply.send'})):
                opener.return_value.open.side_effect = open_fixture
                result = session.generate('synthetic test', Mock(), {'token': 'fixture', 'payload': {'session_key': 'group:fixture'}})
        self.assertTrue(result['reply_submitted'])
        self.assertEqual(len(requests), 4)
        self.assertEqual(requests[1]['contents'], canonical[1])
        self.assertEqual(text_field(requests[2]['contents'], 2)[_REF]['call_id'], 'read-2')
        self.assertEqual(text_field(canonical[2], 2), _TEXT)
        self.assertEqual(requests[3]['contents'], canonical[2])
        self.assertEqual([entry.args[0] for entry in session.tool_runner.call_args_list], ['web.read', 'web.read', 'reply.send'])

    def test_carbon_text_and_argon_backup_receive_complete_history(self):
        contents = fixture()
        add_batch(contents, ('web.read', 'read-1', web_result('W1')))
        add_batch(contents, ('web.read', 'read-2', web_result('W2')))
        body, _ = encode_request({'contents': contents})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route = Mock(root=root, model='gemini-4-argon')
            route.check.return_value = {'active_provider': 'vertex'}
            carbon = VertexSession([], root, dict(_ENV, AGY_LLM_GATEWAY_MODELS='gemini-4-carbon'))
            argon = ArgonSession([], root, dict(_ENV, AGY_LLM_GATEWAY_MODELS='gemini-4-argon'))
            limit = io.BytesIO(json.dumps({'error': {'message': vertex_route.MONTHLY_EXHAUSTED}}).encode())
            rejected = HTTPError('https://fixture.invalid', 429, 'Fixture limit', {}, limit)
            with (patch('hakimi.models.vertex_session.ssl.create_default_context'),
                  patch('hakimi.models.vertex_session.build_opener') as opener,
                  patch('hakimi.models.argon_session.backup_config', return_value={'enabled': True}),
                  patch.object(vertex_route, 'load', return_value=route),
                  patch.object(argon, '_backup_request', return_value={'backup': True}) as backup):
                opener.return_value.open.side_effect = [stream({'text': 'fixture'}), rejected]
                carbon._request(body, 10)
                uncompressed = {'contents': contents}
                self.assertEqual(argon._request(uncompressed, 10), {'backup': True})
        sent = [json.loads(entry.args[0].data) for entry in opener.return_value.open.call_args_list]
        self.assertEqual(sent[0], body)
        self.assertEqual(text_field(sent[1]['contents'], 2)[_REF]['call_id'], 'read-2')
        self.assertIs(backup.call_args.args[0], uncompressed)
        self.assertEqual(text_field(uncompressed['contents'], 2), _TEXT)


if __name__ == '__main__':
    unittest.main()
