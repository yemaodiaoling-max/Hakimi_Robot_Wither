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
                append(state/'chat_history.jsonl', **key, message_id='10', received_at=1)
                append(state/'outbox.jsonl', **key, reply_target_id='10', created_at=2,
                       segments=['a', 'b'], provider=provider, model='gemini-4-argon')
                for at in [3, 4]:
                    append(state/'sent_messages.jsonl', **key, at=at, provider=provider, text='bubble')
                if index < 4:
                    append(root/'state/task_model_routing.jsonl', **key, selected_model='gemini-4-argon',
                           actual_requests=[{'model':'earlier-tool-model'}, {'model':expected or 'unrelated'}],
                           error='TimeoutError' if index == 3 else None)
                rows = recent_messages(base, key['session_key'])
                self.assertEqual(len(rows), 4)
                for row in rows:
                    self.assertNotIn('gemini-4-argon', row['decision'])
                    self.assertNotIn('unrelated', row['decision'])
                    if expected:
                        self.assertIn('回复模型：'+expected, row['decision'])
                    elif provider == 'gemini':
                        self.assertIn('具体模型未确认', row['decision'])
                    if row['kind'] != 'received':
                        self.assertEqual(row['reply_model'], expected)
