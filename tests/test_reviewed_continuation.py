"""Two offline checks; no models or QQ sends."""
import unittest
from unittest.mock import patch
from hakimi.bridge.conversation_scheduler import ConversationScheduler
from tests.test_conversation_scheduler import config

class ReviewedContinuationTests(unittest.TestCase):
    def prepare(self):
        s=ConversationScheduler(); key='group:100'
        s.sent_reply(key,'聊聊猫咪',1000,'123');s.delivery_finished(key,1000)
        row={'received_at':1010,'user_id':'22','message_id':'1','text':'还有个问题，你怎么看这个解释'}
        return s,key,row

    def test_reviewed_followup_survives_moderate_participation_pressure(self):
        s,key,row=self.prepare()
        with patch.object(s,'_pressures',return_value=(2,2,2,4)):
            plain=s.score_window_messages(key,config(),1045,[row])
            reviewed=s.score_window_messages(key,config(),1045,[row],reviewed_message_ids=('1',))
        self.assertFalse(plain.ready);self.assertTrue(reviewed.ready)
        self.assertGreaterEqual(reviewed.relevance,.65)
        self.assertAlmostEqual(reviewed.pressure,plain.pressure*.35)

    def test_unreviewed_images_and_empty_input_get_no_boost(self):
        s,key,row=self.prepare()
        ordinary=s.score_window_messages(key,config(),1045,[row])
        other=s.score_window_messages(key,config(),1045,[row],reviewed_message_ids=('different',))
        self.assertEqual(ordinary,other)
        self.assertFalse(s.score_window_messages(key,config(),1045,[],reviewed_message_ids=('1',)).ready)
