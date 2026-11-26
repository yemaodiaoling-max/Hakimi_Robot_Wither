"""Real model runners, durable ownership and official QQ formatting, with no network."""
import json
import os
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from hakimi.chat import bot_registry
from hakimi.chat.reply_format import text_bubbles
from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.chat.turn_lanes import lane_context, lane_id
from hakimi.models.dsh_tools import resolve_tools
from hakimi.models.fallback_commit import submit_fallback_result
from hakimi.models.fallback_runner import parse_response
from hakimi.models.provider_health import ProviderHealth
from hakimi.models.vertex_session import VertexSession
from hakimi.prompt.prompt_context import activate_context
from hakimi.chat.member_mentions import mention
from hakimi.transport.official_transport import OfficialAdapter, RouteStore
from test_delivery_guard import OfflineEventLoop
from test_official_adapter import fixture_root


GROUP = 'group:850245632'


class MentionDeliveryFlow(unittest.IsolatedAsyncioTestCase):
    loop_factory = OfflineEventLoop

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        fixture_root(self.root)
        self.base = self.root/'official_test_runtime'
        # fixture_root stages operator data only. Use the installed CLI for the runner's real child process.
        wrapper = self.base/'qq_cli.py'
        if not wrapper.exists():
            wrapper.write_text("import runpy\nrunpy.run_module('hakimi.tools.qq_cli', run_name='__main__')\n",
                               encoding='utf-8')
        self.store = RouteStore(self.base/'state/official_routes.sqlite3')
        self.store.set('group_openid', 'TEST_GROUP')
        self.person = self.store.actor('PERSON_IN_GROUP')
        self.speaker = self.store.actor('CURRENT_SPEAKER')
        self.api = AsyncMock()
        async def reply(method, path, *args):
            return {'allow_proactive_msg': True} if method == 'GET' else {'id':'PLATFORM_ACK'}
        self.api.call.side_effect = reply
        self.adapter = OfficialAdapter(self.base, {'test_group_number':GROUP[6:], 'allow_proactive':True},
                                       self.api, self.store)

    def payload(self, *, proactive=False, duplicate=False, session=GROUP):
        source, _ = self.store.remember('INPUT', 'REF_INPUT', time.time(), group='TEST_GROUP')
        rows = [dict(message_id='1', user_id=self.person, nickname='杨桃', session_key=session, text='早上好'),
                dict(message_id=source, user_id=self.speaker, nickname='杨桃' if duplicate else '来访者',
                     session_key=session, text='今天天气好' if proactive else '@哈基米 叫一下杨桃')]
        return dict(session_key=session, trigger_id='turn-'+session[6:], recent_context_messages=rows,
                    is_proactive=proactive, wake_mode='冷场主动抛话题' if proactive else '即时唤醒',
                    reply_target_message_id=source)

    @contextmanager
    def take(self, payload, provider):
        ProviderHealth(self.base).set_mode(provider)
        published = TurnCoordinator(self.base).publish(payload)
        self.assertTrue(published.get('accepted'), published)
        with lane_context(published['conversation_lane']):
            coordinator = TurnCoordinator(self.base)
            claim = coordinator.claim_primary() if provider == 'gemini' else coordinator.claim_fallback()
            self.assertTrue(claim and claim.get('token'), claim)
            activate_context(payload, self.base, claim=claim)
            yield coordinator, claim

    def gemini(self, payload, second, *, name='杨桃', first_text=None):
        args = {'name':name}
        if first_text is not None:
            args['text'] = first_text
        steps = [{'functionCall':{'name':'members.mention','args':args}}]
        if second is not None:
            steps.append(second)
        with self.take(payload, 'gemini') as (coordinator, claim):
            def local_model(body, timeout):
                self.assertIn('members.mention', {d['name'] for d in body['tools'][0]['functionDeclarations']})
                return {'candidates':[{'finishReason':'STOP','content':{'role':'model','parts':[steps.pop(0)]}}]}
            env = dict(os.environ, HAKIMI_BASE=str(self.base), HAKIMI_ROOT=str(self.root),
                       HAKIMI_TURN_LANE=lane_id())
            env['PYTHONPATH'] = os.pathsep.join(str(Path(path).resolve()) for path in sys.path if path)
            session = VertexSession([], self.base, env, transport=local_model, image_loader=lambda claim: [])
            result = session.generate('隔离回放', coordinator, claim)
            self.assertTrue(result.get('reply_submitted'), result)
            self.assertEqual(steps, [])

    async def wire(self, payload, expected, *, index=0, count=1):
        rows = [json.loads(line) for line in (self.base/'state/outbox.jsonl').read_text().splitlines()]
        matches = [row for row in rows if row['trigger_id'] == payload['trigger_id']]
        self.assertEqual(len(matches), 1)
        record = matches[0]
        self.assertEqual(len(record['segments']), count)
        self.store.attach_turn(record['trigger_id'], str(payload['reply_target_message_id']))
        path = self.base/'state/delivery_state.json'
        path.write_text(json.dumps({'pending':dict(record, next_segment=index)}))
        self.adapter.cfg['allow_proactive'] = bool(payload['is_proactive'])
        sent = await self.adapter.send(record['session_key'], record['segments'][index])
        self.assertEqual(sent['status'], 'success', sent)
        self.assertTrue(sent['message_id'])
        posts = [item.args for item in self.api.call.await_args_list if item.args[0] == 'POST']
        self.assertEqual(len(posts), index + 1)
        self.assertEqual(posts[-1][1], '/v2/groups/TEST_GROUP/messages')
        body = posts[-1][2]
        if expected:
            self.assertEqual(body['msg_type'], 2, body)
            content = body['markdown']['content']
            self.assertEqual(content.count('<qqbot-at-user'), 1, content)
            self.assertIn('<qqbot-at-user id="PERSON_IN_GROUP" />', content)
            self.assertNotIn('CQ:at', content)
            self.assertNotIn('&#91;', content)
        else:
            self.assertEqual(body['msg_type'], 0, body)
            self.assertNotIn('<qqbot-at-user', body['content'])
        if payload['is_proactive']:
            self.assertNotIn('msg_id', body)
        return body

    async def test_gemini_explicit_ordinary_final(self):
        payload = self.payload()
        self.gemini(payload, {'text':'你来啦。'})
        await self.wire(payload, True)

    async def test_gemini_proactive_reply_send_echoed_marker_once(self):
        payload = self.payload(proactive=True)
        self.gemini(payload, {'functionCall':{'name':'reply.send',
                    'args':{'text':f'[CQ:at,qq={self.person}] 你来啦。'}}})
        await self.wire(payload, True)

    async def test_registered_bot_speaker_does_not_replace_human_addressee(self):
        payload = self.payload()
        bot_registry.platform(self.base, self.speaker, '测试机器人', GROUP)
        self.gemini(payload, None, first_text='你来啦。')
        await self.wire(payload, True)

    async def test_dsh_resolved_member_followed_by_ordinary_reply(self):
        payload = self.payload()
        with self.take(payload, 'dsh') as (coordinator, claim):
            first = parse_response(json.dumps({'action':'tool','tool':'members.mention',
                                               'input':{'name':'杨桃'},'text':''}, ensure_ascii=False))
            async def local_model(prompt, active_claim):
                return parse_response('{"action":"reply","text":"来吃饭啦。"}')
            final = await resolve_tools(first, claim, coordinator, self.base, local_model, {'calls':[]}, {})
            self.assertTrue(submit_fallback_result(final, claim, coordinator, self.base, finish=True)['accepted'])
        await self.wire(payload, True)

    async def test_unknown_member_plain_reply(self):
        payload = self.payload()
        self.gemini(payload, {'text':'我还没找到这位。'}, name='测试代号甲乙丙丁')
        await self.wire(payload, False)

    async def test_gemini_unknown_typed_id_clarifies_without_leaking_id(self):
        payload = self.payload()
        self.gemini(payload, {'text':'@1234567890123456789 签到'}, name='测试代号甲乙丙丁')
        body = await self.wire(payload, False)
        self.assertIn('昵称', body['content'])
        self.assertNotIn('1234567890123456789', body['content'])

    async def test_dsh_unknown_typed_id_clarifies_without_leaking_id(self):
        payload = self.payload()
        with self.take(payload, 'dsh') as (coordinator, claim):
            reply = {'action':'reply', 'text':'@1234567890123456789 签到'}
            self.assertTrue(submit_fallback_result(reply, claim, coordinator, self.base, finish=True)['accepted'])
        body = await self.wire(payload, False)
        self.assertIn('昵称', body['content'])
        self.assertNotIn('1234567890123456789', body['content'])

    async def test_ambiguous_member_plain_reply(self):
        payload = self.payload(duplicate=True)
        self.gemini(payload, {'text':'你说的是哪一位？'})
        await self.wire(payload, False)

    async def test_known_bare_local_id_with_quote_and_second_bubble(self):
        payload = self.payload()
        source = payload['reply_target_message_id']
        self.assertRegex(self.person, r'^[1-9][0-9]{18}$')
        self.store.remember('OTHER_INPUT', 'OTHER_REF', time.time(), group='TEST_GROUP', user_id=self.person)
        with self.take(payload, 'gemini') as (coordinator, claim):
            parts = text_bubbles(f'@{self.person} 到啦。|我把包裹放这儿。')
            parts[0] = f'[CQ:reply,id={source}]' + parts[0]
            receipt = coordinator.submit_reply(claim['token'], parts, 'gemini', reply_to=source)
            self.assertTrue(receipt['accepted'], receipt)
        first = await self.wire(payload, True, count=2)
        self.assertEqual(first['message_reference'], {'message_id':'REF_INPUT'})
        self.assertNotIn('@'+self.person, first['markdown']['content'])
        second = await self.wire(payload, False, index=1, count=2)
        self.assertEqual(second['content'], '我把包裹放这儿。')
        self.assertNotIn('message_reference', second)

    async def test_unseen_bare_local_id_cannot_queue_then_plain_retry_works(self):
        payload = self.payload()
        outsider = self.store.actor('PERSON_ONLY_IN_OTHER_GROUP')
        self.store.remember('OUTSIDE', 'OUTSIDE_REF', time.time(), group='OTHER_GROUP', user_id=outsider)
        with self.take(payload, 'gemini') as (coordinator, claim):
            self.assertFalse(coordinator.submit_reply(claim['token'], text_bubbles(f'@{outsider} 你好。'),
                                                      'gemini')['accepted'])
            outbox = self.base/'state/outbox.jsonl'
            self.assertFalse(outbox.exists() and outbox.read_text().strip())
            self.assertTrue(coordinator.submit_reply(claim['token'], text_bubbles('先说声你好。'),
                                                     'gemini')['accepted'])
        await self.wire(payload, False)

    async def test_unselected_raw_cq_example_stays_plain_even_for_known_member(self):
        payload = self.payload()
        with self.take(payload, 'gemini') as (coordinator, claim):
            example = f'[CQ:at,qq={self.person}] 这是一个格式示例。'
            receipt = coordinator.submit_reply(claim['token'], text_bubbles(example), 'gemini')
            self.assertTrue(receipt['accepted'], receipt)
        body = await self.wire(payload, False)
        self.assertEqual(body['content'], example)

    async def test_takeover_and_new_group_do_not_inherit_selection(self):
        payload = self.payload()
        with self.take(payload, 'gemini') as (coordinator, claim):
            self.assertTrue(mention(self.base, coordinator, claim['token'], 'gemini', '杨桃')['ok'])
            self.assertTrue(coordinator.report_failure(claim['token'], 'fixture'))
            ProviderHealth(self.base).set_mode('dsh')
            replacement = coordinator.claim_fallback()
            self.assertTrue(replacement)
            self.assertFalse(mention(self.base, coordinator, claim['token'], 'gemini', '杨桃')['ok'])
            self.assertFalse(coordinator.submit_reply(claim['token'], ['过期'], 'gemini')['accepted'])
            final = parse_response('{"action":"reply","text":"收到啦。"}')
            self.assertTrue(submit_fallback_result(final, replacement, coordinator, self.base, finish=True)['accepted'])
        await self.wire(payload, False)
        other = self.payload(session='group:43210')
        with self.take(other, 'gemini') as (coordinator, claim):
            self.assertTrue(coordinator.submit_reply(claim['token'], ['别的群。'], 'gemini')['accepted'])
        row = json.loads((self.base/'state/outbox.jsonl').read_text().splitlines()[-1])
        self.assertEqual(row['session_key'], other['session_key'])
        self.assertEqual(row['segments'], ['别的群。'])
