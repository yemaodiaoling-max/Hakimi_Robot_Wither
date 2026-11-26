import json
import unittest

import test_post_reply_review as fixtures
from hakimi.console.payloads_review_history import review_history_payload
from hakimi.memory import review_recovery, review_results
from hakimi.curation import impression_events, post_reply_review


class ReviewResultTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.PostReplyReviewTests()
        self.case.setUp();self.addCleanup(self.case.doCleanups)
        human=self.case.human(targets=('999',));self.case.send(humans=[human])
        post_reply_review.close(self.case.base,'group:1',1020)

    def metadata(self,db):
        review_recovery.ensure_metadata(db)
        window=db.execute('SELECT id FROM reply_review_windows').fetchone()[0]
        db.execute("""INSERT INTO review_recovery_jobs(kind,job,session,target,original_review_at,time_basis,status,reason,prepared_at)
            VALUES ('window',?,'group:1','101',1021,'first_matching_audit','done','old generic receipt',1030)""",(window,))
        return window

    def test_actual_scoring_and_source_survive_audit_and_raw_expiry_without_duplicate_rows(self):
        post_reply_review.tick(self.case.base,1021,runner=self.case.verdict)
        with self.case.index.db() as db:
            window=self.metadata(db)
            review_results.snapshot_result(db,'window',window)
        page=review_history_payload(self.case.base,{'person':'101'})
        self.assertEqual(page['total'],1)
        row=page['items'][0]
        self.assertEqual((row['affinity_delta'],row['closeness_delta']),(1,2))
        self.assertIn('愿意陪你',row['review_result']['events'][0]['excerpt'])
        with self.case.index.db() as db:
            db.execute('DELETE FROM relationship_review_audit')
            db.execute("UPDATE records SET text='' WHERE kind='chat'")
            before=tuple(db.execute('SELECT affinity,closeness FROM impression_scores').fetchone())
        again=review_history_payload(self.case.base,{'person':'101'})
        self.assertEqual(again['items'][0]['review_result'],row['review_result'])
        with self.case.index.db() as db:
            self.assertEqual(tuple(db.execute('SELECT affinity,closeness FROM impression_scores').fetchone()),before)

    def test_explicit_correction_uses_existing_policy_and_keeps_the_prior_decision(self):
        post_reply_review.tick(self.case.base,1021,runner=self.case.verdict)
        with self.case.index.db() as db:
            window=self.metadata(db)
            source=db.execute("SELECT * FROM records WHERE origin='human'").fetchone()
            job={'session':'group:1','target':'101','record_id':None,'original_id':None,
                 'noted_at':1000,'review_source':'window','review_job':window,'reviewer':'operator_context_review',
                 'previous_decision':'原来作为情绪支持计分','reviewed_at':1030}
            item={'event_id':source['id'],'kind':'relationship','directed_to_hakimi':True,
                  'event_type':'banter','evidence':source['text'],'reason':'顺着话题接梗，没有明确开心表现',
                  'reaction_type':'neutral','reaction_source':'reply','reaction_evidence':'我在听呢'}
            direction={'author':'101','self_id':'999','session':'group:1','mentions':['999'],
                       'context':[{'kind':'chat','user_id':'999','text':'我在听呢','at':1010,'reply_to':['human']}]}
            self.assertEqual(impression_events.apply(db,job,None,item,1030,direction=direction,allow_revision=True),'revised')
            review_recovery.complete_metadata(db,'window',window,1030)
            result=json.loads(db.execute('SELECT result FROM review_recovery_jobs').fetchone()[0])
            self.assertEqual(result['deltas'],{'affinity':-1,'closeness':-2})
            self.assertEqual(result['events'][0]['details']['settled_effect'],{'affinity':0,'closeness':0})
            self.assertIn('原来作为',result['events'][0]['details']['previous_decision'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM impression_effects').fetchone()[0],1)
