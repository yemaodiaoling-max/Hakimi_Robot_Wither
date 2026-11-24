import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from hakimi.models.memory_retrieval import MemoryRetrieval
from hakimi.models.vertex_session import VertexSession


def parts(*names):
    return [{'functionCall': {'name': name, 'args': {}}} for name in names]


class RetrievalTests(unittest.TestCase):
    def test_progressive_search_reuses_results_without_crossing_scope(self):
        state = MemoryRetrieval()
        calls = []
        def execute(args):
            calls.append(args)
            return {'ok': True, 'stdout': json.dumps({'result': {
                'scope': args.get('scope', 'current'),
                'memories': [{'id': 'M' + str(i), 'session_key': 'group:A'}
                             for i in range(args['limit'])]}})}
        sizes = []
        for _ in range(4):
            state.begin(parts('memory.search'))
            result = state.run('memory.search', {'query': 'history', 'scope': 'current'}, execute)
            sizes.append(len(json.loads(result['stdout'])['result']['memories'])
                         if 'stdout' in result else 0)
        self.assertEqual([a['limit'] for a in calls], [3, 6, 12])
        self.assertEqual(sizes, [3, 3, 6, 0])
        self.assertTrue(all(a['scope'] == 'current' for a in calls))
        state.begin(parts('memory.search'))
        state.run('memory.search', {'query': 'history', 'scope': 'all_groups'}, execute)
        self.assertEqual(calls[-1]['limit'], 3)
        self.assertEqual(calls[-1]['scope'], 'all_groups')
        # Every new generation owns a fresh cache, with no cross-session data.
        fresh = MemoryRetrieval()
        fresh.begin(parts('memory.search'))
        fresh.run('memory.search', {'query': 'history'}, execute)
        self.assertEqual(calls[-1]['limit'], 3)

    def test_read_adds_only_unseen_text_and_preserves_pages_and_sources(self):
        state = MemoryRetrieval()
        calls = []
        text = 'x' * 5000
        def execute(args):
            calls.append(args)
            start, size = args['offset'], args['limit']
            end = min(len(text), start + size)
            return {'ok': True, 'stdout': json.dumps({'result': {
                'id': 'M1', 'text': text[start:end], 'offset': start,
                'next_offset': end if end < len(text) else None,
                'session_key': 'group:A', 'source_view': args.get('source', 'note')}})}
        for expected in [(0, 900), (900, 2100)]:
            state.begin(parts('memory.read'))
            result = state.run('memory.read', {'memory_id': 'M1'}, execute)
            page = json.loads(result['stdout'])['result']
            self.assertEqual((page['offset'], len(page['text'])), expected)
            self.assertEqual(page['next_offset'], sum(expected))
        state.begin(parts('memory.read'))
        self.assertTrue(state.run('memory.read', {'memory_id': 'M1'}, execute)['already_retrieved'])
        state.begin(parts('memory.read'))
        result = state.run('memory.read', {'memory_id': 'M1', 'offset': 3000}, execute)
        self.assertEqual(json.loads(result['stdout'])['result']['offset'], 3000)
        state.begin(parts('memory.read'))
        state.run('memory.read', {'memory_id': 'M1', 'source': 'message'}, execute)
        self.assertEqual(calls[-1]['offset'], 0)
        self.assertEqual(calls[-1]['source'], 'message')

    def test_errors_can_retry_and_batches_count_once(self):
        state = MemoryRetrieval()
        state.begin(parts('memory.search', 'memory.read', 'memory.graph'))
        self.assertEqual(state.rounds, 1)
        runner = Mock(return_value={'ok': False, 'error': 'temporary'})
        for _ in range(2):
            state.run('memory.graph', {'topic': 'x'}, runner)
        self.assertEqual(runner.call_count, 2)

    def test_real_session_stops_retrieval_at_eight_and_still_sends(self):
        requests, executed = [], []
        def transport(payload, timeout):
            requests.append(payload)
            n = len(requests)
            batch = [{'functionCall': {'name': 'memory.search',
                      'args': {'query': str(n), 'scope': 'current'}}}]
            if n == 1:
                batch.append({'functionCall': {'name': 'memory.graph', 'args': {'topic': 'x'}}})
            if n == 9:
                batch.append({'functionCall': {'name': 'reply.send', 'args': {'text': 'done'}}})
            return {'candidates': [{'content': {'role': 'model', 'parts': batch}}]}
        def tool(name, args, claim, timeout):
            executed.append((name, args))
            return {'ok': True}
        coordinator = Mock()
        with tempfile.TemporaryDirectory() as cwd, patch(
                'hakimi.prompt.tool_selection.select_names',
                return_value={'memory.search', 'memory.read', 'memory.graph', 'reply.send'}):
            session = VertexSession([], cwd, {}, transport=transport,
                                    tool_runner=tool, image_loader=lambda claim: [])
            result = session.generate('test', coordinator,
                                      {'token': 'T', 'payload': {'session_key': 'group:A'}})
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(sum(name == 'memory.search' for name, _ in executed), 8)
        self.assertEqual(executed[-1], ('reply.send', {'text': 'done'}))
        last_names = {d['name'] for d in requests[8]['tools'][0]['functionDeclarations']}
        self.assertFalse(last_names & MemoryRetrieval.names)
        self.assertIn('reply.send', last_names)
        self.assertEqual(len(requests), 9)

    def test_existing_persona_keeps_short_style_and_conditional_detail(self):
        from hakimi.prompt.prompt_language import PERSONA
        self.assertIn('日常15至90字', PERSONA)
        self.assertIn('一般问答先简短给结论和关键依据', PERSONA)
        self.assertIn('明确要求详细解释、完整推导或多项证据对照时再按需展开', PERSONA)

    def test_envelope_failure_retries_and_string_metadata_does_not_duplicate(self):
        state = MemoryRetrieval()
        state.begin(parts('memory.graph'))
        runner = Mock(return_value={'ok': True, 'stdout': '{"ok": false, "error": "temporary"}'})
        state.run('memory.graph', {'topic': 'x'}, runner)
        state.run('memory.graph', {'topic': 'x'}, runner)
        self.assertEqual(runner.call_count, 2)
        calls = []
        def search(args):
            calls.append(args)
            channel = 'term' if len(calls) == 1 else 'vector'
            rows = ['[M1] 人物·命中:' + channel + '·记昨天：original\n来源：group:A']
            if len(calls) == 3:
                rows.append('[M1] 人物·命中:vector·记昨天：new excerpt\n来源：group:A')
            return {'ok': True, 'stdout': json.dumps({'ok': True, 'result': {'memories': rows}})}
        results = []
        for query in ['one', 'two', 'three']:
            state.begin(parts('memory.search'))
            output = state.run('memory.search', {'query': query}, search)
            results.append(json.loads(output['stdout'])['result']['memories'])
        self.assertEqual([len(rows) for rows in results], [1, 0, 1])
        self.assertIn('new excerpt', results[-1][0])
