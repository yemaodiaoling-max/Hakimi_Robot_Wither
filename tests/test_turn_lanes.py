import json
import tempfile
from pathlib import Path
import unittest
from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.bridge.turn_lanes import lane_context, active_context_path


class LaneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.now = 1000
        self.coordinator = TurnCoordinator(self.base, clock=lambda: self.now)

    def payload(self, group='123', mode='即时唤醒(被@/私聊)', **extra):
        return dict(session_key='group:'+group, wake_mode=mode, created_at=self.now,
                    recent_context_messages=[{'message_id':'10','session_key':'group:'+group,'user_id':'42','text':'猫猫你好'}], **extra)

    def test_same_and_different_groups_can_fill_four_slots(self):
        results = [self.coordinator.publish(self.payload(group)) for group in ['123','123','456','789']]
        self.assertTrue(all(r['accepted'] for r in results))
        self.assertEqual([r['conversation_lane'] for r in results], ['', 'at1', 'at2', 'at3'])
        self.assertFalse(self.coordinator.publish(self.payload())['accepted'])
        self.assertTrue(self.coordinator.any_lane_active())
        for lane in self.coordinator.lane_ids():
            owner = self.coordinator.for_lane(lane)
            claim = owner.claim_primary()
            self.assertIsNotNone(claim)
            self.assertTrue(owner.check_token(claim['token'], 'gemini'))
            if lane:self.assertFalse(self.coordinator.check_token(claim['token'], 'gemini'))

    def test_background_waits_and_continuation_reuses_its_free_lane(self):
        self.coordinator.publish(self.payload())
        self.coordinator.publish(self.payload('456'))
        self.assertFalse(self.coordinator.publish(self.payload('789', '上下文积累判断'))['accepted'])
        at = self.coordinator.for_lane('at1');claim = at.claim_primary();at.complete(claim['token'])
        result = self.coordinator.publish(self.payload('456','被@后监听窗口',conversation_lane='at1'))
        self.assertTrue(result['accepted'])
        self.assertEqual(result['conversation_lane'], 'at1')
        self.assertFalse(self.coordinator.publish(self.payload('789', '冷场主动抛话题'))['accepted'])

    def test_sent_rounds_share_quota_and_outbox(self):
        self.coordinator.publish(self.payload()); self.coordinator.publish(self.payload('456'))
        for lane in ['', 'at1']:
            owner = self.coordinator.for_lane(lane);claim = owner.claim_primary()
            result = owner.submit_reply(claim['token'], ['猫猫收到'], 'gemini')
            self.assertTrue(result['accepted'], result)
        root = self.coordinator.snapshot()
        self.assertEqual(len(root['user_reply_history']), 2)
        self.assertEqual(len(self.coordinator.for_lane('at1')._read()['user_reply_history']), 2)
        rows = [json.loads(s) for s in (self.base/'state/outbox.jsonl').read_text().splitlines()]
        self.assertEqual([row['conversation_lane'] for row in rows], ['', 'at1'])
        self.assertEqual([row['session_key'] for row in rows], ['group:123', 'group:456'])

    def test_contexts_and_restart_keep_lane_identity(self):
        first = active_context_path(self.base)
        with lane_context('at1'):
            self.assertNotEqual(first, active_context_path(self.base))
            self.assertEqual(TurnCoordinator(self.base).lane_id, 'at1')
        self.coordinator.publish(self.payload());second = self.coordinator.publish(self.payload())
        restored = TurnCoordinator(self.base, clock=lambda: self.now)
        self.assertEqual(restored.for_lane('at1').snapshot()['trigger_id'], second['trigger_id'])

    def test_parallel_replies_cannot_cross_the_shared_user_limit(self):
        from hakimi.bridge import user_reply_quota
        state = self.coordinator._read()
        history = []
        for index in range(6):
            history = user_reply_quota.record(history, '42', 'old'+str(index), self.now-10)
        state[user_reply_quota.HISTORY_KEY] = history
        self.coordinator._save(state)
        self.assertTrue(self.coordinator.publish(self.payload())['accepted'])
        self.assertTrue(self.coordinator.publish(self.payload('456'))['accepted'])
        main = self.coordinator.claim_primary()
        other = self.coordinator.for_lane('at1');second = other.claim_primary()
        self.assertTrue(self.coordinator.submit_reply(main['token'], ['第七轮'], 'gemini')['accepted'])
        rejected = other.submit_reply(second['token'], ['不应成为第八轮'], 'gemini')
        self.assertFalse(rejected['accepted'])
        self.assertEqual(rejected['reason'], user_reply_quota.LIMIT_REASON)
