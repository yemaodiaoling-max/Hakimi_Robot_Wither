import unittest
import time
from unittest.mock import patch

import test_post_reply_review as fixtures
from hakimi.memory import review_backfill, review_recovery
from hakimi.memory.memory_store import MemoryStore
from hakimi.curation import impression_events, review_runtime
from hakimi.curation import post_reply_review as review
from hakimi.console.payloads_review_history import review_history_payload


class BackfillTests(unittest.TestCase):
    def test_fresh_daily_review_grants_ten_after_reopen_and_skips_settled(self):
        case=fixtures.PostReplyReviewTests()
        case.setUp();self.addCleanup(case.doCleanups)
        for i in range(13):
            case.human(f'old-{i}',at=100+i,targets=('999',))
        for i in range(2):
            case.human(f'fresh-{i}',at=900+i,targets=('999',))
        with case.index.db() as db:
            db.execute("""INSERT OR REPLACE INTO daily_event_jobs(source_id,created_at,status)
                SELECT id,at+1,CASE WHEN origin LIKE 'old-%' THEN 'error' ELSE 'pending' END
                FROM records WHERE kind='chat'""")
            settled=db.execute("SELECT id FROM records WHERE origin='old-0'").fetchone()[0]
            db.execute("INSERT INTO impression_effects VALUES ('groups','101',?,1,0,NULL,'positive','settled',151)",(settled,))
            db.execute("INSERT INTO daily_event_jobs(source_id,created_at,status) VALUES (90000,1,'expired')")
        empty={'items':[],'daily_events':[],'relationship_events':[]}
        with patch.object(review_runtime,'sol_backfill_enabled',return_value=True), \
             patch.object(review_runtime,'classify_sol',return_value=empty) as sol:
            result=impression_events.tick(case.base,1001,runner=lambda *_:empty)
            self.assertEqual(result['status'],'completed')
            now=time.time()
            first=review_backfill._claim(case.index,now)
            self.assertIsNone(review_backfill._claim(case.index,now+1))
            reopened=MemoryStore(case.base)
            replacement=review_backfill._claim(reopened,now+review_backfill.LEASE_SECONDS+1)
            self.assertIsNotNone(replacement)
            self.assertFalse(review_backfill.debit(case.index,first))
            impression_events.tick(case.base,backfill=replacement)
            for _ in range(13):
                review_backfill.tick(case.base)
            self.assertEqual(sol.call_count,10)
        with reopened.db() as db:
            self.assertEqual(db.execute('SELECT credits FROM review_backfill_state').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM review_recovery_jobs WHERE status='done'").fetchone()[0],10)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM impression_effects').fetchone()[0],1)
            saved=review_recovery.load_metadata(db,'daily',settled)
            self.assertEqual((saved['status'],saved['original_review_at']),('already_reviewed',101))
            self.assertEqual(db.execute("SELECT status FROM daily_event_jobs WHERE source_id=90000").fetchone()[0],'expired')

    def test_recovered_review_keeps_original_time_and_survives_bad_sibling(self):
        case=fixtures.PostReplyReviewTests()
        case.setUp();self.addCleanup(case.doCleanups)
        human=case.human(targets=('999',));case.send(humans=[human])
        review.close(case.base,'group:1',1020)
        with case.index.db() as db:
            review_recovery.ensure_metadata(db)
            window=db.execute('SELECT id FROM reply_review_windows').fetchone()[0]
            db.execute("""INSERT INTO review_recovery_jobs(kind,job,session,target,original_review_at,
                time_basis,status,reason,prepared_at) VALUES ('window',?,'group:1','101',1021,
                'first_audit','pending','恢复复盘',1029)""",(window,))
        def runner(packet,base):
            valid=case.verdict(packet,base)['relationship_events'][0]
            return {'relationship_events':[None,dict(valid,event_id='m'+str(packet['messages'][0]['id']),directed_to_hakimi='true')]}
        result=review.tick(case.base,1030,runner=runner)
        self.assertEqual((result['status'],result['reviewed'],result['ignored_sources']),('completed',1,1))
        with case.index.db() as db:
            self.assertEqual(db.execute('SELECT at FROM relationship_change_log').fetchone()[0],1021)
            self.assertEqual(tuple(db.execute('SELECT status,completed_at FROM review_recovery_jobs').fetchone()),('done',1030))
        rows=review_history_payload(case.base,{'person':'101'})['items']
        recovered=[r for r in rows if r.get('recovery')]
        self.assertEqual(len(recovered),1)
        self.assertEqual((recovered[0]['at'],recovered[0]['affinity_delta']),(1021,1))
        self.assertIsNone(recovered[0]['affinity_before'])
