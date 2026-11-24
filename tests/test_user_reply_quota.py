"""Small offline regression for the shared reply budget; no model or QQ calls."""
import json
from pathlib import Path
import tempfile
import unittest

from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.bridge import user_reply_quota as quota


class UserReplyQuotaTests(unittest.TestCase):
    def test_seven_rounds_shared_by_providers_sessions_and_restart(self):
        with tempfile.TemporaryDirectory(prefix='hakimi_user_quota_') as directory:
            base = Path(directory)
            now = [1000.0]
            coordinator = lambda: TurnCoordinator(base, clock=lambda: now[0])
            def payload(trigger, user='22', session='group:123'):
                return {'trigger_id': trigger, 'session_key': session,
                        'reply_target_message_id': '1', 'recent_context_messages': [
                            {'message_id': '1', 'user_id': user, 'text': '问题'},
                            {'message_id': '2', 'user_id': '22', 'text': '另一个问题'}]}
            c = coordinator()
            for i in range(7):
                self.assertTrue(c.publish(payload(str(i), session='group:456' if i == 6 else 'group:123'))['accepted'])
                claim = c.claim_primary()
                if i % 2:
                    c.report_failure(claim['token'], 'unavailable')
                    claim = c.claim_fallback()
                result = c.submit_reply(claim['token'], ['回答', '补充'], claim['provider'])
                self.assertTrue(result['accepted'])
                self.assertFalse(c.submit_reply(claim['token'], ['重复'], claim['provider'])['accepted'])
                c = coordinator()
                now[0] += 1
            blocked = c.publish(payload('eighth', session='group:456'))
            self.assertEqual(blocked['reason'], quota.LIMIT_REASON)
            self.assertGreater(blocked['retry_after'], 0)
            self.assertTrue(c.publish(payload('other-user', user='33'))['accepted'])
            claim = c.claim_primary()
            self.assertEqual(c.submit_reply(claim['token'], ['改回回复22'], 'gemini', reply_to='2')['reason'], quota.LIMIT_REASON)
            now[0] = 2800
            self.assertTrue(coordinator().publish(payload('after-window'))['accepted'])
            history = c.snapshot()[quota.HISTORY_KEY]
            self.assertEqual(len(history), 6)

    def test_bound_owner_is_exempt_only_for_explicit_mentions(self):
        with tempfile.TemporaryDirectory(prefix='hakimi_owner_quota_') as directory:
            base = Path(directory); (base/'state').mkdir()
            (base/'state/feeding.json').write_text(json.dumps({'owner_user_id': '22'}))
            c = TurnCoordinator(base, clock=lambda: 1000)
            history = [dict(user_id='22', trigger_id=str(i), completed_at=999) for i in range(7)]
            state = {quota.HISTORY_KEY: history}
            payload = {'session_key': 'group:123', 'reply_target_message_id': '1',
                       'pending_explicit_question_ids': ['1'], 'recent_context_messages': [
                           {'message_id': '1', 'user_id': '22', 'text': '问题'},
                           {'message_id': '2', 'user_id': '33', 'text': '别人的问题'}]}
            self.assertEqual(c._user_reply_wait(state, payload), 0)
            self.assertGreater(c._user_reply_wait(state, dict(payload, pending_explicit_question_ids=[])), 0)
            state[quota.HISTORY_KEY] += [dict(user_id='33', trigger_id=str(i), completed_at=999) for i in range(7)]
            self.assertGreater(c._user_reply_wait(state, payload, reply_to='2'), 0)
            c._save(dict(c._load_ready(), **{quota.HISTORY_KEY: history}))
            self.assertTrue(c.publish(payload)['accepted'])
            claim = c.claim_primary()
            self.assertTrue(c.submit_reply(claim['token'], ['第一条', '第二条'], 'gemini')['accepted'])

    def test_old_outbox_history_is_imported_once(self):
        with tempfile.TemporaryDirectory(prefix='hakimi_user_quota_') as directory:
            base = Path(directory)
            (base/'state').mkdir()
            rows = [{'trigger_id': str(i), 'created_at': 999,
                     'interaction': {'user_id': '22'}, 'provider': 'dsh'} for i in range(7)]
            (base/'state/outbox.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
            c = TurnCoordinator(base, clock=lambda: 1000)
            question = {'session_key': 'group:123', 'recent_context_messages': [{'user_id': '22', 'message_id': '1'}],
                        'reply_target_message_id': '1'}
            self.assertEqual(c.publish(question)['reason'], quota.LIMIT_REASON)
            self.assertTrue(c.publish({'session_key': 'private:22'})['accepted'])

    def test_recipient_and_unaddressed_work(self):
        payload = {'session_key': 'group:123', 'reply_target_message_id': '1',
                   'recent_context_messages': [{'message_id': '1', 'user_id': '22', 'text': '问题'}]}
        self.assertEqual(quota.recipient(payload), '22')
        self.assertIsNone(quota.recipient(dict(payload, internal_review={'id': 1})))
        self.assertIsNone(quota.recipient(dict(payload, is_proactive=True)))
        self.assertEqual(quota.recipient(dict(payload, is_proactive=True), '1'), '22')
