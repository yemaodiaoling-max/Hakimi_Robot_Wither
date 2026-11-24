import json,tempfile,unittest
from pathlib import Path
from hakimi.bridge.delivery_guard import DeliveryGuard

class NetworkRecoveryTests(unittest.TestCase):
    def test_unknown_is_not_replayed_and_other_group_recovers_after_fifteen_seconds(self):
        for restart in (False,True):
            with self.subTest(restart=restart),tempfile.TemporaryDirectory() as folder:
                base=Path(folder);now=[1000.];out=base/'outbox.jsonl';state=base/'delivery_state.json'
                guard=DeliveryGuard(state,out,clock=lambda:now[0])
                with out.open('a') as stream:
                    for rid,group,text in [('uncertain','group:1','第一条'),('fresh','group:2','新回复')]:
                        stream.write(json.dumps({'reply_id':rid,'session_key':group,'segments':[text],'created_at':now[0]})+'\n')
                self.assertEqual(guard.next_message(),('group:1','第一条'));guard.begin_attempt()
                if restart:guard=DeliveryGuard(state,out,clock=lambda:now[0])
                else:guard.finish_attempt('unknown')
                self.assertEqual(guard.state['paused_until'],1015.)
                self.assertIn('uncertain',guard.state['completed_ids'])
                self.assertIsNone(guard.next_message())
                now[0]=1016.
                self.assertEqual(guard.next_message(),('group:2','新回复'))

    def test_explicit_rejection_keeps_separate_platform_pause(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);out=base/'outbox.jsonl';guard=DeliveryGuard(base/'state.json',out,clock=lambda:1000.)
            out.write_text(json.dumps({'reply_id':'r','session_key':'group:1','segments':['x'],'created_at':1000.})+'\n')
            guard.next_message();guard.begin_attempt();guard.finish_attempt('rejected')
            self.assertEqual(guard.state['paused_until'],2800.)

if __name__=='__main__':unittest.main()
