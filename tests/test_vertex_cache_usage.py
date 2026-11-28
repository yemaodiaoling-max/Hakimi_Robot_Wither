import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.models.gateway_text import _extract_gemini_text_and_usage
from hakimi.models.vertex_session import VertexSession


def _response(parts, usage):
    return {'candidates': [{'finishReason': 'STOP', 'content': {'role': 'model', 'parts': parts}}],
            'usageMetadata': usage}


class VertexCacheUsageTests(unittest.TestCase):
    def test_cached_tokens_reach_audit_entries_summed_usage_and_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            provider = Mock(side_effect=[
                _response([{'functionCall': {'name': 'tools.list', 'args': {}}}],
                          {'promptTokenCount': 1000, 'cachedContentTokenCount': 800,
                           'candidatesTokenCount': 10, 'totalTokenCount': 1010}),
                _response([{'text': 'hi'}], {'promptTokenCount': 1100, 'candidatesTokenCount': 5,
                                             'totalTokenCount': 1105}),
            ])
            coordinator = TurnCoordinator(base)
            coordinator.publish({'session_key': 'group:123', 'trigger_id': 'cache',
                                 'recent_context_messages': [{'user_id': '1', 'message_id': '1', 'text': 'hi'}]})
            claim = coordinator.claim_primary()
            claim['task_model_routing'] = {'model': 'gemini-4-hydrogen', 'reason': 'test'}
            session = VertexSession([], base, {}, transport=provider, image_loader=lambda _: [])
            session.tool_runner = Mock(side_effect=lambda name, args, claim, timeout:
                                       session._tool(name, args, claim, timeout) if name == 'tools.list'
                                       else coordinator.submit_reply(claim['token'], [args['text']], 'gemini'))
            with patch('hakimi.models.task_routing.audit') as audit:
                result = session.generate('hi', coordinator, claim)
            requests = audit.call_args.args[4]
            self.assertEqual(len(requests), 2)
            self.assertEqual((requests[0]['promptTokenCount'], requests[0]['cachedContentTokenCount'],
                              requests[0]['candidatesTokenCount']), (1000, 800, 10))
            self.assertNotIn('cachedContentTokenCount', requests[1])
            self.assertEqual(requests[1]['promptTokenCount'], 1100)
            self.assertEqual(result['usage']['cached_input_tokens'], 800)
            self.assertEqual(result['usage']['input_tokens'], 2100)
            self.assertEqual(result['usage']['raw']['cachedContentTokenCount'], 800)
            self.assertEqual(session.usage['cachedContentTokenCount'], 800)

    def test_gateway_text_usage_keeps_cached_tokens(self):
        data = {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': 'x'}]}}],
                'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 2, 'cachedContentTokenCount': 6}}
        _, _, usage = _extract_gemini_text_and_usage(data, 'm')
        self.assertEqual(usage, {'prompt_tokens': 10, 'completion_tokens': 2, 'cached_tokens': 6})
        del data['usageMetadata']['cachedContentTokenCount']
        _, _, usage = _extract_gemini_text_and_usage(data, 'm')
        self.assertNotIn('cached_tokens', usage)


if __name__ == '__main__':
    unittest.main()
