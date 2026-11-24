import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch, Mock
from hakimi.bridge import conversation_judge as judge
from hakimi.models.dsh_text_worker import TextWorker
from hakimi.models.dsh_text_judge import JudgeError


class ResidentTests(unittest.TestCase):
    def test_mentions_use_off_and_continuation_keeps_low(self):
        rows = [{'message_id': '1', 'user_id': '123', 'nickname': '群友388', 'text': '猫今天吃饭了吗'}]
        with patch.object(judge, 'run_judge', return_value={'selected_message_ids': ['1']}) as run:
            judge.select_messages(rows, '', Path('.'), focus_ids=['1'], limit=12)
            self.assertEqual(run.call_args.kwargs['thinking'], 'off')
            judge.select_messages(rows, '', Path('.'))
            self.assertEqual(run.call_args.kwargs['thinking'], 'low')

    def test_process_reused_then_failed_connection_is_recreated(self):
        worker = TextWorker()
        starts = []
        def start(base, deadline):
            starts.append(base)
            worker.process = Mock()
            worker.process.poll.return_value = None
            worker.connection = Mock()
            worker.base = base
        def receive(timeout):
            import json
            request = json.loads(worker.connection.sendall.call_args.args[0])
            return {'id': request['id'], 'text': '{"selected_message_ids":["1"]}'}
        def close():
            worker.process = worker.connection = worker.job = worker.reader = None
        with tempfile.TemporaryDirectory() as directory, patch.object(worker, '_start', side_effect=start), patch.object(worker, '_receive', side_effect=receive), patch.object(worker, 'close', side_effect=close):
            base = Path(directory)
            (base/'state').mkdir()
            self.assertEqual(worker.run('one', base, 30, 'off')['selected_message_ids'], ['1'])
            worker.run('two', base, 30, 'low')
            self.assertEqual(len(starts), 1)
            worker._receive.side_effect = OSError('disconnected')
            with self.assertRaises(JudgeError): worker.run('three', base, 30, 'low')
            self.assertIsNone(worker.process)
            worker._receive.side_effect = receive
            worker.run('four', base, 30, 'low')
            self.assertEqual(len(starts), 2)
        worker.close()
