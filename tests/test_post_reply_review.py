"""Focused regression cases for ended, actually sent reply review windows."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from hakimi.memory import daily_events
from hakimi.curation import impression_events, post_reply_review as review
from hakimi.memory.memory_store import MemoryStore
from hakimi.social.social_schema import ensure_schema
from hakimi.bridge.conversation_scheduler import ConversationScheduler


class PostReplyReviewTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.index=MemoryStore(temporary.name);self.base=temporary.name
        with self.index.db() as db:
            ensure_schema(db);db.executescript(impression_events.SCHEMA)
            daily_events.initialize(db);review.initialize(db)
        self.sync=patch.object(MemoryStore,'sync',return_value={})
        self.sync_mock=self.sync.start();self.addCleanup(self.sync.stop)

    def human(self, mid='human', at=1000, targets=(), reply=''):
        self.index.observe({'session_key':'group:1','user_id':'101','text':'仍然愿意陪你说说话\n也听听你的想法 '+mid,
                            'message_id':mid,'received_at':at,'self_id':'999',
                            'direct_targets':list(targets),'reply_to':reply})
        return {'message_id':mid,'user_id':'101','received_at':at}

    def send(self, receipt='r1:0', text='我在听呢', at=1010, humans=(), targets=('human',)):
        review.receipt(self.base,'group:1',receipt,text,'bot-'+receipt,'trigger',at,humans,targets)

    def verdict(self, packet, base):
        return {'relationship_events':[{'event_id':packet['messages'][0]['id'],'kind':'relationship',
            'directed_to_hakimi':True,'event_type':'emotional_support','evidence':packet['messages'][0]['text'],
            'reason':'对方愿意陪伴，实际回复接纳并倾听'}]}

    def test_bubbles_and_continuation_wait_for_real_end(self):
        human=self.human();self.send(humans=[human])
        self.send('r2:0','还在呢',1020,[human])
        review.close(self.base,'group:1',1021,delivering=True)
        self.send('r2:1','慢慢说',1022,[human])
        self.assertEqual(review.tick(self.base,1023,runner=self.verdict)['status'],'idle')
        review.delivery_finished(self.base,'group:1',1024,listening=False)
        with self.index.db() as db:
            windows=db.execute('SELECT * FROM reply_review_windows').fetchall()
            self.assertEqual(len(windows),1)
            self.assertEqual([r['text'] for r in json.loads(windows[0]['replies'])],['我在听呢','还在呢','慢慢说'])
        self.assertEqual(review.tick(self.base,1025,runner=self.verdict)['status'],'completed')
        with self.index.db() as db:
            self.assertEqual(db.execute('SELECT closeness FROM impression_scores').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT replies FROM reply_review_windows').fetchone()[0],'[]')

    def test_actual_reply_context_survives_apply_and_shares_effect_dedup(self):
        human=self.human(reply='bot-r1:0');self.send(at=1300,humans=[human])
        review.close(self.base,'group:1',1350)
        result=review.tick(self.base,1351,runner=self.verdict)
        self.assertEqual(result['status'],'completed')
        with self.index.db() as db:
            row=db.execute("SELECT * FROM records WHERE origin='human'").fetchone()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM impression_effects').fetchone()[0],1)
            item={'event_id':row['id'],'kind':'relationship','directed_to_hakimi':True,
                  'event_type':'emotional_support','evidence':row['text'],'reason':'same event'}
            job={'session':'group:1','target':'101','record_id':None,'original_id':None,'noted_at':1000}
            impression_events.apply(db,job,None,item,1352,direction={'author':'101','self_id':'999','session':'group:1','mentions':['999']})
            self.assertEqual(db.execute('SELECT COUNT(*) FROM impression_effects').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT outcome FROM relationship_review_audit ORDER BY id DESC LIMIT 1').fetchone()[0],'duplicate')

    def test_explicit_other_addressee_overrides_actual_self_context(self):
        human=self.human(targets=('102',));self.send(humans=[human])
        review.close(self.base,'group:1',1020)
        review.tick(self.base,1021,runner=self.verdict)
        with self.index.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM impression_effects').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT outcome FROM relationship_review_audit').fetchone()[0],'other_addressee')

    def test_retry_is_short_and_does_not_block_another_ready_window(self):
        human=self.human();self.send(humans=[human]);review.close(self.base,'group:1',1020)
        def fail(packet,base):raise RuntimeError('provider secret must not persist')
        result=review.tick(self.base,1021,runner=fail)
        self.assertEqual(result['reason'],'REVIEW_FAILED')
        self.assertEqual(review.next_due(self.base,1022),1051)
        self.send('other:0','下一段',1023,[human]);review.close(self.base,'group:1',1024)
        self.assertEqual(review.tick(self.base,1025,runner=self.verdict)['status'],'completed')
        self.assertEqual(review.tick(self.base,1050,runner=self.verdict)['status'],'idle')
        self.assertEqual(review.tick(self.base,1051,runner=self.verdict)['status'],'completed')

    def test_recovery_keeps_live_window_and_seals_only_finished_one(self):
        human=self.human();self.send(humans=[human])
        scheduler=ConversationScheduler();scheduler.group('group:1').listen_round=1
        review.recover(self.base,scheduler,1100)
        self.assertEqual(review.tick(self.base,1101,runner=self.verdict)['status'],'idle')
        scheduler.group('group:1').listen_round=0
        checkpoint=Path(self.base)/'state/delivery_state.json';checkpoint.parent.mkdir(exist_ok=True)
        checkpoint.write_text(json.dumps({'pending':{'session_key':'group:1'}}),encoding='utf-8')
        review.recover(self.base,scheduler,1102)
        self.assertEqual(review.tick(self.base,1103,runner=self.verdict)['status'],'idle')
        checkpoint.write_text(json.dumps({'pending':None}),encoding='utf-8')
        review.recover(self.base,scheduler,1104)
        self.assertEqual(review.tick(self.base,1105,runner=self.verdict)['status'],'completed')

    def test_idle_does_not_sync_and_dead_retry_does_not_spin(self):
        self.assertEqual(review.tick(self.base,1000)['status'],'idle')
        self.sync_mock.assert_not_called()
        human=self.human()
        with self.index.db() as db:
            daily_events.discover(db,1001)
            row=db.execute("SELECT id FROM records WHERE origin='human'").fetchone()
            db.execute("UPDATE daily_event_jobs SET attempts=1,next_attempt=900")
            db.execute("UPDATE records SET text='' WHERE id=?",(row[0],))
        self.assertIsNone(review.next_due(self.base,1100))

    def test_full_live_queue_keeps_cursor_before_unqueued_candidate(self):
        for number in range(daily_events.MAX_PENDING+1):
            self.human(str(number),1000+number,targets=('999',))
        with self.index.db() as db:
            daily_events.discover(db,1100)
            cursor=db.execute('SELECT offset FROM cursors WHERE name=?',(daily_events.CURSOR,)).fetchone()[0]
            missing=db.execute("SELECT id FROM records WHERE origin=?",(str(daily_events.MAX_PENDING),)).fetchone()[0]
            self.assertLess(cursor,missing)
            db.execute("UPDATE daily_event_jobs SET status='done'")
            daily_events.discover(db,1101)
            self.assertEqual(db.execute('SELECT status FROM daily_event_jobs WHERE source_id=?',(missing,)).fetchone()[0],'pending')

    def test_prune_slots_do_not_displace_live_and_retry_ignores_success_throttle(self):
        for number in range(6):self.human('old'+str(number),100+number,targets=('999',))
        with self.index.db() as db:
            old_ids=[r[0] for r in db.execute('SELECT id FROM records')]
            daily_events.protect_before_prune(db,old_ids,10000)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM daily_event_jobs WHERE priority=1").fetchone()[0],4)
        self.human('fresh',10001,targets=('999',))
        with self.index.db() as db:
            packets=daily_events.prepare(db,10002,with_impressions=True)
            fresh=next(p for p in packets if p['message_id']=='fresh')
            self.assertEqual(fresh['source'],'daily')
            db.execute("INSERT OR REPLACE INTO memory_meta VALUES ('daily_events_last_attempt','10001')")
            daily_events.failed(db,[fresh],10002)
            self.assertTrue(any(p['id']==fresh['id'] for p in daily_events.prepare(db,10032)))


if __name__=='__main__':unittest.main()
