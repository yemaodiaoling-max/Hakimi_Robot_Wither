import unittest

import test_post_reply_review as fixtures
from hakimi.memory import post_reply_review as review, review_recovery
from hakimi.console.payloads_review_history import review_history_payload


class BackfillTests(unittest.TestCase):
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
