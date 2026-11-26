import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.models.antigravity_tasks import AntigravitySession
from hakimi.models.argon_session import ArgonSession, chat_tools, messages
from hakimi.models.carbon_session import CarbonSession
from hakimi.models.carbon_timing import CarbonProgress
from hakimi.models.vertex_session import VertexSession, declarations
from hakimi.prompt.prompt_context import render_vertex_prompt
from hakimi.prompt.tool_selection import ON_DEMAND, REVIEW, select_names


def event(text='早上好', **extra):
    return {'session_key': 'group:123', 'trigger_id': 'test',
            'recent_context_messages': [{'user_id': '456', 'message_id': '1', 'text': text}],
            **extra}


def response(*parts):
    return {'candidates': [{'finishReason': 'STOP', 'content': {'role': 'model', 'parts': list(parts)}}]}


def call(name, **args):
    return {'functionCall': {'name': name, 'args': args}}


def schemas(body):
    return {entry['name']: entry['parametersJsonSchema'] for group in body['tools']
            for entry in group['functionDeclarations']}


def receipt(body, index=0):
    return body['contents'][-1]['parts'][index]['functionResponse']['response']


class DirectToolSelectionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.names = set(schemas({'tools': [{'functionDeclarations': declarations()}]}))

    def session(self, provider, cls=VertexSession, **kwargs):
        session = cls([], self.base, {}, transport=provider, image_loader=lambda _: [], **kwargs)
        def run(name, args, claim, timeout):
            if name == 'tools.list':
                return session._tool(name, args, claim, timeout)
            if name == 'memory.graph':
                return {'result': {'nodes': []}}
            return {'ok': True}
        session.tool_runner = Mock(side_effect=run)
        return session

    def test_greeting_finishes_in_one_model_request(self):
        provider = Mock(return_value=response({'text': '早呀喵'}))
        coordinator = TurnCoordinator(self.base)
        coordinator.publish(event())
        claim = coordinator.claim_primary()
        session = VertexSession([], self.base, {}, transport=provider, image_loader=lambda _: [])
        session.tool_runner = Mock(side_effect=lambda name, args, claim, timeout:
            coordinator.submit_reply(claim['token'], [args['text']], 'gemini'))

        result = session.generate('早上好', coordinator, claim)

        provider.assert_called_once()
        declared = schemas(provider.call_args.args[0])
        self.assertEqual(len(declared), 18)
        self.assertFalse(ON_DEMAND & declared.keys())
        self.assertIn('memory.social_append', declared)
        self.assertTrue(result['reply_submitted'])
        self.assertEqual(coordinator.snapshot()['outcome'], 'replied')

    def test_menu_keeps_tools_lazy_then_exact_names_load_and_execute(self):
        provider = Mock(side_effect=[
            response(call('tools.list')),
            response(call('tools.list', tool_name='memory.graph'), call('tools.list', tool_name='image.read')),
            response(call('memory.graph', topic='校园夜景'), call('reply.send', text='找到啦')),
        ])
        session = self.session(provider)
        result = session.generate('测试', Mock(), {'token': 'T', 'payload': event()})

        first, menu, loaded = [item.args[0] for item in provider.call_args_list]
        self.assertEqual(schemas(first), schemas(menu))
        listed = {row['name'] for row in receipt(menu)['tools']}
        self.assertTrue({'memory.graph', 'image.read', 'diary.read', 'web.read'} <= listed)
        self.assertFalse({'image.draw', 'voice.speak', 'voice.hum'} & listed)
        self.assertEqual(schemas(loaded).keys() - schemas(first).keys(), {'memory.graph', 'image.read'})
        self.assertEqual(receipt(loaded)['inputSchema'], schemas(loaded)['memory.graph'])
        self.assertEqual(receipt(loaded, 1)['inputSchema'], schemas(loaded)['image.read'])
        self.assertIn('type', schemas(loaded)['image.read']['properties']['path'])
        self.assertIn(('memory.graph', {'topic': '校园夜景'}), [item.args[:2] for item in session.tool_runner.call_args_list])
        self.assertTrue(result['reply_submitted'])

        # Discovery does not leak to the next event, even on the same session.
        provider.side_effect = [response(call('reply.send', text='早呀'))]
        session.generate('下一轮', Mock(), {'token': 'T2', 'payload': event()})
        self.assertEqual(schemas(provider.call_args.args[0]), schemas(first))

    def test_unknown_drawing_and_unoffered_voice_cannot_be_loaded(self):
        provider = Mock(side_effect=[
            response(*(call('tools.list', tool_name=name)
                       for name in ('shell', 'image.draw', 'voice.speak'))),
            response({'text': '早呀'}),
        ])
        self.session(provider).generate('测试', Mock(), {'token': 'T', 'payload': event()})
        first, last = [item.args[0] for item in provider.call_args_list]
        self.assertEqual(schemas(last), schemas(first))
        for index in range(3):
            self.assertEqual(receipt(last, index), {'error': 'unknown_tool'})

    def test_real_requests_and_related_context_receive_their_tools_immediately(self):
        examples = {
            '你昨天日记写了啥，发给我看看': {'diary.read', 'diary.export'},
            '你对我是什么印象？': {'memory.graph'},
            '人物小本子帮我新建档吧': {'memory.social'},
            '检查一下工具为什么报错': {'runtime.status'},
            '把这段讨论整理成 Markdown 文件发给我': {'files.list', 'files.send'},
            '能导出聊天记录吗？': {'chat.export'},
        }
        for text, expected in examples.items():
            with self.subTest(text=text):
                self.assertTrue(expected <= select_names(event(text), self.names, self.base))
        casual_diary = select_names(event('我想养成每天写日记的习惯'), self.names, self.base)
        self.assertNotIn('diary.export', casual_diary)
        attached = select_names(event(attachments=[{'file_name': 'notes.txt'}]), self.names, self.base)
        self.assertTrue({'files.list', 'files.send', 'chat.export', 'diary.export'} <= attached)
        previous = {'message_id': '0', 'user_id': '456', 'text': '我想看你的日记'}
        payload = event()
        payload['recent_context_messages'].insert(0, previous)
        self.assertNotIn('diary.read', select_names(payload, self.names, self.base))
        payload['recent_context_messages'][-1].update(reply_to='0', text='就这个')
        self.assertIn('diary.read', select_names(payload, self.names, self.base))

    def test_rendered_opportunities_feed_only_the_current_local_claim(self):
        payload = event('那天的校园夜景呢？', _prompt_tool_hints=['runtime.status'])
        claim = {'token': 'T', 'payload': payload}
        context = {'session_key': 'group:123', 'own_diary_memory': [{'day': '2026-11-23'}],
                   'social_memory_opportunity': {'target_id': '456'},
                   'memory_recall_opportunity': {'tool': 'memory.graph'}}
        with patch('hakimi.prompt.prompt_context.build_context', return_value=context):
            prompt = render_vertex_prompt(payload, self.base, claim)
        provider = Mock(return_value=response({'text': '我记得'}))
        self.session(provider).generate(prompt, Mock(), claim)
        offered = schemas(provider.call_args.args[0]).keys()
        self.assertTrue({'diary.read', 'memory.social', 'memory.graph'} <= offered)
        self.assertNotIn('runtime.status', offered)
        self.assertNotIn('_prompt_tool_hints', prompt)
        self.assertEqual(payload['_prompt_tool_hints'], ['runtime.status'])

    def test_review_discovery_and_execution_stay_with_review_tools(self):
        provider = Mock(side_effect=[
            response(call('tools.list'), call('tools.list', tool_name='web.read'),
                     call('web.read', url='https://example.org')),
            response(call('reply.skip')),
        ])
        session = self.session(provider)
        result = session.generate('回看', Mock(), {'token': 'T', 'payload': event(internal_review={})})
        last = provider.call_args.args[0]
        self.assertEqual(schemas(last).keys(), REVIEW)
        self.assertEqual({row['name'] for row in receipt(last)['tools']}, REVIEW)
        self.assertEqual(receipt(last, 1), {'error': 'unknown_tool'})
        self.assertEqual(receipt(last, 2), {'error': 'review_tool_not_allowed'})
        self.assertNotIn('web.read', [item.args[0] for item in session.tool_runner.call_args_list])
        self.assertFalse(result['reply_submitted'])

    def test_argon_backup_adapter_encodes_and_resolves_the_new_tool(self):
        wires = []
        def transport(body, timeout):
            _, name_map = chat_tools(body)
            wires.append((set(name_map.values()), messages(body)))
            if len(wires) == 1:
                return response(call('tools.list', tool_name='memory.graph'))
            return response(call('memory.graph', topic='校园夜景'), call('reply.send', text='找到了'))
        result = self.session(transport, ArgonSession).generate('交谈', Mock(), {'token': 'T', 'payload': event()})
        self.assertEqual(wires[1][0] - wires[0][0], {'memory.graph'})
        self.assertEqual(json.loads(wires[1][1][-1]['content'])['name'], 'memory.graph')
        self.assertTrue(result['reply_submitted'])

    def test_antigravity_adds_named_tools_to_its_json_schema(self):
        session = AntigravitySession('test-medium', self.base, {}, image_loader=lambda _: [])
        def run(name, args, claim, timeout):
            return session._tool(name, args, claim, timeout) if name == 'tools.list' else {'ok': True}
        session.tool_runner = Mock(side_effect=run)
        actions = [
            {'calls': [{'name': 'tools.list', 'args': {'tool_name': 'memory.graph'}}], 'text': ''},
            {'calls': [{'name': 'memory.graph', 'args': {'topic': '校园夜景'}},
                       {'name': 'reply.send', 'args': {'text': '找到了'}}], 'text': ''},
        ]
        with patch('hakimi.models.antigravity_tasks.acp.complete',
                   side_effect=[(json.dumps(action), 'synthetic', {}) for action in actions]) as complete:
            result = session.generate('交谈', Mock(), {'token': 'T', 'payload': event()})
        first, last = [item.args[1][0]['text'] for item in complete.call_args_list]
        self.assertNotIn('"memory.graph":', first)
        self.assertIn('"memory.graph":', last)
        self.assertTrue(result['reply_submitted'])

    def test_carbon_research_has_direct_tools_and_closing_blocks_discovery(self):
        coordinator = Mock()
        coordinator.clock.return_value = 1000.
        claim = {'token': 'T', 'payload': event()}
        progress = CarbonProgress(coordinator, claim, 120)
        requests = []
        def transport(body, timeout):
            requests.append(body)
            if len(requests) == 1:
                progress.nudged = 0
                progress.rounds = 1
                return response(call('tools.list', tool_name='image.read'))
            if len(requests) == 2:
                return response(call('tools.list', tool_name='video.watch'))
            return response({'text': '材料整理好了'})

        session = self.session(transport, CarbonSession)
        session._carbon_progress = progress
        session._carbon_attempts = []
        session._carbon_actions = {}
        session._carbon_stage = 'carbon'
        session.generate('深度研究', coordinator, claim, timeout=30)

        self.assertTrue(ON_DEMAND <= schemas(requests[0]).keys())
        self.assertIn('image.read', receipt(requests[1])['name'])
        self.assertTrue(schemas(requests[1]).keys() <= progress.delivery)
        self.assertEqual(receipt(requests[2])['error'], 'research_complete')
        self.assertEqual([item.args[0] for item in session.tool_runner.call_args_list], ['tools.list', 'reply.send'])
