import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hakimi.models.vertex_session import VertexSession, declarations
from hakimi.prompt import tool_sticky
from hakimi.prompt.tool_selection import ON_DEMAND, REVIEW, select_names, sticky_names


def event(text, key='group:123', **extra):
    return {'session_key': key, 'trigger_id': 'test',
            'recent_context_messages': [{'user_id': '456', 'message_id': '1', 'text': text}],
            **extra}


def names_of(body):
    return [row['name'] for row in body['tools'][0]['functionDeclarations']]


def reply(body, timeout):
    if 'reply.send' in names_of(body):
        call = {'name': 'reply.send', 'args': {'text': '好'}}
    else:
        call = {'name': 'reply.skip', 'args': {}}
    return {'candidates': [{'finishReason': 'STOP', 'content': {'role': 'model', 'parts': [
        {'functionCall': call}]}}]}


def wire(body):
    return json.dumps(body['tools'], ensure_ascii=False, separators=(',', ':'))


DIARY = '你昨天日记写了啥，发给我看看'
FILES = '把这段讨论整理成 Markdown 文件发给我'


class StickyToolTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.names = {row['name'] for row in declarations()}
        self.provider = Mock(side_effect=reply)
        self.session = VertexSession([], self.base, {}, transport=self.provider,
                                     image_loader=lambda _: [],
                                     tool_runner=Mock(return_value={'ok': True}))

    def turn(self, payload):
        self.session.generate('测试', Mock(), {'token': 'T', 'payload': payload})
        return self.provider.call_args.args[0]

    def sticky(self, payload, now):
        selected = select_names(payload, self.names, self.base)
        return sticky_names(payload, self.names, selected, self.base, now=now)

    def test_turns_with_different_text_send_identical_declarations_once_seen(self):
        first = self.turn(event(DIARY))
        self.assertIn('diary.read', names_of(first))
        self.assertNotIn('files.send', names_of(first))
        second = self.turn(event(FILES))
        third = self.turn(event(DIARY))
        greeting = self.turn(event('早上好'))
        self.assertTrue({'diary.read', 'diary.export', 'files.send'} <= set(names_of(second)))
        self.assertEqual(wire(second), wire(third))
        self.assertEqual(wire(second), wire(greeting))
        self.assertTrue(set(names_of(second)) > set(names_of(first)))

    def test_permission_gates_still_remove_sticky_tools(self):
        self.turn(event(DIARY))
        self.turn(event(FILES))
        review = self.turn(event('回看', internal_review={}))
        self.assertEqual(set(names_of(review)), REVIEW)
        # A remembered per-turn offer (drawing, voice) is never declared by stickiness.
        path = self.base / tool_sticky.FILE
        rows = json.loads(path.read_text(encoding='utf-8'))
        rows['group:123'].update({'image.draw': max(rows['group:123'].values()),
                                  'voice.speak': max(rows['group:123'].values())})
        path.write_text(json.dumps(rows), encoding='utf-8')
        declared = set(names_of(self.turn(event('早上好'))))
        self.assertIn('files.send', declared)
        self.assertFalse({'image.draw', 'voice.speak', 'voice.hum'} & declared)
        # Tools missing from the permitted names are never added.
        self.assertNotIn('files.send', sticky_names(event('早上好'), self.names - {'files.send'},
                                                    {'reply.send'}, self.base))

    def test_declarations_follow_catalog_order(self):
        catalog = [row['name'] for row in declarations()]
        chosen = ['web.read', 'reply.send', 'files.send', 'diary.read', 'image.read']
        forward = json.dumps(declarations(chosen), ensure_ascii=False, separators=(',', ':'))
        backward = json.dumps(declarations(list(reversed(chosen))), ensure_ascii=False, separators=(',', ':'))
        self.assertEqual(forward, backward)
        self.assertEqual([row['name'] for row in declarations(chosen)],
                         [name for name in catalog if name in chosen])
        self.turn(event(FILES))
        body = self.turn(event(DIARY))
        self.assertEqual(names_of(body), [name for name in catalog if name in names_of(body)])

    def test_sticky_tools_expire_after_the_window(self):
        start = 1_000_000.0
        self.assertIn('diary.read', self.sticky(event(DIARY), start))
        window = tool_sticky.WINDOW_SECONDS
        self.assertIn('diary.read', self.sticky(event('早上好'), start + window - 1))
        self.assertNotIn('diary.read', self.sticky(event('早上好'), start + window + 1))
        # Another session never sees this session's tools.
        self.assertNotIn('diary.read', self.sticky(event('早上好', key='group:9'), start + 1))

    def test_research_defaults_are_not_remembered(self):
        payload = event('早上好')
        selected = select_names(payload, self.names, self.base, research=True)
        self.assertTrue(ON_DEMAND <= selected)
        sticky_names(payload, self.names, selected, self.base, research=True)
        self.assertFalse(ON_DEMAND & self.sticky(event('早上好'), None))

    def test_corrupted_or_oversized_state_is_tolerated(self):
        path = self.base / tool_sticky.FILE
        path.parent.mkdir(parents=True)
        for damaged in ('{not json', '[1, 2]', '{"group:123": {"diary.read": "soon", "files.send": null}}'):
            path.write_text(damaged, encoding='utf-8')
            self.assertNotIn('files.send', self.sticky(event('早上好'), None))
        self.assertIn('diary.read', self.sticky(event(DIARY), None))
        self.assertIn('diary.read', self.sticky(event('早上好'), None))
        json.loads(path.read_text(encoding='utf-8'))
        with patch.object(tool_sticky, 'MAX_SESSIONS', 3):
            for index in range(5):
                self.sticky(event(DIARY, key=f'group:{index}'), 2_000_000.0 + index)
        self.assertEqual(sorted(json.loads(path.read_text(encoding='utf-8'))),
                         ['group:2', 'group:3', 'group:4'])
        self.assertFalse(list(path.parent.glob('*.tmp')))


if __name__ == '__main__':
    unittest.main()
