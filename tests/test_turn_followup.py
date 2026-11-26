"""Exercise follow-up ownership and commit recovery without network or providers."""
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from hakimi.bridge.turn_coordinator import TurnCoordinator


class TurnFollowupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.now = 1000.0
        self.coordinator = TurnCoordinator(Path(directory.name), clock=lambda: self.now)

    def payload(self, trigger, ids, *, session='private:12', user='12'):
        return dict(trigger_id=trigger, created_at=self.now, session_key=session,
                    wake_mode='即时唤醒(被@/私聊)', reply_target_message_id=ids[-1],
                    pending_explicit_question_ids=ids,
                    recent_context_messages=[dict(message_id=mid, user_id=user, session_key=session,
                                                  text=f'Question {mid}') for mid in ids])

    def replace(self, trigger='new', *, ids=None, old='old', **kwargs):
        return self.coordinator.publish(self.payload(trigger, ids or ['1', '2'], **kwargs),
                                        replace_trigger_id=old)

    def outbox(self):
        path = self.coordinator.outbox_path
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []

    def test_replaces_active_private_owner_and_records_old_outcome(self):
        self.assertTrue(self.coordinator.publish(self.payload('old', ['1']))['accepted'])
        old = self.coordinator.claim_primary('old')['token']
        self.assertIsNone(self.coordinator.available_lane(self.payload('new', ['1', '2'])))
        self.assertEqual(self.replace(), dict(accepted=True, status='queued', trigger_id='new',
                                              conversation_lane='', replaced_trigger_id='old'))
        self.assertFalse(self.coordinator.check_token(old))
        self.assertTrue(self.coordinator.was_superseded('old'))
        self.assertEqual(self.coordinator.submit_reply(old, ['stale'], 'gemini')['reason'], 'stale_owner')
        new = self.coordinator.claim_primary('new')['token']
        self.assertTrue(self.coordinator.submit_reply(new, ['combined'], 'gemini')['accepted'])
        self.assertEqual([record['trigger_id'] for record in self.outbox()], ['new'])
        self.assertEqual(set(self.coordinator.snapshot()['answered_sources']),
                         {'private:12/1', 'private:12/2'})

    def test_full_group_lanes_and_checkpoint_failure(self):
        self.coordinator.publish(self.payload('other', ['99'], session='group:90'))
        result = self.coordinator.publish(self.payload('old', ['1'], session='group:91'))
        lane = self.coordinator.for_lane(result['conversation_lane'])
        old = lane.claim_primary('old')['token']
        for group in ('92', '93'):
            self.coordinator.publish(self.payload(f'other-{group}', ['99'], session=f'group:{group}'))
        self.assertIsNone(self.coordinator.available_lane(self.payload('new', ['1', '2'], session='group:91')))
        with patch('hakimi.bridge.turn_coordinator.atomic_replace', side_effect=OSError('checkpoint failure')):
            with self.assertRaises(OSError):
                self.replace(session='group:91')
        self.assertTrue(lane.check_token(old))
        self.assertFalse(lane.was_superseded('old'))
        result = self.replace(session='group:91')
        self.assertTrue(result['accepted'])
        self.assertEqual(result['conversation_lane'], lane.lane_id)
        self.assertFalse(lane.check_token(old))
        self.assertTrue(lane.was_superseded('old'))

    def test_rejects_cross_author_cross_session_and_repeated_source(self):
        self.coordinator.publish(self.payload('old', ['1'], session='group:91'))
        for params in (dict(session='group:91', user='13'), dict(session='group:92'),
                       dict(session='group:91', ids=['1'])):
            self.assertEqual(self.replace(**params)['reason'], 'replacement_unavailable')
        payload = self.payload('new', ['1', '2'], session='group:91')
        payload['recent_context_messages'][-1]['user_id'] = '13'
        self.assertEqual(self.coordinator.publish(payload, replace_trigger_id='old')['reason'],
                         'replacement_unavailable')
        self.assertEqual(self.coordinator.snapshot()['trigger_id'], 'old')

    def test_expired_owner_is_ineligible(self):
        self.coordinator.publish(self.payload('old', ['1']))
        claim = self.coordinator.claim_primary('old')
        self.now = claim['deadline']
        self.assertEqual(self.replace()['reason'], 'replacement_unavailable')
        self.assertFalse(self.coordinator.was_superseded('old'))

    def test_replaces_fallback_without_extending_original_lifetime(self):
        self.coordinator.publish(self.payload('old', ['1']))
        primary = self.coordinator.claim_primary('old')
        self.assertTrue(self.coordinator.report_failure(primary['token'], 'model_error'))
        fallback = self.coordinator.claim_fallback()
        self.assertIsNotNone(fallback)
        self.now += 10
        self.assertTrue(self.replace()['accepted'])
        self.assertEqual(self.coordinator.snapshot()['expires_at'], primary['expires_at'])
        self.assertFalse(self.coordinator.check_token(fallback['token']))

    def test_replacement_audit_and_id_survive_interrupted_audit_flush(self):
        self.coordinator.publish(self.payload('other', ['99'], session='group:90'))
        result = self.coordinator.publish(self.payload('old', ['1'], session='group:91'))
        lane = self.coordinator.for_lane(result['conversation_lane'])
        with patch.object(TurnCoordinator, '_write_turn_events', side_effect=OSError('audit failure')):
            self.assertTrue(self.replace(session='group:91')['accepted'])
            self.assertTrue(lane.was_superseded('old'))
            result = self.coordinator.publish(self.payload('new', ['8'], session='group:92'))
            self.assertEqual(result['reason'], 'duplicate_trigger')
        self.assertEqual(lane.snapshot()['trigger_id'], 'new')
        self.assertTrue(lane.was_superseded('old'))

    def test_reconciles_durable_intent_and_keeps_committed_owner(self):
        self.coordinator.publish(self.payload('old', ['1']))
        token = self.coordinator.claim_primary('old')['token']
        self.coordinator.enable_post_reply(token)
        with patch.object(self.coordinator, '_append_bytes', side_effect=OSError('append interrupted')):
            with self.assertRaises(OSError):
                self.coordinator.submit_reply(token, ['committed'], 'gemini')
        self.assertEqual(self.replace()['reason'], 'replacement_unavailable')
        self.assertTrue(self.coordinator.check_token(token))
        self.assertEqual([record['trigger_id'] for record in self.outbox()], ['old'])
        self.assertFalse(self.coordinator.was_superseded('old'))

    def test_submit_and_replace_are_linearized(self):
        self.coordinator.publish(self.payload('old', ['1']))
        token = self.coordinator.claim_primary('old')['token']
        ready = threading.Barrier(2)

        def submit():
            ready.wait(timeout=5)
            return self.coordinator.submit_reply(token, ['answer'], 'gemini')

        def replace():
            ready.wait(timeout=5)
            return self.replace()

        with ThreadPoolExecutor(max_workers=2) as pool:
            submission = pool.submit(submit)
            replacement = pool.submit(replace)
            self.assertNotEqual(submission.result()['accepted'], replacement.result()['accepted'])
        self.assertLessEqual(len(self.outbox()), 1)


if __name__ == '__main__':
    unittest.main()
