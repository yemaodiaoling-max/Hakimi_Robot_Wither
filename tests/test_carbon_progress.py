import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.models.carbon_session import CarbonSession
from hakimi.models.carbon_timing import CarbonProgress


class CarbonProgressTests(unittest.TestCase):
    def test_slow_receipts_prompt_a_final_answer_in_the_same_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1000.]
            clock = lambda: now[0]
            root = Path(directory)
            coordinator = TurnCoordinator(root, clock=clock)
            coordinator.publish({'session_key': 'group:1', 'trigger_id': 'slow'})
            claim = coordinator.claim_primary()
            (root/'state/carbon_turn_timing.json').write_text(json.dumps({'timeout_seconds':129}))
            requests = []
            def transport(payload, timeout):
                now[0] += 146
                self.assertIsNone(coordinator.claim_fallback())
                session._progress()
                text = str(payload['contents'][-1])
                requests.append(text)
                parts = [{'text':'已完成'}] if '现在用已有结果' in text else [
                    {'functionCall':{'name':'time.now','args':{}}}]
                return {'candidates':[{'content':{'parts':parts},'finishReason':'STOP'}]}
            def tool(name, args, claim, timeout):
                if name == 'reply.send':
                    return coordinator.submit_reply(claim['token'],[args['text']],'gemini')
                return {'ok':True}
            session = CarbonSession([],root,{},transport=transport,image_loader=lambda _:[],tool_runner=tool)
            with patch('hakimi.models.carbon_timing.CarbonProgress',
                       side_effect=lambda c,k,s: CarbonProgress(c,k,s,clock)):
                result=session.generate('fixture',coordinator,claim)
            self.assertTrue(result['reply_submitted'])
            self.assertEqual(coordinator.snapshot()['outcome'],'replied')
            self.assertGreater(now[0]-1000,420)
            self.assertTrue(any('请整理已有材料' in r for r in requests))

    def test_carbon_can_deliver_after_four_and_a_half_minutes_without_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = [1000.]
            clock = lambda: now[0]
            coordinator = TurnCoordinator(root, clock=clock)
            coordinator.publish({'session_key': 'group:1', 'trigger_id': 'slow-first-response'})
            claim = coordinator.claim_primary()
            (root/'state/carbon_turn_timing.json').write_text(json.dumps({'timeout_seconds': 129}))
            session = CarbonSession([], root, {}, image_loader=lambda _: [],
                tool_runner=lambda name, args, claim, timeout:
                    coordinator.submit_reply(claim['token'], [args['text']], 'gemini'))

            def slow_response(payload, timeout):
                self.assertAlmostEqual(timeout, 300)
                now[0] += 270
                self.assertIsNone(coordinator.claim_fallback())
                call = {'functionCall': {'name': 'reply.send', 'args': {'text': '已完成'}}}
                return {'candidates': [{'content': {'parts': [call]}, 'finishReason': 'STOP'}]}

            with patch('hakimi.models.carbon_timing.CarbonProgress',
                       side_effect=lambda c, k, seconds: CarbonProgress(c, k, seconds, clock)), patch(
                       'hakimi.models.argon_session.ArgonSession._request', side_effect=slow_response) as transport:
                result = session.generate('fixture', coordinator, claim)
            transport.assert_called_once()
            self.assertTrue(result['reply_submitted'])
            self.assertEqual(coordinator.snapshot()['outcome'], 'replied')

    def test_stalled_carbon_hands_off_once(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1000.]
            clock = lambda: now[0]
            coordinator = TurnCoordinator(directory,clock=clock)
            coordinator.publish({'session_key':'group:1','trigger_id':'stalled'})
            claim=coordinator.claim_primary()
            progress=CarbonProgress(coordinator,claim,129,clock)
            now[0]+=146
            self.assertIsNone(coordinator.claim_fallback())
            progress.touch()
            now[0]+=366
            with self.assertRaisesRegex(TimeoutError,'carbon_no_progress'):
                progress.remaining()
            self.assertEqual(coordinator.claim_fallback()['provider'],'dsh')
            self.assertFalse(coordinator.check_token(claim['token'],'gemini'))
            self.assertIsNone(coordinator.claim_fallback())

if __name__ == '__main__':
    unittest.main()
