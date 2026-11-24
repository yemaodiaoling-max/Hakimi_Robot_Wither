import unittest
from concurrent.futures import Future
from unittest.mock import Mock, patch

from hakimi.bridge import conversation_judge as judge


class RefinementTests(unittest.TestCase):
    def setUp(self):
        judge._review_task = None

    def test_polling_serializes_workers_and_preserves_candidates(self):
        pending = Future()
        entry, other = {}, {}
        rows = [{'message_id': 'one', 'text': 'hello'}]
        with patch.object(judge.asyncio, 'to_thread', new=Mock(return_value='worker')) as worker, \
             patch.object(judge.asyncio, 'create_task', return_value=pending):
            self.assertIsNone(judge.continuation_context(entry, rows, '', '.'))
            self.assertIsNone(judge.continuation_context(other, rows, '', '.'))
            rows[0]['message_id'] = 'changed'
            self.assertEqual(worker.call_count, 1)
            self.assertEqual(worker.call_args.kwargs['timeout_seconds'], 30)
            self.assertEqual(worker.call_args.args[1][0]['message_id'], 'one')
            pending.set_result(['one'])
            self.assertEqual(judge.continuation_context(entry, rows, '', '.'), ['one'])

    def test_mention_timeout_falls_back_to_anchors_and_relevant_rows(self):
        rows = [{'message_id': str(i), 'user_id': 'a' if i < 5 else 'b',
                 'text': 'question', 'received_at': 100+i} for i in range(6)]
        entry, pending = {}, Future()
        with patch.object(judge.asyncio, 'to_thread', new=Mock(return_value='worker')) as worker, \
             patch.object(judge.asyncio, 'create_task', return_value=pending):
            self.assertIsNone(judge.mention_context(entry, rows, [rows[4]], '.'))
            pending.set_exception(RuntimeError('timeout'))
            with patch.object(judge, '_report_failure'):
                result = judge.mention_context(entry, rows, [rows[4]], '.')
            self.assertEqual([r['message_id'] for r in result], ['1', '2', '3', '4'])
            self.assertEqual(worker.call_args.kwargs['timeout_seconds'], 30)

    def test_custom_runner_remains_two_argument(self):
        rows = [{'message_id': 'one', 'user_id': 'a', 'text': 'hello'}]
        self.assertEqual(judge.select_messages(rows, '', '.',
            runner=lambda task, base: {'selected_message_ids': ['one']}), ['one'])

    def test_compact_references_keep_names_text_and_restore_real_ids(self):
        import json
        rows = [{'message_id':'8165862396903856010', 'user_id':'42', 'nickname':'小树',
                 'text':'小树问：那一步为什么？', 'received_at':100}]
        def runner(task, base):
            data=json.loads(task)
            self.assertEqual(data['messages'][0]['name'], '小树')
            self.assertEqual(data['messages'][0]['text'], rows[0]['text'])
            self.assertEqual(data['focus_message_ids'], ['1'])
            self.assertNotIn('received_at', data['messages'][0])
            return {'selected_message_ids':['1']}
        self.assertEqual(judge.select_messages(rows,'','.',runner=runner,
            focus_ids=[rows[0]['message_id']],limit=12),[rows[0]['message_id']])

    def test_new_anchor_is_kept_without_restarting_review(self):
        rows=[{'message_id':'old','user_id':'a','text':'first','received_at':100}]
        later={'message_id':'new','user_id':'a','text':'补充条件','received_at':101}
        entry,pending,ready={},Future(),Mock()
        with patch.object(judge.asyncio,'to_thread',new=Mock(return_value='worker')) as worker, \
             patch.object(judge.asyncio,'create_task',return_value=pending):
            self.assertIsNone(judge.mention_context(entry,rows,rows,'.',on_ready=ready))
            pending.set_result(['old'])
            result=judge.mention_context(entry,[*rows,later],[*rows,later],'.',on_ready=ready)
            self.assertEqual([r['message_id'] for r in result],['old','new'])
            self.assertEqual(worker.call_count,1)
            ready.assert_called_once()

    def test_explicit_output_protocol_and_phase_diagnostics(self):
        import json
        from hakimi.models.dsh_text_judge import _phase_times, parse_selection, JudgeError
        def runner(task, base):
            payload=json.loads(task)
            self.assertEqual(payload['output_format'],{'selected_message_ids':['message_id']})
            return parse_selection('{"selected_message_ids":["1"]}')
        self.assertEqual(judge.select_messages([{'message_id':'real-id','user_id':'a','text':'问题'}],
            '', '.', runner=runner, focus_ids=['real-id'],limit=12),['real-id'])
        events=[{'phase':'api_request','at':2000},{'phase':'api_headers','at':2600,'status':200},
                {'phase':'first_reasoning','at':2900},{'phase':'api_finish','at':7000,'reason':'length'}]
        wire=('unlogged reasoning\n'+'\n'.join('HAKIMI_TRACE '+json.dumps(e) for e in events)).encode()
        phases=_phase_times(wire,1000)
        self.assertEqual(phases['startup_ms'],1000)
        self.assertEqual(phases['api_headers_ms'],600)
        self.assertEqual(phases['generation_ms'],4400)
        self.assertEqual(phases['finish_reason'],'length')
        with self.assertRaises(JudgeError):parse_selection('{"selected_message_ids":')
