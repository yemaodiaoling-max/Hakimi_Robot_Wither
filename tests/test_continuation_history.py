import json
from pathlib import Path
import tempfile
import unittest
from hakimi.bridge import conversation_judge as judge
from hakimi.bridge.conversation_scheduler import ConversationScheduler


class ContinuationHistoryTests(unittest.TestCase):
    def test_old_exchange_supplies_context_without_becoming_candidates(self):
        history = [{'message_id': '100', 'user_id': '42', 'nickname': '群友8eed', 'text': '怎么也得凑个六翼'},
                   {'message_id': '101', 'user_id': 'SELF', 'nickname': '哈基米', 'text': '二翼是基础款普通天使吧'},
                   {'message_id': '102', 'user_id': 'SELF', 'nickname': '哈基米', 'text': '六翼炽天使那是高定配置了'}]
        rows = [{'message_id': '103', 'user_id': '42', 'nickname': '群友8eed', 'text': '又是断臂又是无貌', 'reply_to': '102'}]
        def runner(task, base):
            request = json.loads(task)
            self.assertEqual([r['text'] for r in request['previous_exchange']], [r['text'] for r in history])
            self.assertEqual(request['previous_exchange'][0]['name'], '群友8eed')
            self.assertEqual([r['text'] for r in request['messages']], ['又是断臂又是无貌'])
            self.assertEqual(request['messages'][0]['reply_to'], 'h3')
            self.assertNotIn('previous_reply', request)
            return {'selected_message_ids': ['h1', '1']}
        self.assertEqual(judge.select_messages(rows, 'duplicate answer', '.', runner=runner, history=history), ['103'])

    def test_existing_logs_resolve_the_selected_exchange_only(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); (base/'state').mkdir()
            rows = [{'session_key': 'group:123', 'message_id': '10', 'user_id': '42', 'nickname': '群友9d04', 'text': '原始问题', 'received_at': 1},
                    {'session_key': 'group:456', 'message_id': '10', 'user_id': '99', 'text': '其他群内容', 'received_at': 2}]
            sent = [{'session_key': 'group:123', 'trigger_id': 'turn1', 'message_id': '11', 'text': '第一条实际回复', 'at': 3},
                    {'session_key': 'group:123', 'trigger_id': 'turn1', 'message_id': '12', 'text': '第二条实际回复', 'at': 4},
                    {'session_key': 'group:123', 'trigger_id': 'turn2', 'message_id': '13', 'text': '其他轮回复', 'at': 5}]
            for name, data in [('chat_history.jsonl', rows), ('sent_messages.jsonl', sent)]:
                (base/'state'/name).write_text(''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in data), encoding='utf-8')
            result = judge.previous_exchange(base, 'group:123', ['10'], 'turn1')
            self.assertEqual([r['text'] for r in result], ['原始问题', '第一条实际回复', '第二条实际回复'])
            self.assertEqual([r['user_id'] for r in result], ['42', 'SELF', 'SELF'])

    def test_exchange_ids_survive_batch_rejection_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            scheduler = ConversationScheduler(); scheduler.restore(base, 100)
            messages = [{'message_id': str(i), 'user_id': '42', 'text': '具体问题', 'received_at': 100} for i in range(1, 7)]
            scheduler.offered_turn('group:123', 100, 'turn1', messages=messages)
            scheduler.sent_reply('group:123', '实际回复', 101, '20', 'turn1')
            scheduler.delivery_finished('group:123', 102)
            scheduler.reject_window_batch('group:123', 110, 111)
            restored = ConversationScheduler(); restored.restore(base, 112)
            state = restored.group('group:123')
            self.assertEqual(state.listen_source_ids, ['1','2','3','4','5','6'])
            self.assertEqual(state.listen_trigger_id, 'turn1')
            restored.stop_listening('group:123')
            self.assertEqual(state.listen_source_ids, [])
