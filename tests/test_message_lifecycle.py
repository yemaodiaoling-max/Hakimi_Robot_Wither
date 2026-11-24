import json,tempfile,time,unittest
from pathlib import Path
from hakimi.bridge.message_lifecycle import record
from hakimi.bridge.reply_format import text_bubbles
from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.console.message_log import recent_messages

class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.state=self.base/'state';self.state.mkdir()
        self.now=time.time();self.payload=dict(session_key='group:100',trigger_id='test',reply_target_message_id='1',pending_explicit_question_ids=['1'])
        self.write('chat_history.jsonl',[dict(session_key='group:100',message_id='1',user_id='22',received_at=self.now-100,text='问题')])
        self.write('conversation_decisions.jsonl',[dict(session_key='group:100',last_message_id='1',gate='waiting_batch',basis='collecting_mention')])
    def write(self,name,rows):(self.state/name).write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
    def decision(self):return next(r for r in recent_messages(self.base) if r['kind']=='received')['decision']
    def test_cancel_updates_same_message(self):
        record(self.base,self.payload,'context_ready')
        record(self.base,self.payload,'cancelled',reason='user_reply_limit',retry_after=60)
        d=self.decision();self.assertIn('上下文已收集完成',d);self.assertIn('半小时7轮',d);self.assertIn('已取消',d);self.assertIn('初始调度',d)
    def test_generation_error_without_outbox_or_route(self):
        record(self.base,self.payload,'published')
        record(self.base,self.payload,'generation_error',reason='FALLBACK_TOOL_INVALID')
        self.write('turn_events.jsonl',[dict(session_key='group:100',trigger_id='test',outcome='fallback_error',provider='dsh')])
        self.assertIn('FALLBACK_TOOL_INVALID',self.decision());self.assertIn('备用回复出错',self.decision())
    def test_partial_delivery_and_send_error(self):
        record(self.base,self.payload,'published')
        record(self.base,self.payload,'send_error',reason='SEND_TIMEOUT_UNKNOWN')
        self.write('outbox.jsonl',[dict(session_key='group:100',trigger_id='test',reply_target_id='1',segments=['a','b'],created_at=self.now)])
        self.write('sent_messages.jsonl',[dict(session_key='group:100',trigger_id='test',at=self.now,text='a')])
        self.assertIn('1/2',self.decision());self.assertIn('SEND_TIMEOUT_UNKNOWN',self.decision())
    def test_active_generation_linked_before_completion(self):
        (self.state/'turn_state.json').write_text(json.dumps(dict(status='fallback_active',trigger_id='test',payload=self.payload)))
        self.assertIn('备用通道正在生成',self.decision())
    def test_historical_limit_is_marked_inference(self):
        self.write('outbox.jsonl',[dict(session_key='group:100',trigger_id=f'old{i}',reply_user_id='22',created_at=self.now-200-i) for i in range(7)])
        self.write('sent_messages.jsonl',[dict(session_key='group:100',trigger_id=f'old{i}',at=self.now-199-i) for i in range(7)])
        self.assertIn('推断',self.decision());self.assertIn('半小时7轮',self.decision())
    def test_missing_history_is_not_reported_as_waiting_forever(self):
        self.assertIn('后续状态未记录',self.decision())
    def test_quota_rejection_is_logged_at_source(self):
        c=TurnCoordinator(self.base,clock=lambda:1000)
        history=[dict(user_id='22',trigger_id=f'old{i}',completed_at=900+i) for i in range(7)]
        with c._locked():
            s=c._load_ready();s['user_reply_history']=history;c._save(s)
        payload=dict(self.payload,created_at=1000,recent_context_messages=[dict(user_id='22',message_id='1',text='问题')])
        result=c.publish(payload)
        self.assertEqual(result['reason'],'user_reply_limit')
        self.assertIn('已取消',self.decision())
    def test_no_chat_or_credentials_in_audit(self):
        record(self.base,dict(self.payload,text='secret',token='credential'),'generation_error',reason='unsafe value secret')
        data=(self.state/'message_lifecycle.jsonl').read_text();self.assertNotIn('secret',data);self.assertNotIn('credential',data)

class Bubbles(unittest.TestCase):
    def test_full_width_ascii_and_mixed(self):
        self.assertEqual(text_bubbles('第一条｜第二条|第三条'),['第一条','第二条','第三条'])
        self.assertEqual(text_bubbles('甲｜｜乙\n丙'),['甲','乙','丙'])
    def test_literal_ampersands_and_plain_text(self):
        self.assertEqual(text_bubbles('普通聊天'),['普通聊天'])
        self.assertEqual(text_bubbles('甲&乙｜丙'),['甲&amp;乙','丙'])

if __name__=='__main__':unittest.main()
