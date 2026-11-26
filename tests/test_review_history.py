"""Focused panel/history cleanup checks without models, network or browser installs."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from hakimi.console.payloads_review_history import review_history_payload
from hakimi.console.console_db import ApiError
from hakimi.memory import relationship_history, relationship_events
from hakimi.curation import impression_events
from hakimi.memory.memory_store import MemoryStore
from hakimi.social.social_behavior import behavior_for
from hakimi.social.social_schema import ensure_schema


class ReviewHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.base=temporary.name;self.index=MemoryStore(self.base);self.now=time.time()
        with self.index.db() as db:
            ensure_schema(db);db.executescript(impression_events.SCHEMA);relationship_history.initialize(db)

    def event(self, origin='human', session='group:1', at=None):
        at=self.now if at is None else at
        self.index.observe({'session_key':session,'user_id':'101','nickname':'某位群友','text':'I completed the requested reference lookup and sent the sources',
                            'message_id':origin,'received_at':at,'self_id':'999','direct_targets':['999']})
        with self.index.db() as db:
            row=db.execute('SELECT * FROM records WHERE origin=?',(origin,)).fetchone()
            return dict(row)

    def apply(self,db,row,now):
        job={'session':row['session'],'target':'101','record_id':None,'original_id':None,'noted_at':row['at'],'review_source':'window'}
        item={'event_id':row['id'],'kind':'relationship','directed_to_hakimi':True,
              'event_type':'practical_help','evidence':row['text'],'reason':'双方明确接纳并愿意继续陪伴'}
        return impression_events.apply(db,job,None,item,now)

    def test_api_filters_pages_and_legacy_does_not_invent_snapshots(self):
        public=self.event();private=self.event('private','private:101',self.now+500)
        with self.index.db() as db:
            self.apply(db,public,self.now);self.apply(db,private,self.now+500)
            # Old ledger entry without detailed snapshots remains explicitly unknown.
            db.execute("INSERT INTO impression_effects VALUES ('groups','102',999,1,0,NULL,'positive','old reason',?)",(self.now,))
        data=review_history_payload(self.base,{'page_size':'1'})
        self.assertEqual(data['total'],2);self.assertTrue(data['has_more'])
        filtered=review_history_payload(self.base,{'person':'101','group':'group:1','source':'window'})
        self.assertEqual(len(filtered['items']),1)
        self.assertEqual((filtered['items'][0]['affinity_after'],filtered['items'][0]['affinity_delta']),(52,2))
        self.assertNotIn('text',filtered['items'][0]);self.assertLessEqual(len(filtered['items'][0]['reason']),160)
        legacy=review_history_payload(self.base,{'person':'102'})['items'][0]
        self.assertTrue(legacy['legacy']);self.assertIsNone(legacy['affinity_before']);self.assertIsNone(legacy['affinity_after'])
        self.assertEqual(review_history_payload(self.base,{'group':'private:101'})['total'],1)
        with self.assertRaises(ApiError):review_history_payload(self.base,{'group':"group:1' OR 1=1"})
        with self.assertRaises(ApiError):review_history_payload(self.base,{'since':'nan'})

    def test_retention_does_not_clear_score_profile_or_dedup_ledger(self):
        row=self.event()
        with self.index.db() as db:self.apply(db,row,self.now)
        before=behavior_for(self.base,'101','group:1',now=self.now,config={})
        self.assertEqual(before['affinity'],52)
        with self.index.db() as db:
            # Raw evidence removal is independent from score continuity.
            db.execute('DELETE FROM records WHERE id=?',(row['id'],))
            relationship_history.maintain(db,self.now+31*86400)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM relationship_change_log').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM relationship_change_daily').fetchone()[0],1)
            effect=db.execute('SELECT * FROM impression_effects').fetchone()
            self.assertEqual(effect['reason'],'')
            self.assertEqual(db.execute('SELECT affinity FROM impression_scores').fetchone()[0],52)
            item={'valence':'positive','closeness':'closer'}
            self.assertTrue(relationship_events.duplicate(db,'groups','101',row,item))
        after=behavior_for(self.base,'101','group:1',now=self.now+31*86400,config={})
        self.assertEqual((after['affinity'],after['closeness']),(52,1))

    def test_daily_rollup_respects_real_order_and_bounded_cleanup(self):
        row=self.event()
        job={'session':'group:1','target':'101','record_id':None,'original_id':None,'review_source':'window'}
        item={'event_id':row['id'],'reason':'short context'}
        # New York is still Oct 31 while UTC is Nov 1. Insert out of time order.
        stamp=1793493000
        with self.index.db() as db:
            relationship_history.record(db,job,item,{'affinity':80,'closeness':1},{'affinity':81,'closeness':2},stamp+100)
            relationship_history.record(db,job,item,{'affinity':50,'closeness':0},{'affinity':51,'closeness':1},stamp)
            roll=db.execute('SELECT * FROM relationship_change_daily').fetchone()
            self.assertEqual((roll['affinity_before'],roll['affinity_after'],roll['affinity_delta']),(50,81,2))
            self.assertEqual(roll['at'],stamp+100)
            with patch.object(relationship_history,'DETAIL_LIMIT',1):
                relationship_history.maintain(db,stamp+101)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM relationship_change_log').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM relationship_change_daily').fetchone()[0],1)


    def test_global_processing_health_handles_missing_tables_and_ignores_filters(self):
        empty=review_history_payload(self.base,{})['health']
        self.assertEqual(empty['summary']['audit_total'],0)
        self.assertEqual(empty['summary']['pending_windows'],0)
        self.assertIsNone(empty['summary']['last_attempt_at'])
        from hakimi.memory import review_audit
        with self.index.db() as db:
            review_audit.initialize(db)
            for outcome,code in (('no_change',''),('not_selected',''),
                                 ('error','REVIEW_INVALID_RELATIONSHIP_SOURCE'),
                                 ('error','private provider output')):
                review_audit.record(db,self.now-10,outcome=outcome,error_code=code,
                                    reason='private chat must stay private')
            review_audit.record(db,self.now-7200,outcome='error',error_code='REVIEW_FAILED')
            db.execute('CREATE TABLE reply_review_windows(id TEXT,status TEXT,updated_at REAL,attempts INTEGER)')
            db.executemany('INSERT INTO reply_review_windows VALUES (?,?,?,?)',
                           [('ready','ready',self.now-5,0),('retry','retry',self.now-5,1),('done','done',self.now-5,0)])
        health=review_history_payload(self.base,{'person':'999','group':'group:999','since':str(self.now+100)})['health']
        self.assertEqual(health['scope'],'global')
        self.assertEqual(health['summary'],{'audit_total':4,'no_change_or_unscored':2,'errors':2,
                         'last_attempt_at':self.now-5,'pending_windows':2,'unresolved_jobs':0,'recovered_jobs':0,'source_unavailable_jobs':0,'pending_recovery_jobs':0})
        self.assertEqual({r['code'] for r in health['error_codes']},
                         {'REVIEW_INVALID_RELATIONSHIP_SOURCE','REVIEW_ERROR'})
        self.assertNotIn('private',json.dumps(health))
        self.assertEqual(health,review_history_payload(self.base,{})['health'])


if __name__=='__main__':unittest.main()
