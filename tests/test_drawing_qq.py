"""Drawing intent and delivery with no live model or QQ calls."""

from concurrent.futures import ThreadPoolExecutor
import asyncio
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from hakimi.bridge.delivery_guard import DeliveryGuard
from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.models.provider_health import ProviderHealth
from hakimi.models.vertex_session import VertexSession, declarations
from hakimi.prompt.tool_selection import select_names
from hakimi.tools.drawing_delivery import draw, drawing_in, onebot_image, receipts
from hakimi.tools.drawing_intent import COOLDOWN_NOTE, TOOL, request_id, requested
from hakimi.transport.file_delivery import from_segment, menu, stage


CONFIG = {'enabled': True, 'has_api_key': True}
BRIEF = {'brief': '水彩画，俯瞰旧校园的蓝色夜景，窗旁一串猫爪形灯饰。'}
PICTURE = SimpleNamespace(data=b'synthetic generated PNG payload', mime_type='image/png',
                          models_tried=('fixture-image',))


def message(text, ident='11', user='5', session='group:123', at=None, **extra):
    return dict(text=text, message_id=ident, user_id=user, session_key=session,
                received_at=time.time() if at is None else at, **extra)


def turn(*rows, **extra):
    target = rows[-1]
    return dict(session_key=target['session_key'], reply_target_message_id=target['message_id'],
                recent_context_messages=list(rows), **extra)


