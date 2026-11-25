import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hakimi.models import antigravity_acp as acp, antigravity_tasks as tasks
from hakimi.models.task_routing import FLASH, CARBON
from hakimi.ops import cloud_service
from hakimi.memory import personal_review


class RoutingTests(unittest.TestCase):
    def test_opt_in_and_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'task_routing.json').write_text(json.dumps(
                {'mode': 'dual', 'single_model': FLASH, 'dual_model': CARBON}))
            route = SimpleNamespace(enabled=True, model=FLASH)
            def choose(text=None, **extra):
                row = {'message_id': '1', 'text': text, 'user_id': '123'}
                return tasks.routing(root, dict(recent_context_messages=[row],
                    reply_target_message_id='1', session_key='group:123', **extra), route)
            self.assertEqual(choose('请帮我详细推导波动方程')['model'], CARBON)
            (root/'antigravity_tasks.json').write_text(json.dumps(
                {'complex': 'test-high', 'reflection': 'test-medium', 'diary': 'test-medium'}))
            self.assertEqual(choose('请帮我详细推导波动方程')['model'], 'test-high')
            self.assertEqual(choose('你好')['model'], FLASH)
            self.assertEqual(choose('你好', internal_review={})['model'], 'test-medium')
            self.assertNotIn('provider', choose('你好'))
            with patch('hakimi.models.vertex_route.check', side_effect=AssertionError):
                self.assertFalse(personal_review._vertex_budget_blocked(root, time.time()))

    def test_dispatch_and_review_tool_receipts(self):
        coordinator = Mock()
        claim = {'payload': {'session_key': 'group:123', 'internal_review': {}},
                 'token': 'test', 'task_model_routing': {'model':'test-medium',
                 'reason':'internal_review', 'provider':'antigravity'}}
        reply = '{"calls":[{"name":"memory.social","args":{"target_id":"123","note":"test"}},{"name":"reply.skip","args":{}}],"text":""}'
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(cloud_service, 'RUNTIME', Path(directory)), \
                patch('hakimi.models.vertex_route.load'), \
                patch('hakimi.models.vertex_route.enforce_primary', side_effect=AssertionError), \
                patch.object(tasks.VertexSession, '_images', return_value=[]), \
                patch.object(tasks.VertexSession, '_tool', return_value={'ok': True}) as tool, \
                patch.object(acp, 'complete', return_value=(reply,'synthetic',{})):
            result = cloud_service.generate('synthetic', coordinator, claim)
            self.assertEqual(result['status'], 'SUCCESS')
            self.assertEqual([c.args[0] for c in tool.call_args_list], ['memory.social','reply.skip'])
            audit = json.loads((Path(directory)/'state/task_model_routing.jsonl').read_text())
            self.assertEqual(audit['actual_requests'][0]['provider'], 'antigravity')

    def test_image_survives_without_becoming_a_filename(self):
        body = {'systemInstruction': {'parts': [{'text':'instructions'}]},
                'contents':[{'role':'user','parts':[{'inlineData':{'data':'YQ==','mimeType':'image/png'}}]}]}
        self.assertEqual(tasks.prompt_blocks(body)[-1],
                         {'type':'image','data':'YQ==','mimeType':'image/png'})


class WireTests(unittest.TestCase):
    def test_model_receipt_required_before_prompt_and_permissions_cancelled(self):
        class Pipe:
            def __init__(self, accepted):
                self.events = []
                self.writes = []
                self.accepted = accepted
            async def drain(self):
                pass
            async def readline(self):
                return json.dumps(self.events.pop(0)).encode()
            def write(self, raw):
                item = json.loads(raw)
                self.writes.append(item)
                if 'method' not in item:
                    return
                method = item['method']
                if method == 'initialize':
                    value = {}
                elif method == 'session/new':
                    value = {'sessionId':'id','models':{'availableModels':[{'modelId':'neon-medium'}]}}
                elif method == 'session/set_config_option':
                    value = {'configOptions':[{'id':'model','currentValue':'neon'},
                        {'id':'thought_level','currentValue':'medium' if self.accepted else 'high'}]}
                else:
                    self.events.extend([{'method':'session/request_permission', 'id':62},
                        {'method':'session/update','params':{'sessionId':'id','update':{
                        'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'ok'}}}}])
                    value = {'stopReason':'end_turn'}
                self.events.append({'id':item['id'],'result':value})
        for accepted in (False, True):
            pipe = Pipe(accepted)
            process = SimpleNamespace(stdout=pipe, stdin=pipe)
            call = acp._exchange(process, 'neon-medium', '/tmp', [{'type':'text','text':'test'}], None)
            if accepted:
                self.assertEqual(asyncio.run(call)[:2], ('ok','id'))
                self.assertEqual(pipe.writes[-1]['result']['outcome']['outcome'], 'cancelled')
            else:
                with self.assertRaisesRegex(acp.AntigravityError, 'model_mismatch'):
                    asyncio.run(call)
                self.assertFalse(any(w.get('method') == 'session/prompt' for w in pipe.writes))
