"""Focused offline checks for explicit historical recovery preparation."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from hakimi.memory.memory_store import MemoryStore
from hakimi.memory import impression_events,daily_events,post_reply_review,review_audit,review_recovery
from hakimi.social.social_schema import ensure_schema
from hakimi.ops.recover_reviews import plan_recovery,prepare_recovery


class ReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.base=Path(tmp.name);self.index=MemoryStore(tmp.name)
        with self.index.db() as db:
            ensure_schema(db);db.executescript(impression_events.SCHEMA)
            daily_events.initialize(db);post_reply_review.initialize(db)
        impression_events.initialize(self.base)

    def human(self,origin,at=100):
        self.index.observe({'session_key':'group:1','user_id':'101','text':'actual help',
                            'message_id':origin,'received_at':at,'self_id':'999','direct_targets':['999']})
        with self.index.db() as db:
            return dict(db.execute("SELECT * FROM records WHERE kind='chat' AND origin=?",(origin,)).fetchone())

    def test_window_preparation_uses_confirmed_sends_origins_and_first_attempt(self):
        source=self.human('human-origin');reply='reply-id'
        wid=hashlib.sha256(('group:1:'+reply+':0').encode()).hexdigest()[:24]
        state=self.base/'state'
        (state/'outbox.jsonl').write_text(json.dumps({'reply_id':reply,'session_key':'group:1','trigger_id':'turn',
            'created_at':109,'segments':['[CQ:at,qq=101] welcome','again'],'reply_target_id':'human-origin'})+'\n')
        (state/'sent_messages.jsonl').write_text(''.join(json.dumps({'session_key':'group:1','trigger_id':'turn',
            'at':110+i,'message_id':'bot-'+str(i),'text':text})+'\n' for i,text in enumerate(['welcome','again'])))
        with self.index.db() as db:
            db.execute("INSERT INTO reply_review_windows(id,session,created_at,updated_at,status,attempts) VALUES (?,'group:1',110,170,'error',4)",(wid,))
            review_audit.record(db,120,job=wid,source='window',outcome='error',error_code='REVIEW_INVALID_RELATIONSHIP_SOURCE')
        plan=plan_recovery(self.base)
        self.assertEqual([(p['kind'],p['status']) for p in plan],[('window','pending')])
        with self.index.db() as db:
            self.assertIsNone(review_recovery.load_metadata(db,'window',wid))
        self.assertEqual(len(prepare_recovery(self.base,plan,now=200,ready_at=500)),1)
        with self.index.db() as db:
            window=db.execute('SELECT * FROM reply_review_windows WHERE id=?',(wid,)).fetchone()
            self.assertEqual(json.loads(window['human_ids']),['human-origin'])
            replies=json.loads(window['replies'])
            self.assertEqual([r['receipt'] for r in replies],[reply+':0',reply+':1'])
            self.assertEqual(replies[0]['reply_to'],['human-origin'])
            self.assertEqual(replies[0]['text'],'welcome')
            self.assertEqual(window['next_attempt'],500)
            meta=review_recovery.load_metadata(db,'window',wid)
            self.assertEqual((meta['original_review_at'],meta['time_basis']),(120,'first_matching_audit'))
            review_recovery.complete_metadata(db,'window',wid,600)
            self.assertTrue(review_recovery.load_metadata(db,'window',wid)['reason'])
        self.assertEqual(prepare_recovery(self.base,plan,now=201),[])

    def test_maintenance_recovery_backdates_one_job_without_processing_new_jobs(self):
        source=self.human('old-source');fresh=self.human('fresh-source',at=300)
        with self.index.db() as db:
            db.execute("INSERT OR REPLACE INTO daily_event_jobs(source_id,created_at,status) VALUES (?,110,'pending')",(source['id'],))
            db.execute("INSERT OR REPLACE INTO daily_event_jobs(source_id,created_at,status) VALUES (?,300,'pending')",(fresh['id'],))
            review_recovery.ensure_metadata(db)
            db.execute("INSERT INTO review_recovery_jobs(kind,job,session,target,original_review_at,time_basis,status,reason) VALUES ('daily',?,'group:1','101',120,'original_job_time','pending','waiting')",(str(source['id']),))
        def runner(packets,base):
            self.assertEqual(packets,[])
            return {'items':[],'daily_events':[],'relationship_events':[{'event_id':source['id'],
                'kind':'relationship','directed_to_hakimi':True,'event_type':'practical_help',
                'evidence':'actual help','reason':'verified practical help'}]}
        result=impression_events.tick(self.base,1000,runner=runner)
        self.assertEqual(result['status'],'completed')
        with self.index.db() as db:
            self.assertEqual(db.execute('SELECT at FROM relationship_change_log WHERE event_id=?',(source['id'],)).fetchone()[0],120)
            self.assertEqual(db.execute('SELECT status FROM daily_event_jobs WHERE source_id=?',(fresh['id'],)).fetchone()[0],'pending')
            self.assertEqual(review_recovery.load_metadata(db,'daily',source['id'])['status'],'done')
            self.assertFalse(review_recovery.eligible_event(db,source['id']))


if __name__=='__main__':unittest.main()