class DrawingIntent(unittest.TestCase):
    def test_real_requests_and_art_chat(self):
        for text in (
            '@哈基米 给我画张小猫', '我想让你画个水母', '给我整张图', '你会画猫吗，给我画一个',
            '把这段聊天画成四格漫画', '能不能把这段对话画成漫画', '给这个群设计个头像',
            '麻烦用水彩画张校园夜景', '我想要一张你画的星空', '别搜图，自己画',
            '请画一只戴耳机的猫', '帮我重画这个', '生成一张海报吧', '请画一张「冰河」',
            'Could you draw a sleeping fox?',
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(requested(turn(message(text))))
        for text in (
            '你会画图吗', '画画真有意思', '绘图这门课挺好玩', '来张图', '别画这张了',
            '他说，帮我画一张图', '昨天哈基米画了一只猫', '「帮我画张图」是什么意思',
            '请教我怎么画猫', '给我做个报告', '看看这张画怎么样', '帮我找一张猫图',
            '画猫的教程有哪些', '画猫的提示词怎么写', '画猫的方法是什么', '画猫的步骤',
            '[引用 成员 的消息(id=10): "帮我画一张图"] 请解释', '[群友发图(可用view_file看图): /tmp/a.png]',
            '再来一张吧', '就画这个',
        ):
            with self.subTest(text=text):
                self.assertIsNone(requested(turn(message(text))))

    def test_same_speaker_fresh_followup_attachment_and_cancellation(self):
        previous = message('帮我画这只猫', '10')
        self.assertIsNone(requested(turn(previous, message('今天天气不错'))))
        self.assertIsNone(requested(turn(previous, message('再来一张吧', user='9'))))
        self.assertIsNone(requested(turn(previous, message('就画这个', at=time.time() + 180))))
        self.assertIsNotNone(requested(turn(previous, message('就画这个'))))
        self.assertIsNotNone(requested(turn(message('就画这个', image_paths=['/tmp/current.png']))))
        self.assertIsNone(requested(turn(message('[群友发图]', image_paths=['/tmp/current.png']))))
        source_id = request_id(previous['session_key'], previous)
        delivered = message('【已送达图片】', '12', 'SELF',
                            media={'kind': 'file', 'name': f'_drawing_{source_id}.png'})
        self.assertIsNotNone(requested(turn(previous, delivered, message('换成水彩吧', '13'))))
        self.assertIsNone(requested(turn(previous, delivered, message('换成水彩吧', '13', '9'))))
        cancelled = turn(previous, message('算了不用画了'))
        cancelled['reply_target_message_id'] = '10'
        self.assertIsNone(requested(cancelled))
        unrelated_quote = turn(previous, message('看看这句话', reply_to='10'))
        self.assertIsNone(requested(unrelated_quote))

    def test_visibility_respects_explicit_offline_and_internal_turns(self):
        def tools(payload):
            return select_names(payload, {TOOL, 'reply.send'}, Path('.'))

        with patch('hakimi.tools.drawing.public_settings', return_value=CONFIG):
            self.assertIn(TOOL, tools(turn(message('别搜网图，自己画一张'))))
            self.assertNotIn(TOOL, tools(turn(message('请别再联网，帮我画一只猫'))))
            self.assertNotIn(TOOL, tools(turn(message('请画一只猫'), allow_network=False)))
            self.assertNotIn(TOOL, tools(turn(message('请画一只猫'), internal_review={})))
            self.assertNotIn(TOOL, tools(turn(message('请画一只猫'), is_proactive=True)))
        with patch('hakimi.tools.drawing.public_settings', return_value={'enabled': False}):
            self.assertNotIn(TOOL, tools(turn(message('请画一只猫'))))
        schema = next(tool for tool in declarations({TOOL}) if tool['name'] == TOOL)
        self.assertEqual(list(schema['parametersJsonSchema']['properties']), ['brief'])


class DrawingDelivery(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.coordinator = TurnCoordinator(self.base)
        self.guard = DeliveryGuard(self.base / 'state/delivery_state.json', self.coordinator.outbox_path)
        config = patch('hakimi.tools.drawing.public_settings', return_value=CONFIG)
        config.start()
        self.addCleanup(config.stop)

    def claim(self, row, provider='gemini'):
        published = self.coordinator.publish(turn(row))
        self.assertTrue(published['accepted'])
        if provider == 'dsh':
            ProviderHealth(self.base).set_mode('dsh')
            return self.coordinator.claim_fallback()
        return self.coordinator.claim_primary()

    def send(self, claim, brief=BRIEF):
        return draw(self.base, self.coordinator, claim['token'], claim['provider'], brief)

    def status(self, drawing_id):
        with receipts(self.base) as db:
            return db.execute('SELECT status,reply_id FROM drawings WHERE id=?', (drawing_id,)).fetchone()

    def test_onebot_queue_and_actual_ack_with_concurrent_retry(self):
        claim = self.claim(message('画张校园夜景'))
        started, release = threading.Event(), threading.Event()

        def generate(*args, **kwargs):
            started.set()
            release.wait(timeout=5)
            return PICTURE

        with patch('hakimi.tools.drawing.generate', side_effect=generate) as model, ThreadPoolExecutor() as pool:
            pending = pool.submit(self.send, claim)
            try:
                self.assertTrue(started.wait(timeout=5))
                self.assertEqual(self.send(claim)['error'], 'drawing_already_attempted')
            finally:
                release.set()
            submitted = pending.result(timeout=5)
        self.assertTrue(submitted['ok'])
        self.assertEqual(model.call_args.args, (self.base, BRIEF['brief']))
        self.assertEqual(model.call_args.kwargs['timeout'], 180)
        model.assert_called_once()
        session, segment = self.guard.next_message()
        ident = drawing_in([segment])
        self.assertEqual(self.status(ident), ('queued', submitted['reply_id']))
        path, kind = from_segment(segment, session, self.base)
        self.assertEqual(path.read_bytes(), PICTURE.data)
        self.assertEqual(kind, 1)
        self.assertIn('[CQ:image,file=file:', onebot_image(self.base, session, segment))
        self.assertEqual(menu(session, self.base), [])
        with self.assertRaises(ValueError):
            stage([path.name], claim['payload'], self.base)
        self.guard.begin_attempt()
        self.guard.finish_attempt('success')
        self.assertEqual(self.status(ident), ('delivered', submitted['reply_id']))
        self.assertIsNone(DeliveryGuard(self.guard.state_path, self.guard.outbox_path).next_message())

    def test_official_group_and_private_wire(self):
        from hakimi.transport.official_transport import OfficialAdapter, RouteStore
        for private in (False, True):
            with self.subTest(private=private), tempfile.TemporaryDirectory(dir=self.base) as folder:
                base = Path(folder).resolve()
                (base / 'official_lane_config.json').write_text('{}', encoding='utf-8')
                routes = RouteStore(base / 'state/official_routes.sqlite3')
                routes.set('group_openid', 'GROUP_TEST')
                api = AsyncMock()
                api.upload_file.return_value = 'FILE_INFO'
                api.call.return_value = {'id': 'ACK'}
                adapter = OfficialAdapter(base, {'test_group_number': '123', 'allow_proactive': False}, api, routes)
                if private:
                    route = adapter.ensure_private('PERSON_TEST')
                    session = 'private:' + route['local_id']
                else:
                    route = adapter.route_for_session('group:123')
                    session = 'group:123'
                source_id, _ = routes.remember('incoming', 'REF', time.time(), group=route.get('scope', route['openid']), user_id='5')
                coordinator = TurnCoordinator(base)
                guard = DeliveryGuard(base / 'state/delivery_state.json', coordinator.outbox_path)
                coordinator.publish(turn(message('画张校园夜景', source_id, session=session)))
                claim = coordinator.claim_primary()
                routes.attach_turn(claim['trigger_id'], source_id)
                with patch('hakimi.tools.drawing.generate', return_value=PICTURE):
                    self.assertTrue(draw(base, coordinator, claim['token'], 'gemini', BRIEF)['ok'])
                destination, segment = guard.next_message()
                self.assertEqual(destination, session)
                guard.begin_attempt()
                # All HTTP methods are immediate AsyncMocks, so this does not
                # need an event loop or a loopback socket in the test runner.
                sending = adapter._send_once(session, segment)
                with self.assertRaises(StopIteration) as done:
                    sending.send(None)
                result = done.exception.value
                self.assertEqual(result['status'], 'success')
                guard.finish_attempt(result['status'])
                self.assertEqual(api.upload_file.call_args.args[2], 1)
                self.assertEqual(bool(api.upload_file.call_args.kwargs.get('private')), private)
                self.assertEqual(api.call.call_args.args[2]['media'], {'file_info': 'FILE_INFO'})

    def test_stale_owner_route_missing_and_context_guard_do_not_generate(self):
        claim = self.claim(message('只发一张图给我看看'))
        with patch('hakimi.tools.drawing.generate') as model:
            self.assertEqual(self.send(claim)['error'], 'drawing_not_requested')
            self.assertEqual(self.send(dict(claim, token='another owner'))['error'], 'turn_expired')
            model.assert_not_called()
        self.coordinator.complete(claim['token'])
        claim = self.claim(message('画个水母', '12'))
        (self.base / 'official_lane_config.json').write_text('{}', encoding='utf-8')
        with patch('hakimi.tools.drawing.generate') as model:
            self.assertEqual(self.send(claim)['error'], 'drawing_route_unavailable')
            model.assert_not_called()

    def test_dsh_tool_menu_and_terminal_reply(self):
        from hakimi.models.dsh_tools import execute
        from hakimi.models.fallback_prompt import fallback_prompt

        now = [time.time()]
        self.coordinator.clock = lambda: now[0]
        claim = self.claim(message('帮我画一幅校园夜景'), provider='dsh')
        listing = execute({'tool': 'tools.list', 'input': {}}, claim, self.coordinator, self.base)
        self.assertIn(TOOL, {item['name'] for item in listing['result']['tools']})
        self.assertEqual(fallback_prompt(claim['payload'], self.base).count(TOOL), 1)
        self.assertNotIn(TOOL, fallback_prompt(turn(message('早上好')), self.base))
        def slow_image(base, brief, *, timeout):
            self.assertEqual(timeout, 180)
            now[0] += 179
            self.assertGreater(now[0], claim['deadline'])
            self.assertTrue(self.coordinator.check_token(claim['token'], 'dsh'))
            return PICTURE

        with patch('hakimi.tools.drawing.generate', side_effect=slow_image):
            result = execute({'tool': TOOL, 'input': BRIEF}, claim, self.coordinator, self.base)
        self.assertTrue(result['ok'])
        self.assertEqual(result['terminal'], {'action': 'silent', 'text': ''})
        self.assertTrue(claim['_reply_submitted'])

    def test_fallback_drawing_respects_turn_expiry(self):
        now = time.time()
        self.coordinator = TurnCoordinator(self.base, clock=lambda: now, ttl_seconds=100)
        claim = self.claim(message('画张校园夜景'), provider='dsh')
        with patch('hakimi.tools.drawing.generate', return_value=PICTURE) as model:
            self.assertTrue(self.send(claim)['ok'])
        self.assertEqual(model.call_args.kwargs['timeout'], 98)

    def test_known_cooldown_reaches_only_a_drawing_turn(self):
        from hakimi.models.fallback_prompt import fallback_prompt
        from hakimi.prompt.prompt_context import render_vertex_prompt
        from hakimi.tools.drawing_accounting import reserve, quota_status

        payload = turn(message('帮我画张校园夜景'))
        reserve(self.base, {})
        reserve(self.base, {})
        before = quota_status(self.base)
        self.assertIsNotNone(before['available_at'])
        self.assertNotIn(TOOL, select_names(payload, {TOOL}, self.base))
        for render in (fallback_prompt, render_vertex_prompt):
            self.assertIn(COOLDOWN_NOTE, render(payload, self.base))
            self.assertNotIn(COOLDOWN_NOTE, render(turn(message('聊聊校园夜景')), self.base))
        self.assertEqual(before, quota_status(self.base))

    def test_primary_composes_reply_after_simultaneous_quota_hit(self):
        from hakimi.tools.drawing import DrawingError

        claim = self.claim(message('帮我画张校园夜景'))
        reply = '画笔正歇着呢，但那排路灯我想画成猫爪。'
        def answer(part):
            return {'candidates': [{'finishReason': 'STOP', 'content': {'role': 'model', 'parts': [part]}}]}
        transport = Mock(side_effect=[
            answer({'functionCall': {'name': TOOL, 'args': BRIEF}}), answer({'text': reply})])
        session = VertexSession([], self.base, {}, transport=transport, image_loader=lambda _: [])
        def tool(name, args, turn_claim, timeout):
            if name == 'reply.send':
                return {'ok': self.coordinator.submit_reply(turn_claim['token'], [args['text']], 'gemini')['accepted']}
            return session._tool(name, args, turn_claim, timeout)
        session.tool_runner = tool
        denied = DrawingError('drawing_window_images', '内部时间 13:45:25')
        with patch('hakimi.tools.drawing.generate', side_effect=denied) as model:
            session.generate('群友请求画夜景', self.coordinator, claim)
        model.assert_called_once()
        wire = transport.call_args.args[0]['contents'][-1]['parts'][0]['functionResponse']['response']
        self.assertEqual(wire, {'error': 'drawing_cooldown', 'feedback': COOLDOWN_NOTE})
        self.assertEqual(self.guard.next_message(), ('group:123', reply))

    def test_fallback_composes_reply_after_daily_quota_hit(self):
        from hakimi.models.dsh_tools import execute, resolve_tools
        from hakimi.models.fallback_commit import submit_fallback_result
        from hakimi.tools.drawing import DrawingError

        claim = self.claim(message('帮我画张校园夜景'), provider='dsh')
        request = {'action': 'tool', 'tool': TOOL, 'input': BRIEF, 'text': ''}
        reply = '这幅我先欠着，窗边的猫爪灯可别拆。'
        chat = AsyncMock(return_value={'action': 'reply', 'text': reply})
        with patch('hakimi.tools.drawing.generate', side_effect=DrawingError('drawing_day_images', '内部时间 00:00:00')) as model:
            result = asyncio.run(resolve_tools(request, claim, self.coordinator, self.base, chat, {'calls': []}, {}))
            again = execute(request, claim, self.coordinator, self.base)
        model.assert_called_once()
        self.assertEqual(again, {'error': 'drawing_cooldown', 'feedback': COOLDOWN_NOTE})
        self.assertIn(COOLDOWN_NOTE, chat.await_args.args[0])
        self.assertNotIn('内部时间', chat.await_args.args[0])
        self.assertIsNone(self.guard.next_message())
        self.assertTrue(submit_fallback_result(result, claim, self.coordinator, self.base)['accepted'])
        self.assertEqual(self.guard.next_message(), ('group:123', reply))

    def test_native_tool_list_does_not_reopen_hidden_drawing(self):
        def response(name=None, **args):
            part = {'functionCall': {'name': name, 'args': args}} if name else {'text': '早上好'}
            return {'candidates': [{'finishReason': 'STOP', 'content': {'role': 'model', 'parts': [part]}}]}

        provider = Mock(side_effect=[response('tools.list'), response(TOOL, **BRIEF), response()])
        claim = self.claim(message('普通聊天'))
        session = VertexSession([], self.base, {}, transport=provider, image_loader=lambda _: [])
        actual = session._tool
        session.tool_runner = Mock(side_effect=lambda name, args, claim, timeout:
                                   {'ok': True} if name == 'reply.send' else actual(name, args, claim, timeout))
        result = session.generate('聊天', self.coordinator, claim)
        self.assertTrue(result['reply_submitted'])
        self.assertEqual([call.args[0] for call in session.tool_runner.call_args_list], ['tools.list', 'reply.send'])
        for call in provider.call_args_list:
            wire = call.args[0]
            self.assertNotIn(TOOL, {tool['name'] for group in wire['tools'] for tool in group['functionDeclarations']})

    def test_cli_meta_and_execution_only_for_active_request(self):
        from hakimi.prompt.prompt_context import activate_context
        from hakimi.tools import qq_cli

        claim = self.claim(message('帮我画张图'))
        activate_context(claim['payload'], self.base, claim=claim)
        args = {'session': 'group:123', 'turn': claim['token']}
        with patch.object(qq_cli, 'BASE_DIR', self.base), patch('hakimi.tools.drawing.generate', return_value=PICTURE):
            listing = io.StringIO()
            with contextlib.redirect_stdout(listing):
                qq_cli._cmd_call(SimpleNamespace(call_name='tools.list', **args))
            self.assertIn(TOOL, {row['name'] for row in json.loads(listing.getvalue())['result']['tools']})
            result = io.StringIO()
            with contextlib.redirect_stdout(result):
                qq_cli._cmd_call(SimpleNamespace(call_name=TOOL, **BRIEF, **args))
            self.assertEqual(json.loads(result.getvalue())['status'], 'queued')


if __name__ == '__main__':
    unittest.main()
