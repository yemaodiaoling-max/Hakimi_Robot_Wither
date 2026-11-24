import json
import unittest
import test_post_reply_review as fixture_module
from hakimi.memory import post_reply_review as review


class SourceIdTests(unittest.TestCase):
    def test_wire_uses_short_sources_and_resolves_only_known_ids(self):
        messages = [{'id':23145,'message_id':'7012345678901234567','author':'101','context':[]}]
        packet = {'messages':messages,'sent_replies':[{'receipt':'r1:0','message_id':'8112345678901234567','speaker':'SELF','text':'我在听','reply_to':['7012345678901234567']}]}
        wire = review.classifier_packet(packet)
        self.assertEqual(wire['messages'][0]['id'], 'e1')
        self.assertEqual(wire['sent_replies'][0]['id'], 'b1')
        self.assertEqual(wire['sent_replies'][0]['reply_to'], ['e1'])
        self.assertNotIn('7012345678901234567', json.dumps(wire))
        for value in ['e1','23145','M23145',23145,'7012345678901234567']:
            self.assertEqual(review.source_id(value,messages),23145)
        for value in ['b1','e2',True,23145.0,'999']:
            self.assertIsNone(review.source_id(value,messages))

    def fixture(self):
        case = fixture_module.PostReplyReviewTests()
        case.setUp(); self.addCleanup(case.doCleanups)
        human = case.human(targets=('999',));case.send(humans=[human])
        review.close(case.base,'group:1',1020)
        return case

    def test_valid_source_survives_an_unrelated_bad_id_without_retry(self):
        case = self.fixture()
        def runner(packet, base):
            valid = case.verdict(packet,base)['relationship_events'][0]
            return {'relationship_events':[dict(valid,event_id='b1'),dict(valid,event_id='e1')]}
        result = review.tick(case.base,1021,runner=runner)
        self.assertEqual((result['status'],result['reviewed'],result['ignored_sources']),('completed',1,1))
        with case.index.db() as db:
            self.assertEqual(db.execute('SELECT status FROM reply_review_windows').fetchone()[0], 'done')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM impression_effects').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM relationship_review_audit WHERE error_code='REVIEW_INVALID_RELATIONSHIP_SOURCE'").fetchone()[0],1)

    def test_invalid_only_result_is_audited_once_without_rescoring(self):
        case = self.fixture()
        def runner(packet,base):
            valid = case.verdict(packet,base)['relationship_events'][0]
            return {'relationship_events':[dict(valid,event_id='e999')]}
        result = review.tick(case.base,1021,runner=runner)
        self.assertEqual((result['status'],result['reviewed'],result['ignored_sources']),('completed',0,1))
        with case.index.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM impression_effects').fetchone()[0],0)
            self.assertEqual(tuple(db.execute('SELECT status,attempts FROM reply_review_windows').fetchone()),('done',0))
