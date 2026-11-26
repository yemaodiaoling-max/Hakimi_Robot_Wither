"""Drawing authorization is tied to persisted, explicit QQ source IDs."""
import json
import unittest

from hakimi.tools.drawing_intent import allows_generation, request_id, requested


class DrawingFollowupTests(unittest.TestCase):
    def row(self, mid, text, *, user='42', session='group:91'):
        return dict(message_id=mid, text=text, user_id=user, session_key=session,
                    self_id='99', received_at=1000 + int(mid))

    def turn(self, *sources, context=(), merged=True):
        return dict(
            session_key='group:91', wake_mode='即时唤醒(被@/私聊)',
            merged_followup=merged, reply_target_message_id=sources[-1]['message_id'],
            pending_explicit_question_ids=[row['message_id'] for row in sources],
            recent_context_messages=[*sources, *context],
        )

    def test_retains_original_draw_request_and_receipt_after_restart(self):
        original = self.row('1', '帮我画一只小猫')
        detail = self.row('2', '想要暖色背景')
        turn = json.loads(json.dumps(self.turn(original, detail)))
        initial = requested(self.turn(original, merged=False))
        target = requested(turn)
        self.assertEqual(target, original)
        self.assertEqual(request_id(turn['session_key'], target), request_id(turn['session_key'], initial))
        self.assertTrue(allows_generation(turn, target))

    def test_latest_cancellation_revokes_request(self):
        original = self.row('1', '帮我画一只小猫')
        detail = self.row('2', '想要暖色背景')
        for text in ('先别画了', '这张图不画了', '取消吧'):
            self.assertIsNone(requested(self.turn(original, detail, self.row('3', text))))
        cancel = self.row('3', '先别画了')
        self.assertIsNone(requested(self.turn(original, cancel, self.row('4', '背景偏暖色'))))
        renewed = self.row('4', '帮我画一条小狗')
        self.assertEqual(requested(self.turn(original, cancel, renewed)), renewed)

    def test_merged_chain_cannot_borrow_unlisted_context_authority(self):
        old = self.row('1', '帮我画一只小猫')
        unrelated = self.row('2', '真好看')
        followup = self.row('3', '按这个来')
        self.assertIsNone(requested(self.turn(unrelated, followup, context=(old,))))

    def test_rejects_cross_author_cross_session_and_ambiguous_source(self):
        detail = self.row('2', '想要暖色背景')
        for foreign in (self.row('1', '帮我画一只小猫', user='43'),
                        self.row('1', '帮我画一只小猫', session='group:90')):
            self.assertIsNone(requested(self.turn(foreign, detail)))
        original = self.row('1', '帮我画一只小猫')
        self.assertIsNone(requested(self.turn(original, detail, context=(original,))))
        self.assertIsNone(requested(self.turn(original, original)))

    def test_does_not_use_arbitrary_anchors_for_normal_turns(self):
        original = self.row('1', '帮我画一只小猫')
        detail = self.row('2', '想要暖色背景')
        self.assertIsNone(requested(self.turn(original, detail, merged=False)))
        malformed = self.turn(original, detail)
        malformed['reply_target_message_id'] = original['message_id']
        self.assertIsNone(requested(malformed))

    def test_followup_offline_request_blocks_image_service(self):
        original = self.row('1', '帮我画一只小猫')
        turn = self.turn(original, self.row('2', '不要联网'))
        self.assertEqual(requested(turn), original)
        self.assertFalse(allows_generation(turn, original))


if __name__ == '__main__':
    unittest.main()
