"""Relationship scoring keeps facts, direction, and durable scores separate."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hakimi.memory import daily_events, relationship_events
from hakimi.curation import impression_events
from hakimi.memory.memory_store import MemoryStore
from hakimi.social.social_schema import ensure_schema


class RelationshipEventTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.index = MemoryStore(tmp.name)
        with self.index.db() as db:
            ensure_schema(db);db.executescript(impression_events.SCHEMA)
            daily_events.initialize(db)

    def message(self, origin, at=1000, author='101', targets=('999',), text='hello', reply=''):
        self.index.observe({'session_key':'group:1','user_id':author,'text':text,
                            'message_id':origin,'received_at':at,'direct_targets':list(targets),
                            'reply_to':reply,'self_id':'999'})
        with self.index.db() as db:
            return dict(db.execute("SELECT * FROM records WHERE kind='chat' AND origin=?",(origin,)).fetchone())

    def apply(self, db, row, valence='positive', closeness='unchanged'):
        job={'session':row['session'],'target':'101','record_id':None,'original_id':None,'noted_at':row['at']}
        event_type = ('practical_help' if valence == 'positive' else 'teasing_or_bossing' if valence == 'negative'
                      else 'sustained_companionship' if closeness == 'closer' else 'ordinary_chat')
        item={'event_id':row['id'],'kind':'relationship','directed_to_hakimi':True,
              'event_type':event_type,'evidence':row['text'],'reason':'observed relationship event'}
        impression_events.apply(db,job,None,item,row['at']+10)

    def test_neutral_facts_and_closeness_are_independent(self):
        fact=self.message('fact');companion=self.message('companion',at=1400)
        with self.index.db() as db:
            db.execute("INSERT INTO impression_scores VALUES ('groups','101',64,12,80)")
            self.apply(db,fact,'neutral')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM impression_effects').fetchone()[0],0)
            self.apply(db,companion,'neutral','closer')
            score=db.execute('SELECT * FROM impression_scores').fetchone()
            self.assertEqual((score['affinity'],score['closeness']),(64,14))
            projection=db.execute('SELECT * FROM social_assessments').fetchone()
            self.assertEqual((projection['provider'],projection['affinity'],projection['closeness']),('dsh',64,14))
            self.assertIsNone(projection['original_record_id'])

    def test_old_effects_and_both_entry_points_cannot_rescore_episode(self):
        one=self.message('one');two=self.message('two',at=1050);later=self.message('later',at=1600)
        opposite=self.message('opposite',at=1650)
        with self.index.db() as db:
            self.apply(db,one,'negative')
            self.apply(db,one,'neutral')
            self.apply(db,one,'positive')
            self.apply(db,two,'negative')
            self.assertEqual(db.execute('SELECT affinity FROM impression_scores').fetchone()[0],49)
            self.apply(db,later,'negative')
            self.assertEqual(db.execute('SELECT affinity FROM impression_scores').fetchone()[0],47)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM impression_effects').fetchone()[0],2)
            self.apply(db,opposite,'positive')
            self.assertEqual(db.execute('SELECT affinity FROM impression_scores').fetchone()[0],49)

    def test_direction_and_implicit_context(self):
        self.index.dialogue_signature('group:1','trigger','I feel tired today','I am here with you and happy to listen to your stories',1000)
        implicit=self.message('implicit',at=1005,targets=(),text='Still here with you')
        other=self.message('other',at=1010,targets=('102',),text='You are annoying')
        with self.index.db() as db:
            context=relationship_events.packet(db,implicit)
            self.assertEqual(context['context'][0]['kind'],'dialogue')
            self.assertIn('boundary is unresolved',context['context'][0]['label'])
            self.assertTrue(relationship_events.directed(context))
            self.assertFalse(relationship_events.directed(relationship_events.packet(db,other)))
            self.apply(db,other,'negative')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM impression_effects').fetchone()[0],0)
        alone=self.message('alone',at=2000,targets=())
        with self.index.db() as db:
            self.assertFalse(relationship_events.directed(relationship_events.packet(db,alone)))

    def test_short_directed_turns_use_existing_throttled_queue(self):
        self.message('short',text='hi')
        with self.index.db() as db:
            self.assertEqual(daily_events.prepare(db,1001),[])
            packets=daily_events.prepare(db,3000)
            self.assertEqual(len(packets),1)
            self.assertEqual(packets[0]['text'],'hi')

    def test_note_facts_replace_prior_effect_and_share_daily_deduplication(self):
        source=self.message('source')
        self.index.remember_manual({'kind':'person','user_id':'101','key':'facts','note':'likes music'},
                                   session='group:1',provider='gemini',now=1100,origin_message_id='source')
        with self.index.db() as db:
            self.apply(db,source)
            original=db.execute("SELECT * FROM memory_originals WHERE kind='person'").fetchone()
            job={'session':'group:1','target':'101','record_id':original['record_id'],
                 'original_id':original['id'],'noted_at':1100}
            item={'event_id':source['id'],'kind':'fact','directed_to_hakimi':True,
                  'event_type':'personal_fact','evidence':source['text'],'reason':'facts alone'}
            impression_events.apply(db,job,None,item,1100)
            self.assertEqual(db.execute('SELECT affinity FROM impression_scores').fetchone()[0],50)
            projection=db.execute('SELECT * FROM social_assessments WHERE original_record_id=?',
                                  (original['record_id'],)).fetchone()
            self.assertEqual(projection['affinity'],50)
            impression_events.apply(db,job,None,dict(item,kind='relationship'),1100)
            self.assertEqual(db.execute('SELECT affinity FROM impression_scores').fetchone()[0],50)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM impression_effects').fetchone()[0],1)

    def note_job(self, source, now, label):
        self.index.remember_manual({'kind':'person','user_id':'101','key':label,'note':'a reliable observation '+label},
                                   session='group:1',provider='gemini',now=now,origin_message_id=source['origin'])
        with self.index.db() as db:
            row=db.execute("SELECT * FROM memory_originals WHERE kind='person' ORDER BY id DESC LIMIT 1").fetchone()
            return {'session':'group:1','target':'101','record_id':row['record_id'],'original_id':row['id'],'noted_at':now}

    def test_later_note_replaces_only_same_event_and_replayed_notes_are_inert(self):
        one=self.message('one');other=self.message('other',at=1400)
        with self.index.db() as db:
            db.execute("INSERT INTO impression_scores VALUES ('groups','101',50,10,80)")
            self.apply(db,one);self.apply(db,other)
            self.assertEqual(db.execute('SELECT affinity FROM impression_scores').fetchone()[0],54)
        note=self.note_job(one,1600,'first review')
        item={'event_id':one['id'],'kind':'relationship','directed_to_hakimi':True,
              'event_type':'targeted_insult','evidence':one['text'],'reason':'later note re-evaluates the same event'}
        with self.index.db() as db:
            self.assertEqual(impression_events.apply(db,note,None,item,1610),'revised')
            self.assertEqual(db.execute('SELECT affinity FROM impression_scores').fetchone()[0],50)
            self.assertEqual(db.execute('SELECT affinity_delta FROM impression_effects WHERE event_id=?',(other['id'],)).fetchone()[0],2)
            self.assertEqual(impression_events.apply(db,note,None,item,1611),'duplicate')
            self.apply(db,one,'positive')
            self.assertEqual(db.execute('SELECT affinity FROM impression_scores').fetchone()[0],50)
            revision=db.execute('SELECT * FROM relationship_change_log WHERE revision=1').fetchone()
            self.assertEqual((revision['affinity_before'],revision['affinity_after'],revision['affinity_delta']),(54,50,-4))
        neutral=self.note_job(one,1700,'neutral review')
        with self.index.db() as db:
            impression_events.apply(db,neutral,None,dict(item,event_type='ordinary_chat'),1710)
            self.assertEqual(db.execute('SELECT affinity FROM impression_scores').fetchone()[0],52)
            self.assertEqual(db.execute('SELECT affinity_delta FROM impression_effects WHERE event_id=?',(one['id'],)).fetchone()[0],0)
            self.assertEqual(impression_events.apply(db,note,None,item,1711),'duplicate')
            self.assertEqual(db.execute('SELECT affinity FROM impression_scores').fetchone()[0],52)

    def test_neutral_note_priority_and_missing_evidence_keep_scores(self):
        source=self.message('fresh');note=self.note_job(source,1100,'neutral original')
        unscored=self.message('fact',at=1500)
        item={'event_id':source['id'],'kind':'relationship','directed_to_hakimi':True,
              'event_type':'ordinary_chat','evidence':source['text'],'reason':'no change after contextual note'}
        with self.index.db() as db:
            db.execute("INSERT INTO impression_scores VALUES ('groups','101',100,12,80)")
            impression_events.apply(db,note,None,item,1110)
            self.apply(db,source,'positive')
            self.assertEqual(db.execute('SELECT affinity FROM impression_scores').fetchone()[0],100)
            self.assertEqual(db.execute('SELECT original_id FROM impression_effects').fetchone()[0],note['original_id'])
            fact=dict(item,kind='fact',directed_to_hakimi=False)
            ordinary={'session':'group:1','target':'101','record_id':None,'original_id':None,'noted_at':1500}
            self.assertEqual(impression_events.apply(db,ordinary,None,dict(fact,event_id=unscored['id']),1510),'no_change')
        later=self.note_job(source,1600,'positive later')
        with self.index.db() as db:
            impression_events.apply(db,later,None,dict(item,event_type='threat'),1610)
            self.assertEqual(db.execute('SELECT affinity FROM impression_scores').fetchone()[0],94)
        unreadable=self.note_job(source,1700,'source unavailable')
        with self.index.db() as db:
            db.execute("UPDATE records SET text='' WHERE id=?",(source['id'],))
            self.assertEqual(impression_events.apply(db,unreadable,None,item,1710),'skip')
            self.assertEqual(db.execute('SELECT affinity FROM impression_scores').fetchone()[0],94)
            self.assertEqual(db.execute('SELECT affinity_delta FROM impression_effects').fetchone()[0],-6)


    def test_roleplay_ignores_old_score_controls_in_apply(self):
        source=self.message('roleplay',text='a fictional snack for you')
        with self.index.db() as db:
            db.execute("INSERT INTO impression_scores VALUES ('groups','101',85,85,80)")
            job={'session':source['session'],'target':'101','record_id':None,
                 'original_id':None,'noted_at':source['at']}
            item={'event_id':source['id'],'kind':'relationship','directed_to_hakimi':True,
                  'event_type':'roleplay','evidence':source['text'],'reason':'fictional roleplay',
                  'valence':'positive','intensity':'strong','closeness':'closer',
                  'host_changes':(99,99),'host_impact':'major'}
            impression_events.apply(db,job,None,item,source['at']+10)
            score=db.execute('SELECT affinity,closeness FROM impression_scores').fetchone()
            self.assertEqual((score['affinity'],score['closeness']),(85,85))

    def test_ordinary_help_above_eighty_stays_unchanged_in_apply(self):
        source=self.message('ordinary_help',text='here is the practical help')
        with self.index.db() as db:
            db.execute("INSERT INTO impression_scores VALUES ('groups','101',85,85,80)")
            self.apply(db,source,'positive')
            score=db.execute('SELECT affinity,closeness FROM impression_scores').fetchone()
            self.assertEqual((score['affinity'],score['closeness']),(85,85))

    def test_severe_negative_and_more_negative_events_bypass_duplicate(self):
        one = self.message('one_tease', at=1000, text='tease')
        more_neg = self.message('two_insult', at=1050, text='insult')
        spam = self.message('three_spam', at=1080, text='spam')
        with self.index.db() as db:
            # 1. First event: teasing_or_bossing (-1)
            item1 = {'event_id': one['id'], 'kind': 'relationship', 'directed_to_hakimi': True,
                     'event_type': 'teasing_or_bossing', 'evidence': 'tease', 'reason': 'light tease'}
            job1 = {'session': one['session'], 'target': '101', 'record_id': None, 'original_id': None, 'noted_at': 1000}
            self.assertEqual(impression_events.apply(db, job1, None, item1, 1010), 'applied')
            self.assertEqual(db.execute('SELECT affinity FROM impression_scores').fetchone()[0], 49)

            # 2. Second event at 1050: targeted_insult (-2).
            # Strictly more negative than -1 AND in severe types -> bypasses duplicate!
            item2 = {'event_id': more_neg['id'], 'kind': 'relationship', 'directed_to_hakimi': True,
                     'event_type': 'targeted_insult', 'evidence': 'insult', 'reason': 'targeted insult'}
            job2 = {'session': more_neg['session'], 'target': '101', 'record_id': None, 'original_id': None, 'noted_at': 1050}
            self.assertEqual(impression_events.apply(db, job2, None, item2, 1060), 'applied')
            self.assertEqual(db.execute('SELECT affinity FROM impression_scores').fetchone()[0], 47)

            # 3. Third event at 1080: pestering_or_spam (-2).
            # In severe types -> bypasses duplicate!
            item3 = {'event_id': spam['id'], 'kind': 'relationship', 'directed_to_hakimi': True,
                     'event_type': 'pestering_or_spam', 'evidence': 'spam', 'reason': 'spamming'}
            job3 = {'session': spam['session'], 'target': '101', 'record_id': None, 'original_id': None, 'noted_at': 1080}
            self.assertEqual(impression_events.apply(db, job3, None, item3, 1090), 'applied')
            self.assertEqual(db.execute('SELECT affinity FROM impression_scores').fetchone()[0], 45)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM impression_effects').fetchone()[0], 3)


if __name__=='__main__':unittest.main()
