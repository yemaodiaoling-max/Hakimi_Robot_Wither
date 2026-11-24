"""Ordinary private conversations, using the real coordinator and durable delivery guard."""
import json
import asyncio
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from hakimi.bridge import user_reply_quota as quota
from hakimi.bridge.conversation_scheduler import ConversationScheduler
from hakimi.bridge.delivery_guard import DeliveryGuard
from hakimi.bridge.interaction_policy import ignored
from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.models.fallback_supervisor import can_call, record_call
from hakimi.models import fallback_supervisor


class PrivateChatLimits(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.now = 10000
        self.coordinator = TurnCoordinator(self.root, clock=lambda: self.now)
        self.outbox = self.state / 'outbox.jsonl'

    def guard(self):
        return DeliveryGuard(self.state/'delivery_state.json', self.outbox,
                             {'max_messages_per_minute': 1}, clock=lambda: self.now)

    def turn(self, session, number):
        mid = str(number)
        return dict(session_key=session, trigger_id=session + '-' + mid, reply_target_message_id=mid,
                    recent_context_messages=[dict(message_id=mid,session_key=session,user_id='22',text='再来一句')])

    def send(self, guard, expected):
        self.assertEqual(guard.next_message(), expected)
        guard.begin_attempt()
        guard.finish_attempt('success')

    def test_private_burst_with_full_group_quota_and_delivery_window(self):
        c = self.coordinator
        guard = self.guard()
        group = 'group:123'
        dm = 'private:22'
        for n in range(7):
            self.assertTrue(c.publish(self.turn(group,n))['accepted'])
            claim = c.claim_primary()
            self.assertTrue(c.submit_reply(claim['token'], ['群回复'+str(n)], 'gemini')['accepted'])
            self.send(guard,(group,'群回复'+str(n)))
            if n < 6:self.now += 60
        self.assertEqual(c.publish(self.turn(group,9))['reason'], quota.LIMIT_REASON)
        for n in range(20):
            self.assertTrue(c.publish(self.turn(dm,n))['accepted'])
            claim = c.claim_primary()
            if n == 19:
                c.report_failure(claim['token'], 'unavailable')
                claim = c.claim_fallback(private_only=True)
            result = c.submit_reply(claim['token'], ['收到'], claim['provider'])
            self.assertTrue(result['accepted'])
            self.send(guard,(dm,'收到'))
            if n == 9:
                c = TurnCoordinator(self.root, clock=lambda: self.now)
                guard = self.guard()
        self.assertEqual(len(c.snapshot()[quota.HISTORY_KEY]), 7)
        rows = [json.loads(line) for line in self.outbox.read_text().splitlines()]
        self.assertEqual(sum(r['session_key']==dm and r['reply_user_id']=='22' for r in rows), 20)
        self.assertFalse(c.publish(self.turn(group,10))['accepted'])
        # A replay with a new transport ID but the same private source is still a duplicate.
        retry = dict(rows[-1], reply_id='replayed', segments=['收到'])
        with self.outbox.open('a') as stream:stream.write(json.dumps(retry)+'\n')
        self.assertIsNone(guard.next_message())
        self.assertEqual(guard.state['last_event']['status'], 'duplicate_content')

    def test_old_mixed_checkpoint_and_group_allowance(self):
        old = [dict(user_id='22',trigger_id=str(i),completed_at=self.now-10) for i in range(7)]
        self.coordinator._save(dict(self.coordinator._load_ready(), **{quota.HISTORY_KEY:old}))
        with self.outbox.open('w') as stream:
            for i, row in enumerate(old):
                stream.write(json.dumps(dict(row, reply_user_id='22', created_at=self.now-10,
                                             session_key='private:22' if i < 6 else 'group:123'))+'\n')
        self.assertTrue(self.coordinator.publish(self.turn('group:123',101))['accepted'])
        self.assertEqual(len(self.coordinator.snapshot()[quota.HISTORY_KEY]), 1)
        self.assertIsNone(self.coordinator.claim_fallback(private_only=True))

    def test_private_fallback_and_conflict_cooldown_do_not_consume_group_budget(self):
        status = {'calls':[9000]}
        group = {'payload': self.turn('group:123',1)}
        private = {'payload': self.turn('private:22',1)}
        settings = {'max_calls_per_hour':2}
        for i in range(12):
            self.assertTrue(can_call(status, settings, private))
            record_call(status, private, self.now+i)
        self.assertTrue(can_call(status, settings, group))
        record_call(status, group, self.now+20)
        self.assertFalse(can_call(status, settings, group))
        self.assertTrue(can_call(status, settings, private))
        self.assertEqual(status['limited_calls'],[9000,self.now+20])
        (self.state/'interaction_policy.json').write_text(json.dumps({'users':{'22':{'until':self.now+3600}}}))
        self.assertFalse(ignored('22', self.root, now=self.now, session='private:22'))
        self.assertTrue(ignored('22', self.root, now=self.now, session='group:123'))

    def test_private_input_wakes_through_stale_cooldown(self):
        scheduler = ConversationScheduler()
        session = 'private:22'
        cfg = {'lurker_schedule': {'instant_wake_on_at_or_private':False}}
        scheduler.group(session).cooldown_until = self.now+3600
        scheduler.observe(session,'你好','22','1',self.now)
        decision = scheduler.decide(session, cfg, self.now)
        self.assertTrue(decision.ready)
        self.assertEqual(decision.wait, 0)
        cfg['bot_profile']={'allow_private_chat':False}
        self.assertFalse(scheduler.decide(session, cfg, self.now).ready)


class PrivateFallbackLoop(unittest.IsolatedAsyncioTestCase):
    async def test_loop_claims_private_when_hourly_limit_and_pacing_are_both_hit(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            coordinator = TurnCoordinator(base)
            payload = {'session_key':'private:22', 'trigger_id':'private-dsh',
                       'reply_target_message_id':'1', 'recent_context_messages':[
                           {'session_key':'private:22', 'user_id':'22', 'message_id':'1', 'text':'你好'}]}
            self.assertTrue(coordinator.publish(payload)['accepted'])
            primary = coordinator.claim_primary()
            coordinator.report_failure(primary['token'], 'unavailable')
            path = base/'state/fallback_status.json'
            stamp = time.time()
            path.write_text(json.dumps({'calls':[stamp]}))
            settings = {'fallback':{'enabled':True,'max_calls_per_hour':1,'min_call_interval_seconds':1800}}
            def runner(prompt, root):
                return {'action':'reply','text':'收到'}
            with patch.object(fallback_supervisor,'fallback_prompt',return_value='test'), \
                 patch.object(fallback_supervisor.bilibili_video,'prepare',return_value=None), \
                 patch.object(fallback_supervisor,'prepare_images',return_value=[]), \
                 patch('hakimi.models.search_routing.prepare',return_value={}):
                task = asyncio.create_task(fallback_supervisor.fallback_loop(lambda:settings,base,runner=runner))
                try:
                    async with asyncio.timeout(8):
                        while True:
                            status = json.loads(path.read_text())
                            if status.get('state') == 'completed':
                                break
                            await asyncio.sleep(.025)
                    self.assertEqual(status['limited_calls'],[stamp])
                    self.assertEqual(len(status['calls']),2)
                    receipt = json.loads((base/'state/outbox.jsonl').read_text().splitlines()[-1])
                    self.assertEqual((receipt['session_key'],receipt['provider']),('private:22','dsh'))
                finally:
                    task.cancel()
                    await asyncio.gather(task,return_exceptions=True)
