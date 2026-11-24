import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from hakimi.memory import daily_events, impression_events, post_reply_review
from hakimi.memory.memory_store import MemoryStore
from hakimi.social.social_schema import ensure_schema


class ReactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name); self.index = MemoryStore(self.base)
        with self.index.db() as db:
            ensure_schema(db); db.executescript(impression_events.SCHEMA)
            daily_events.initialize(db); post_reply_review.initialize(db)
            db.execute("INSERT INTO impression_scores VALUES ('groups','101',50,10,80)")
        self.index.observe({'session_key':'group:1','user_id':'101','self_id':'999','message_id':'h1',
                            'received_at':1000,'text':'猫猫今天聊得真开心','direct_targets':['999']})

    def window(self, event_type, reply, quote, reaction='positive', source='reply'):
        humans = [{'message_id':'h1','user_id':'101'}]
        post_reply_review.receipt(self.base,'group:1','reply:0',reply,'b1','turn',1010,humans,['h1'])
        post_reply_review.close(self.base,'group:1',1020)
        def runner(packet, base):
            row = packet['messages'][0]
            return {'relationship_events':[{'event_id':row['id'],'kind':'relationship','directed_to_hakimi':True,
                'event_type':event_type,'evidence':row['text'],'reaction_type':reaction,
                'reaction_source':source,'reaction_evidence':quote,'reason':'本次互动和实际反应'}]}
        with patch.object(MemoryStore,'sync',return_value={}):
            result = post_reply_review.tick(self.base,1021,runner=runner)
        self.assertEqual(result['status'], 'completed')
        with self.index.db() as db:
            row = db.execute('SELECT affinity,closeness FROM impression_scores').fetchone()
            return tuple(row)

    def test_banter_uses_actual_happy_reply(self):
        reply = '好耶，和你这样聊天真的很开心'
        self.assertEqual(self.window('banter', reply, reply), (51,11))

    def test_chat_only_increases_closeness_with_engagement_and_real_quote(self):
        reply = '继续说，我还想听你刚才提到的那个细节'
        self.assertEqual(self.window('ordinary_chat', reply, reply, reaction='engaged'), (50,11))

    def test_event_linked_self_note_is_alternative_reaction_evidence(self):
        note = '这次玩笑让我很开心，我很喜欢和他这样相处'
        self.index.remember_manual({'kind':'person','user_id':'101','key':'互动印象','note':note},
                                  session='group:1',provider='gemini',now=1015,origin_message_id='h1')
        self.assertEqual(self.window('roleplay','知道啦',note,source='note'), (51,11))

    def test_teasing_with_annoyed_reply_decreases_affinity(self):
        reply = '这么说吧，我赢在灵活，它赢在圆'
        self.assertEqual(self.window('teasing_or_bossing', reply, reply, reaction='annoyed'), (49,10))

    def test_confiding_with_engaged_reply_only_increases_closeness(self):
        reply = '快醒醒，叔叔喊你起来看报纸了'
        self.assertEqual(self.window('confiding_or_seeking_help', reply, reply, reaction='engaged'), (50,11))
