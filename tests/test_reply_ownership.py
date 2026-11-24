import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from hakimi.bridge.turn_coordinator import TurnCoordinator


class ReplyOwnership(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.now = 1000
        self.c = TurnCoordinator(self.base, clock=lambda:self.now)

    def payload(self, mid, *, mode='即时唤醒(被@/私聊)', context=(), session='group:123'):
        return dict(session_key=session, wake_mode=mode, created_at=self.now,
                    reply_target_message_id=str(mid),
                    recent_context_messages=[dict(session_key=session, user_id='8', message_id=str(i),
                        text='test',received_at=self.now) for i in (*context,mid)])

    def start(self, payload):
        result=self.c.publish(payload)
        self.assertTrue(result['accepted'],result)
        owner=self.c.for_lane(result['conversation_lane'])
        return owner,owner.claim_primary()

    def reply(self, owner, claim):
        return owner.submit_reply(claim['token'],['first bubble','second bubble'],'gemini')

    def test_active_same_input_has_one_owner_across_lanes(self):
        p=self.payload(10)
        owner,claim=self.start(p)
        self.assertEqual(self.c.publish(p)['reason'],'busy')
        self.assertTrue(self.reply(owner,claim)['accepted'])
        self.assertEqual(self.c.publish(p)['reason'],'source_already_answered')
        self.assertEqual(len((self.base/'state/outbox.jsonl').read_text().splitlines()),1)

    def test_real_overlap_new_at_closes_old_automatic_only_after_commit(self):
        p=self.payload(10,mode='被@后监听窗口')
        old,claim=self.start(p)
        self.now+=5
        new,latest=self.start(self.payload(11,context=(10,)))
        self.assertTrue(old.check_token(claim['token'],'gemini'))
        self.assertTrue(self.reply(new,latest)['accepted'])
        self.assertTrue(old.was_superseded(claim['trigger_id']))
        self.assertFalse(self.reply(old,claim)['accepted'])
        self.assertEqual(old.snapshot()['outcome'],'superseded')

    def test_failed_new_at_does_not_cancel_older_turn(self):
        old,claim=self.start(self.payload(10,mode='相关话题续聊'))
        self.now+=5
        new,latest=self.start(self.payload(11,context=(10,)))
        new.complete(latest['token'],'fallback_error')
        self.assertTrue(self.reply(old,claim)['accepted'])

    def test_interrupted_new_reply_commit_is_recovered_before_older_submission(self):
        old,claim=self.start(self.payload(10,mode='被@后监听窗口'))
        self.now+=5
        new,latest=self.start(self.payload(11,context=(10,)))
        with patch.object(new,'_reply_committed',side_effect=OSError('simulated interruption')):
            with self.assertRaises(OSError):self.reply(new,latest)
        self.assertFalse(self.reply(old,claim)['accepted'])
        self.assertTrue(old.was_superseded(claim['trigger_id']))
        self.assertEqual(len((self.base/'state/outbox.jsonl').read_text().splitlines()),1)

    def test_distinct_direct_questions_and_foreign_groups_remain_independent(self):
        for session in ('group:123','group:456'):
            with self.subTest(session=session):
                old,claim=self.start(self.payload(20))
                self.now+=5
                new,latest=self.start(self.payload(21,context=(20,),session=session))
                self.assertTrue(self.reply(new,latest)['accepted'])
                self.assertTrue(self.reply(old,claim)['accepted'])
                self.now+=10
                # Use new inbound IDs next iteration.
                self.setUp()

    def test_restart_and_lane_reuse_keep_source_identity_and_operator_replay(self):
        owner,claim=self.start(self.payload(10))
        self.reply(owner,claim)
        self.now+=5
        owner,claim=self.start(self.payload(11))
        owner.complete(claim['token'])
        c=TurnCoordinator(self.base,clock=lambda:self.now)
        self.assertEqual(c.publish(self.payload(10))['reason'],'source_already_answered')
        replay=dict(self.payload(10),trigger_id='operator-replay-10')
        self.assertTrue(c.publish(replay)['accepted'])

    def test_silent_turn_can_retry_and_new_messages_are_not_text_deduplicated(self):
        owner,claim=self.start(self.payload(10))
        owner.complete(claim['token'])
        owner,claim=self.start(self.payload(10))
        self.assertTrue(self.reply(owner,claim)['accepted'])
        owner,claim=self.start(self.payload(11))
        self.assertTrue(self.reply(owner,claim)['accepted'])
