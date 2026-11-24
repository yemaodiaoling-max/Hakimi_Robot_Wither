import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

from hakimi.models.agy_session import AgySession


FAKE_CLI='''
import json,os,sys
turn=0
for line in sys.stdin:
    request=json.loads(line)
    text=request['message']['content']
    turn+=1
    if text=='hang':continue
    if text=='exit':sys.exit(1)
    if text=='error':
        print(json.dumps({'event':'result','result':{'status':'ERROR','error':'quota'}}),flush=True)
        continue
    frame={'conversation_id':'conversation-'+str(os.getpid()),'status':'SUCCESS',
           'response':str(turn),'num_turns':turn,
           'usage':{'input_tokens':turn*10,'output_tokens':turn*2,'total_tokens':turn*12}}
    print(json.dumps({'event':'step_update','step_update':{'step_type':'tool'}}),flush=True)
    # Split JSON across writes to exercise buffered protocol parsing.
    raw=json.dumps({'event':'result','result':frame})
    sys.stdout.write(raw[:10]);sys.stdout.flush()
    sys.stdout.write(raw[10:]+'\\n');sys.stdout.flush()
'''


class UsageTests(unittest.TestCase):
    def test_healthy_restart_resumes_only_once_and_restores_usage(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            state=Path(folder)/'session.json'
            state.write_text(json.dumps({'status':'ready','conversation_id':'same','turns':2,'usage':{'input_tokens':30}}))
            session=AgySession(['agy','--model','gemini-3.8-flash-low'],folder,{},state_path=state,resume=True)
            with patch('agy_session.subprocess.Popen') as start,patch('agy_session.selectors.DefaultSelector'):
                start.return_value.pid=123
                session.start()
                self.assertEqual(start.call_args.args[0][-2:],['--conversation','same'])
                result=session.accept({'conversation_id':'same','num_turns':3,'usage':{'input_tokens':50}})
                self.assertEqual(result['usage']['input_tokens'],20)
                self.assertIsNone(session.resume)
                session.process=None;session.selector=None
            state.write_text(json.dumps({'status':'error','conversation_id':'old'}))
            self.assertIsNone(AgySession([],folder,{},state_path=state,resume=True).resume)

    def test_cumulative_usage_is_reported_as_per_turn_deltas(self):
        session=AgySession([],'.',{})
        one=session.accept({'status':'SUCCESS','conversation_id':'one','num_turns':1,'usage':{'input_tokens':10}})
        two=session.accept({'status':'SUCCESS','conversation_id':'one','num_turns':2,'usage':{'input_tokens':25}})
        self.assertEqual(one['usage']['input_tokens'],10)
        self.assertEqual(two['usage']['input_tokens'],15)
        with self.assertRaisesRegex(RuntimeError,'conversation_changed'):
            session.accept({'conversation_id':'other'})


@unittest.skipIf(os.name=='nt','Real streaming pipe polling runs on the Linux deployment host')
class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve()
        script=self.base/'fake_cli.py';script.write_text(FAKE_CLI,encoding='utf-8')
        self.session=AgySession([sys.executable,'-u',str(script)],self.base,dict(os.environ),
                                state_path=self.base/'session.json',timeout=1)
        self.addCleanup(self.session.close)
        self.coordinator=Mock()

    def ask(self,text,token='first'):
        return self.session.generate(text,self.coordinator,{'token':token})

    def test_success_keeps_one_process_and_conversation_for_new_owner(self):
        first=self.ask('one');pid=self.session.process.pid
        second=self.ask('two','second')
        self.assertEqual(self.session.process.pid,pid)
        self.assertEqual(first['conversation_id'],second['conversation_id'])
        self.assertEqual(second['response'],'2')
        self.assertEqual(second['usage']['input_tokens'],10)
        self.coordinator.record_progress.assert_any_call('first')
        self.coordinator.record_progress.assert_any_call('second')
        self.assertFalse(self.session.process.stdin.closed)
        self.assertEqual(json.loads((self.base/'session.json').read_text())['turns'],2)

    def test_error_rebuilds_for_next_turn_without_replaying_failed_input(self):
        first=self.ask('one')
        self.assertEqual(self.ask('error')['status'],'ERROR')
        self.assertIsNone(self.session.process)
        next_turn=self.ask('next')
        self.assertEqual(next_turn['num_turns'],1)
        self.assertNotEqual(first['conversation_id'],next_turn['conversation_id'])

    def test_exit_and_timeout_close_the_session_and_allow_recovery(self):
        with self.assertRaisesRegex(RuntimeError,'session_ended'):self.ask('exit')
        self.assertIsNone(self.session.process)
        self.assertEqual(self.ask('recover')['status'],'SUCCESS')
        self.session.timeout=.05
        with self.assertRaises(TimeoutError):self.ask('hang')
        self.assertIsNone(self.session.process)
