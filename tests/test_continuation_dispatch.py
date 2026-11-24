import json,tempfile,unittest
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch
from hakimi.bridge import bridge_daemon as bridge, conversation_judge as judge
from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.bridge.delivery_guard import DeliveryGuard
from tests.test_conversation_scheduler import config

class StopLoop(Exception): pass

class ContinuationDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_queue_publish_and_provider_submission(self):
        for provider in ('gemini','dsh'):
            with self.subTest(provider=provider),tempfile.TemporaryDirectory() as folder,ExitStack() as patches:
                base=Path(folder);(base/'state').mkdir()
                engine=bridge.DynamicHakimiEngine();scheduler=engine.conversation_scheduler
                cfg=config();key='group:100'
                scheduler.sent_reply(key,'聊聊猫咪',1000,'123','prior');scheduler.delivery_finished(key,1000)
                row=dict(session_key=key,received_at=1010,user_id='22',message_id='1',text='还有个问题，你怎么看这个解释')
                scheduler.observe(key,row['text'],'22','1',1010)
                engine.context_windows[key]=[row];engine.unread_counts[key]=1
                coor=TurnCoordinator(base,clock=lambda:1020)
                async def stop(*a,**kw):raise StopLoop
                def selected(entry,rows,previous,base,**kw):
                    return judge.select_messages(rows,previous,base,runner=lambda *_:{'selected_message_ids':['1']})
                for name,value in [('engine',engine),('BASE_DIR',base),('PENDING_TRIGGER',base/'pending.json')]:
                    patches.enter_context(patch.object(bridge,name,value))
                patches.enter_context(patch.object(bridge,'load_config',return_value=cfg))
                patches.enter_context(patch.object(bridge,'coordinator_for',return_value=coor))
                patches.enter_context(patch.object(bridge,'delivery_in_progress',return_value=False))
                patches.enter_context(patch.object(bridge,'conversation_window_rows',return_value=[row]))
                patches.enter_context(patch('hakimi.transport.group_capabilities.can_autonomous',return_value=True))
                patches.enter_context(patch.object(bridge.time,'time',return_value=1020))
                patches.enter_context(patch.object(scheduler,'_pressures',return_value=(2,2,2,4)))
                score=patches.enter_context(patch.object(scheduler,'score_window_messages',wraps=scheduler.score_window_messages))
                patches.enter_context(patch.object(judge,'continuation_context',side_effect=selected))
                patches.enter_context(patch.object(bridge.asyncio,'sleep',side_effect=stop))
                with self.assertRaises(StopLoop):await bridge.adaptive_conversation_loop()
                self.assertEqual(score.call_count,1)
                self.assertEqual(score.call_args.kwargs['reviewed_message_ids'],('1',))
                state=coor.snapshot();self.assertEqual(state['status'],'queued')
                self.assertEqual(state['payload']['wake_mode'],'被@后监听窗口')
                self.assertGreaterEqual(state['payload']['interest_score'],.45)
                self.assertEqual(state['payload']['listening_window']['selected_message_ids'],['1'])
                guard=DeliveryGuard(base/'state/delivery_state.json',base/'state/outbox.jsonl',clock=lambda:1020)
                claim=coor.claim_primary()
                if provider=='dsh':
                    coor.report_failure(claim['token'],'unavailable');claim=coor.claim_fallback()
                self.assertTrue(coor.submit_reply(claim['token'],['离线回答'],provider)['accepted'])
                self.assertIsNotNone(guard.next_message());guard.begin_attempt();guard.finish_attempt('success')

    async def test_distinct_empty_and_ineligible_reasons(self):
        from hakimi.bridge.conversation_scheduler import ConversationScheduler
        s=ConversationScheduler();key='group:100'
        s.sent_reply(key,'猫咪',1000,'123');s.delivery_finished(key,1000)
        s.group(key).listen_started=1000
        stale=dict(received_at=990,user_id='22',message_id='1',text='这是旧消息')
        self.assertEqual(s.score_window_messages(key,config(),1020,[stale]).basis,'review_outside_window')
        ack=dict(stale,received_at=1010,text='哈哈')
        self.assertEqual(s.score_window_messages(key,config(),1020,[ack]).basis,'review_not_actionable')
        entry={'continuation_review':{'task':self.failed_future()}}
        with patch.object(judge,'_report_failure'):
            self.assertEqual(judge.continuation_context(entry,[],'',Path('.')),[])
        self.assertTrue(entry['continuation_review']['failed'])

    def failed_future(self):
        import asyncio
        task=asyncio.get_running_loop().create_future();task.set_exception(RuntimeError('offline'));return task
