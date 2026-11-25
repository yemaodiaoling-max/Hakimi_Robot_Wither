import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hakimi.console.message_log import recent_messages


class ReplyModelLog(unittest.TestCase):
    def test_reply_actor_follows_successful_request_for_same_session_and_turn(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base = root/'official_test_runtime'
            (root/'state').mkdir()
            state = base/'state'
            state.mkdir(parents=True)

            def append(path, **record):
                with path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(record) + '\n')

            for index, expected in enumerate(['gemini-3.8-flash', 'gemini-4-carbon', None, None, None]):
                key = dict(session_key='group:'+str(index+1), trigger_id='same')
                provider = 'dsh' if index == 2 else 'gemini'
                actual_channel = 'antigravity' if index == 1 else 'vertex'
                channel = ['Vertex AI', 'Antigravity CLI', 'DSH（备用）',
                           'Gemini（具体通道未确认）', 'Gemini（具体通道未确认）'][index]
                wake = '相关话题续聊' if index == 1 else '即时唤醒(被@/私聊)'
                append(state/'chat_history.jsonl', **key, message_id='10', received_at=1)
                append(state/'outbox.jsonl', **key, reply_target_id='10', created_at=2,
                       segments=['a', 'b'], provider=provider, model='gemini-4-argon', wake_mode=wake)
                for at in [3, 4]:
                    append(state/'sent_messages.jsonl', **key, at=at, provider=provider, text='bubble')
                if index < 4:
                    append(root/'state/task_model_routing.jsonl', **key, selected_model='gemini-4-argon',
                           actual_requests=[{'model':'earlier-tool-model', 'provider':'argon_backup'},
                                            {'model':expected or 'unrelated', 'provider':actual_channel}],
                           error='TimeoutError' if index == 3 else None)
                rows = recent_messages(base, key['session_key'])
                self.assertEqual(len(rows), 4)
                for row in rows:
                    trigger = '相关话题续聊' if index == 1 else '群内 @'
                    self.assertIn('调用通道：'+channel, row['decision'])
                    self.assertIn('触发原因：'+trigger, row['decision'])
                    self.assertNotIn('Argon 备用 API', row['decision'])
                    self.assertNotIn('gemini-4-argon', row['decision'])
                    self.assertNotIn('unrelated', row['decision'])
                    if expected:
                        self.assertIn('回复模型：'+expected, row['decision'])
                    elif provider == 'gemini':
                        self.assertIn('具体模型未确认', row['decision'])
                    if row['kind'] != 'received':
                        self.assertEqual(row['reply_model'], expected)
                        self.assertEqual((row['reply_channel'], row['reply_trigger']), (channel,trigger))

    def test_private_receipt_uses_turn_event_and_previous_rows_do_not_cross_link(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base = root/'official_test_runtime'
            state = base/'state'
            state.mkdir(parents=True)
            (root/'state').mkdir()
            key = dict(session_key='private:7', trigger_id='turn')
            def write(path, records):
                path.write_text(''.join(json.dumps(record)+'\n' for record in records), encoding='utf-8')
            write(state/'sent_messages.jsonl', [
                dict(**key, at=5, provider='gemini', text='attributed'),
                dict(session_key='private:7', at=4, text='legacy'),
                dict(session_key='private:7', at=3, provider='dsh', text='fallback'),
                dict(session_key='private:7', trigger_id='old-audit', at=2, provider='gemini'),
                dict(session_key='private:7', trigger_id='failed', at=1, provider='gemini'),
            ])
            write(state/'turn_events.jsonl', [
                dict(**key, wake_mode='即时唤醒(被@/私聊)', outcome='replied', provider='gemini')])
            write(root/'state/task_model_routing.jsonl', [
                dict(**key, actual_requests=[dict(model='m',provider='qq_backup_api')]),
                dict(session_key='private:7',trigger_id='old-audit',actual_requests=[dict(model='old')]),
                dict(session_key='private:7',trigger_id='failed',
                     actual_requests=[dict(model='rejected',provider='vertex',error_code='protocol')]),
                dict(session_key='group:7',trigger_id='turn',
                     actual_requests=[dict(model='wrong',provider='vertex')])])
            both, legacy, fallback, old, failed = recent_messages(base, key['session_key'])
            self.assertEqual((both['reply_model'], both['reply_channel'], both['reply_trigger']),
                             ('m','QQ 备用 API','私聊消息'))
            self.assertIn('触发原因：私聊消息', both['decision'])
            self.assertIsNone(legacy['reply_channel'])
            self.assertIn('调用通道：未记录', legacy['decision'])
            self.assertEqual(fallback['reply_channel'], 'DSH（备用）')
            self.assertIn('触发原因：未记录', fallback['decision'])
            self.assertEqual((old['reply_model'], old['reply_channel']), ('old','Gemini（具体通道未确认）'))
            self.assertIsNone(failed['reply_model'])
            self.assertNotIn('Vertex AI', failed['decision'])
