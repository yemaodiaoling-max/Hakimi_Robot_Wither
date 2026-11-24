"""Official QQ fixtures only: no actual credentials, model requests or QQ sends."""
import asyncio
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hakimi.transport import official_test as control
from hakimi.transport import official_transport as transport
from hakimi.transport import official_waiter
from test_delivery_guard import OfflineEventLoop
from _repo import repo_file


def fixture_root(path):
    for name in ("config.json", "group_profiles.json", "memory.md",
            "official_test_config.json", "official_memory_review.json"):
        shutil.copyfile(repo_file(name), path / name)
    (path / "state").mkdir()
    snapshots = path / 'state/memory_migration_sources'
    snapshots.mkdir()
    for snapshot in (ROOT / 'tests/fixtures/memory_migration_sources').glob('group_*.md'):
        shutil.copyfile(snapshot, snapshots / snapshot.name)
    # Fixtures must not follow the operator's current live test group.
    cfg = json.loads((path / 'official_test_config.json').read_text(encoding='utf-8'))
    cfg.update(test_group_number='850245632', enabled=False, operation_mode='test', allow_proactive=False, allow_private_chat=False)
    (path / 'official_test_config.json').write_text(json.dumps(cfg), encoding='utf-8')
    (path / "stickers/collected").mkdir(parents=True)
    return control.prepare(path)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix="official_store_")
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.
        self.store = transport.RouteStore(Path(self.temp.name) / "routes.sqlite3", clock=lambda: self.now)

    def test_pairing_requires_local_challenge_and_actual_group_admin(self):
        code = self.store.arm()
        data = {"group_openid": "OFFICIAL_GROUP", "author": {"member_role": "member"}}
        self.assertFalse(self.store.bind(data, "/绑定测试群 " + code))
        data["author"]["member_role"] = "admin"
        self.assertFalse(self.store.bind(data, "/绑定测试群 deadbeef"))
        self.assertTrue(self.store.bind(data, "/绑定测试群 " + code))
        self.assertEqual(self.store.get("group_openid"), "OFFICIAL_GROUP")
        data["group_openid"] = "OTHER_GROUP"
        self.assertFalse(self.store.bind(data, "/绑定测试群 " + code))
        self.assertEqual(self.store.get("group_openid"), "OFFICIAL_GROUP")

    def test_quote_displacement_ignores_three_supplements_but_counts_four(self):
        target,_=self.store.remember('question','ref',self.now,group='g',user_id='u')
        self.assertIsNone(self.store.quote_reference(target,'g'))
        for i in range(3):
            self.store.remember('supplement'+str(i),'r'+str(i),self.now,group='g',user_id='u')
            self.assertIsNone(self.store.quote_reference(target,'g'))
        self.store.remember('other-group','r',self.now,group='other',user_id='other')
        self.assertIsNone(self.store.quote_reference(target,'g'))
        self.store.remember('fourth','r4',self.now,group='g',user_id='u')
        self.assertEqual(self.store.quote_reference(target,'g'),{'message_id':'ref'})
        self.assertIsNone(self.store.quote_reference(target,'other'))

    def test_quote_counts_other_people_and_own_messages_with_identical_timestamps(self):
        for direction,user in [('in','other'),('out','')]:
            group=direction
            target,_=self.store.remember('q'+group,'ref',self.now,group=group,user_id='u')
            self.store.remember('later'+group,'r',self.now,direction,group,user)
            self.assertEqual(self.store.quote_reference(target,group),{'message_id':'ref'})

    def test_expired_challenge_and_unpaired_group_have_no_send_authority(self):
        code = self.store.arm()
        self.now += 901
        self.assertFalse(self.store.bind({"group_openid": "G", "author": {"member_role": "owner"}}, "/绑定测试群 " + code))
        with self.assertRaises(transport.OfficialError):
            self.store.reserve("reply:0", None)

    def test_unused_placeholder_app_id_can_be_corrected_and_old_pairing_is_invalidated(self):
        self.store.prepare_identity('190592927', '850245632')
        self.store.arm()
        self.store.prepare_identity('8725485240', '850245632')
        self.assertEqual(self.store.get('app_id'), '8725485240')
        self.assertIsNone(self.store.get('pairing'))

    def test_bound_or_used_app_id_cannot_be_changed(self):
        self.store.prepare_identity('8725485240', '850245632')
        self.store.set('group_openid', 'BOUND_GROUP')
        with self.assertRaises(transport.OfficialError):
            self.store.prepare_identity('1905925928', '850245632')
        self.store.set('group_openid', None)
        self.store.remember('INPUT', 'REFIDX_input', self.now)
        with self.assertRaises(transport.OfficialError):
            self.store.prepare_identity('1905925928', '850245632')
        self.assertEqual(self.store.get('app_id'), '8725485240')

    def test_five_reply_sequences_durable_dedup_and_no_silent_active_downgrade(self):
        local, _ = self.store.remember("source", "REFIDX_source", self.now)
        source = self.store.message(local)
        for index in range(5):
            self.assertEqual(self.store.reserve(f"reply:{index}", source), {"msg_id": "source", "msg_seq": index + 1})
        with self.assertRaises(transport.OfficialError):
            self.store.reserve("reply:5", source)
        restored = transport.RouteStore(self.store.path, clock=lambda: self.now)
        with self.assertRaises(transport.OfficialError) as exc:
            restored.reserve("reply:0", source)
        self.assertTrue(exc.exception.ambiguous)
        self.now += 300
        with self.assertRaises(transport.OfficialError):
            restored.reserve("late:0", source)
        self.assertEqual(restored.reserve("operator-enabled-active:0", source, True), {})


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    loop_factory = OfflineEventLoop

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix="official_adapter_")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / "state/img_cache").mkdir(parents=True)
        (self.base / "stickers").mkdir()
        self.now = time.time()
        self.store = transport.RouteStore(self.base / "state/routes.sqlite3", clock=lambda: self.now)
        self.store.set("group_openid", "GROUP_TEST")
        self.api = AsyncMock()
        self.api.call.return_value = {"id": "BOT_REPLY", "ext_info": {"ref_idx": "REFIDX_reply"}}
        self.adapter = transport.OfficialAdapter(self.base, {"test_group_number": "850245632", "allow_proactive": False},
                                                self.api, self.store, clock=lambda: self.now)
        self.adapter.self_remote = "BOT_SELF"

    def data(self, remote="INPUT_1", **kwargs):
        return {"id": remote, "group_openid": "GROUP_TEST", "content": "hello",
            "author": {"id": "MEMBER_1", "username": "示例群友", "bot": False},
            "timestamp": datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
            "message_scene": {"ext": ["msg_idx=REFIDX_" + remote, "auth_token=never_pass_to_model"]}, **kwargs}

    async def test_at_and_full_group_events_normalize_with_untrusted_cq_escaped(self):
        event = await self.adapter.translate("GROUP_AT_MESSAGE_CREATE", self.data(content="[CQ:at,qq=123] literal"))
        self.assertIn("[CQ:at,qq=" + transport.SELF_ALIAS + "]", event["raw_message"])
        self.assertIn("&#91;CQ:at,qq=123&#93;", event["raw_message"])
        self.assertNotIn("never_pass_to_model", json.dumps(event))
        self.assertNotIn("GROUP_TEST", json.dumps(event))
        self.assertEqual(event["group_id"], 850245632)
        full = await self.adapter.translate("GROUP_MESSAGE_CREATE", self.data("INPUT_2", content="<@BOT_SELF> 为什么"))
        self.assertIn("[CQ:at,qq=" + transport.SELF_ALIAS + "]", full["raw_message"])
        self.assertNotEqual(full["message_id"], event["message_id"])

    async def test_feeding_binding_uses_actual_official_actor_and_not_shared_nickname(self):
        from hakimi.tools import feeding
        code = feeding.arm_binding(self.base)
        async def receive(remote, text, member='MEMBER_1'):
            event = await self.adapter.translate('GROUP_AT_MESSAGE_CREATE', self.data(remote, content=text,
                author={'id':member,'username':'同名成员','bot':False}))
            return feeding.record_interaction(event['raw_message'], 'group:'+str(event['group_id']),
                event['user_id'],event['message_id'],self.base,self_id=event['self_id'])
        self.assertTrue(await receive('BIND','/绑定投喂 '+code))
        self.assertFalse(await receive('OTHER','/投喂罐头',member='MEMBER_2'))
        self.assertTrue(await receive('FEED','/投喂 金枪鱼罐头 2'))
        state=json.loads((self.base/'state/feeding.json').read_text(encoding='utf-8'))
        self.assertEqual(state['owner_user_id'],self.store.actor('MEMBER_1'))
        self.assertEqual(state['total'],2)

    async def test_wrong_group_disabled_private_duplicate_and_stale_inputs_are_not_routed(self):
        self.assertIsNone(await self.adapter.translate("C2C_MESSAGE_CREATE", self.data()))
        self.assertIsNone(await self.adapter.translate("GROUP_MESSAGE_CREATE", self.data(group_openid="WRONG")))
        self.assertIsNone(await self.adapter.translate("GROUP_MESSAGE_CREATE", self.data(timestamp="2020-01-01T00:00:00Z")))
        original = "[群聊的聊天记录]\n=== 消息 1 ===\n[消息内容] 群友204的原话\n第二行"
        bot_message = await self.adapter.translate("GROUP_MESSAGE_CREATE", self.data("BOT_MESSAGE", content=original,
            author={"id": "BOT", "bot": True, "username": "群友204"}, message_scene={"source": "ai_search"}))
        self.assertEqual(bot_message["raw_message"], transport.cq_escape(original))
        self.assertIsNotNone(await self.adapter.translate("GROUP_MESSAGE_CREATE", self.data()))
        self.assertIsNone(await self.adapter.translate("GROUP_MESSAGE_CREATE", self.data()))

    async def test_worker_runtime_failure_restarts_but_configuration_failure_stops(self):
        from types import SimpleNamespace
        runtime=self.base/'official_test_runtime'
        (runtime/'state').mkdir(parents=True)
        (self.base/'state/official_appsecret.dpapi').touch()
        processes=[]
        for code in (2,78):
            proc=SimpleNamespace(returncode=None)
            async def finish(proc=proc,code=code):
                proc.returncode=code
                return code
            proc.wait=finish
            processes.append(proc)
        with patch.object(control,'settings',return_value={'enabled':True,'test_group_number':'850245632'}), \
             patch.object(control,'read_secret',return_value='fixture-only'), \
             patch.object(control.asyncio,'create_subprocess_exec',AsyncMock(side_effect=processes)) as spawn, \
             patch.object(control.asyncio,'sleep',AsyncMock()) as sleep, \
             patch('codex_help.report_fault'):
            await control.serve(self.base)
        self.assertEqual(spawn.await_count,2)
        sleep.assert_awaited_once_with(2)
        marker=json.loads((self.base/'state/official_supervisor.json').read_text(encoding='utf-8'))
        self.assertEqual(marker['state'],'stopped_check_configuration')

    async def test_nested_crash_preserves_cause_and_classifies_fatal_codes(self):
        from hakimi.transport import official_lane
        from hakimi.features.codex_help import exception_details
        ordinary=ExceptionGroup('private provider body',[PermissionError(13,'secret')])
        self.assertFalse(official_lane.configuration_failure(ordinary))
        fatal=ExceptionGroup('private body',[transport.OfficialError('GATEWAY_REQUIRES_CONFIGURATION_4915')])
        self.assertTrue(official_lane.configuration_failure(fatal))
        details=exception_details(ordinary)
        self.assertEqual(details['causes'][0]['error_type'],'PermissionError')
        self.assertNotIn('secret',json.dumps(details))
        self.assertNotIn('private',json.dumps(details))

    async def test_state_replace_recovers_from_brief_windows_reader_conflict(self):
        target=self.base/'state/atomic.json'
        transport.fixed_json(target,{'old':True})
        original=Path.replace
        calls=[]
        def replace(path,destination):
            calls.append(path)
            if len(calls)<3:
                raise PermissionError(13,'reader still holds file')
            return original(path,destination)
        with patch.object(Path,'replace',replace),patch('runtime_io.time.sleep') as pause:
            transport.fixed_json(target,{'new':True})
        self.assertEqual(len(calls),3)
        self.assertEqual(pause.call_count,2)
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')),{'new':True})

    async def test_receive_all_uses_platform_self_mention_despite_different_ready_id(self):
        event = await self.adapter.translate('GROUP_MESSAGE_CREATE', self.data(content='<@GROUP_BOT> question',
            mentions=[{'id':'GROUP_BOT', 'is_you':True}, {'id':'OTHER_BOT', 'bot':True, 'is_you':False}]))
        self.assertEqual(event['raw_message'], '[CQ:at,qq=' + transport.SELF_ALIAS + '] question')
        # A later event without mentions still recognizes the verified group ID after restart.
        restarted = transport.OfficialAdapter(self.base, self.adapter.cfg, self.api, self.store, clock=lambda:self.now)
        later = await restarted.translate('GROUP_MESSAGE_CREATE', self.data('INPUT_2', content='<@GROUP_BOT> again'))
        self.assertIn('[CQ:at,qq=' + transport.SELF_ALIAS + ']', later['raw_message'])
        self.assertIsNone(await restarted.translate('GROUP_MESSAGE_CREATE', self.data('SELF_ECHO', author={'id':'GROUP_BOT'})))

    async def test_other_mentions_plain_messages_and_untrusted_flags_do_not_trigger_self_at(self):
        for index, mentions in enumerate(([{'id':'OTHER', 'bot':True, 'is_you':False}],
                                         [{'id':'OTHER', 'is_you':'true'}], [], None)):
            event = await self.adapter.translate('GROUP_MESSAGE_CREATE', self.data(str(index),
                content='<@OTHER> hello', mentions=mentions))
            self.assertNotIn('[CQ:at,qq=' + transport.SELF_ALIAS + ']', event['raw_message'])
        plain = await self.adapter.translate('GROUP_MESSAGE_CREATE', self.data('PLAIN', content='hello'))
        self.assertNotIn('[CQ:at', plain['raw_message'])
        missing_marker = await self.adapter.translate('GROUP_MESSAGE_CREATE', self.data('NO_MARKER', content='hello',
            mentions=[{'id':'GROUP_BOT', 'is_you':True}]))
        self.assertIn('[CQ:at,qq=' + transport.SELF_ALIAS + ']', missing_marker['raw_message'])

    async def test_pairing_learns_self_identity_only_in_the_bound_group(self):
        self.store.set('group_openid', None)
        code = self.store.arm()
        self.assertIsNone(await self.adapter.translate('GROUP_MESSAGE_CREATE', self.data(content='<@GROUP_BOT> /绑定测试群 ' + code,
            author={'id':'OWNER','member_role':'owner'}, mentions=[{'id':'GROUP_BOT','is_you':True}])))
        self.assertEqual(self.store.get('group_self_remote_ids'), ['GROUP_BOT'])
        await self.adapter.translate('GROUP_MESSAGE_CREATE', self.data('WRONG', group_openid='OTHER_GROUP',
            mentions=[{'id':'WRONG_SELF','is_you':True}]))
        self.assertEqual(self.store.get('group_self_remote_ids'), ['GROUP_BOT'])

    async def test_image_error_and_forwarded_message_do_not_grant_arbitrary_file_access(self):
        item = self.data(content="", message_scene=None, attachments=[{"content_type": "image/png", "url": "http://127.0.0.1/secret"}],
                         msg_elements=[{"author": {"username": "甲"}, "content": "[CQ:image,file=C:/secret]"}])
        result = await self.adapter.translate("GROUP_MESSAGE_CREATE", item)
        self.assertIn("&#91;CQ:image,file=C:/secret&#93;", result["raw_message"])
        self.assertIn("未取得图片内容", result["raw_message"])

    async def test_at_quote_keeps_the_request_and_its_context_separate(self):
        current = "我的第一行\n我的第二行"
        envelope = ("=== 消息 1 ===\n[消息内容] 刚刚发送的答复\n\n"
                    "=== 消息 2 ===\n[消息内容] 另一条发言\n[关联消息]\n--- 第1条 ---\n"
                    "    [消息内容] 引用里的第一行\n    第二行\n    [发送者] 被引用者\n"
                    "    [消息类型] 引用消息\n\n"
                    "=== 消息 3 ===\n[发送者] 甲\n[消息内容] [CQ:at,qq=123] 以前的提问\n\n"
                    "=== 消息 4 ===\n[附件1] 类型:动图 文件名:例.gif URL:https://example.org/?rkey=hidden")
        receipts = [
            {'session_key': 'group:850245632', 'at': self.now-1, 'text': '刚刚发送的答复'},
            {'session_key': 'group:99', 'at': self.now-1, 'text': '另一条发言'},
            {'session_key': 'group:850245632', 'at': self.now+10, 'text': '另一条发言'},
        ]
        (self.base / 'state/sent_messages.jsonl').write_text(
            ''.join(json.dumps(r) + '\n' for r in receipts), encoding='utf-8')
        packet = self.data(content=current, message_type=103, msg_elements=[{'content': envelope}])
        event = await self.adapter.translate('GROUP_AT_MESSAGE_CREATE', packet)
        self.assertEqual(event['raw_message'], f'[CQ:at,qq={transport.SELF_ALIAS}] ' + current)
        self.assertEqual(event['sender']['nickname'], '示例群友')
        rows = event['qq_prior_messages']
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]['same_text_as'], '哈基米在本群的已发送回执')
        self.assertEqual(rows[0]['speaker'], 'QQ未标注')
        self.assertEqual(rows[1], {'speaker': 'QQ未标注', 'text': '另一条发言',
                                   'quotes': [{'speaker': '被引用者', 'text': '引用里的第一行\n第二行'}]})
        self.assertEqual(rows[2], {'speaker': '甲', 'text': '[CQ:at,qq=123] 以前的提问'})
        self.assertEqual(rows[3]['attachments'], ['附件（动图）：例.gif'])
        self.assertNotIn('rkey', str(event))
        self.assertIsNone(await self.adapter.translate('GROUP_AT_MESSAGE_CREATE', packet))

    async def test_bare_at_with_context_remains_one_mention_and_preserves_other_elements(self):
        envelope = {'content': '=== 消息 1 ===\n[消息内容] 前一句'}
        event = await self.adapter.translate('GROUP_AT_MESSAGE_CREATE', self.data(
            content='', message_type='103', msg_elements=[envelope]))
        self.assertEqual(event['raw_message'].strip(), f'[CQ:at,qq={transport.SELF_ALIAS}]')
        self.assertEqual([r['text'] for r in event['qq_prior_messages']], ['前一句'])
        current = '=== 消息 1 ===\n[消息内容] 我正在讨论这段格式'
        other = {'author': {'username': '乙'}, 'content': '随附节点'}
        event = await self.adapter.translate('GROUP_AT_MESSAGE_CREATE', self.data(
            'MIXED', content=current, message_type=103, msg_elements=[envelope, other]))
        self.assertIn(transport.cq_escape(current), event['raw_message'])
        self.assertIn('乙：随附节点', event['raw_message'])
        self.assertNotIn('前一句', event['raw_message'])
        forward = await self.adapter.translate('GROUP_AT_MESSAGE_CREATE', self.data(
            'FORWARD', content='看这段转发', message_type=102, msg_elements=[envelope]))
        self.assertIn('前一句', forward['raw_message'])
        self.assertNotIn('qq_prior_messages', forward)

    async def test_inline_forward_keeps_caption_speakers_and_nested_text(self):
        item = self.data(content="帮忙看看这段记录", msg_elements=[
            {"type": "node", "data": {"author": {"username": "甲", "bot": True}, "content": "先有这个条件"}},
            {"type": "node", "data": {"nickname": "乙", "content": [
                {"type": "text", "data": {"text": "再推下一步"}}]}},
            {"type": "forward", "data": {"messages": [{"type": "node", "data": {"content": {"nodes": [
                {"author": {"username": "丙", "bot": True}, "content": "继续保留这一句"},
            ]}}}]}}])
        result = await self.adapter.translate("GROUP_MESSAGE_CREATE", item)
        for text in ("帮忙看看这段记录", "甲", "乙", "先有这个条件", "再推下一步", "丙：继续保留这一句"):
            self.assertIn(text, result["raw_message"])

    async def test_qq_record_envelope_preserves_speakers_text_and_attachments(self):
        element = {"content": "[群聊的聊天记录]\n=== 消息 1 ===\n[发送者] 群友204\n"
                              "[消息内容] 第一行\n第二行\n[附件1] 类型:图片 文件名:例图.png\n\n"
                              "=== 消息 2 ===\n[消息内容] https://example.org/\n[发送者] 群友\n\n"
                              "=== 消息 3 ===\n[发送者] 群友204\n[附件1] 类型:图片 文件名:仅附件.jpg"}
        event = await self.adapter.translate("GROUP_MESSAGE_CREATE", self.data(message_type=102,
            content="看这段记录", msg_elements=[element]))
        self.assertEqual(event["raw_message"], "看这段记录\n群友204：第一行\n第二行\n附件（图片）：例图.png"
                                               "\n\n群友：https://example.org/\n\n群友204：附件（图片）：仅附件.jpg")
        quote = await self.adapter.translate("GROUP_MESSAGE_CREATE", self.data("QUOTE", message_type=103, content=" ",
            msg_elements=[{"content": "=== 消息 1 ===\n[消息内容] 被引用正文\n独立第二行\n"
                                      "[消息类型] 引用消息\n[关联消息]\n旧引用历史"}]))
        self.assertEqual(quote["raw_message"], "被引用正文\n独立第二行")
        caption = await self.adapter.translate("GROUP_MESSAGE_CREATE", self.data("CAPTION", message_type=102,
            content="随消息发的原话\n[群聊的聊天记录]\n=== 消息 1 ===\n"
                    "[消息内容] 转发者原话\n[发送者] 群友204\n[消息类型] 引用消息\n[关联消息]\n"
                    "--- 第1条 ---\n    [消息内容] 关联内容\n    第二行\n    [消息类型] 普通文本\n\n"
                    "=== 消息 2 ===\n无法识别的包装项\n\n=== 消息 3 ===\n[发送者] 群友\n"
                    "[附件1] 类型:图片 文件名:附件.png 尺寸:800×600 大小:83KB URL:https://qq.com/image?rkey=private"))
        self.assertEqual(caption["raw_message"], "随消息发的原话\n群友204：转发者原话\n引用：关联内容\n第二行"
                                                "\n\n群友：附件（图片）：附件.png\n\n"
                                                + transport.cq_escape("[部分 QQ 聊天记录未能读取]"))

    async def test_platform_controls_and_card_previews_do_not_supply_chat_text(self):
        preview = {"prompt": "预览不当聊天", "callback_data": "控制参数",
                   "fields": {"preview": "https://www.bilibili.com/video/BV1234567890"}}
        self.assertIsNone(await self.adapter.translate("INTERACTION_CREATE", self.data(content="按钮事件")))
        self.assertIsNone(await self.adapter.translate("GROUP_AT_MESSAGE_CREATE", self.data("CONTROL",
            content={"prompt": "控制正文"}, ark_data=preview, msg_elements=[{"content": "无作者协议包装"}])))
        ping = await self.adapter.translate("GROUP_AT_MESSAGE_CREATE", self.data("PING", content=""))
        self.assertIn("[CQ:at,qq=" + transport.SELF_ALIAS + "]", ping["raw_message"])
        event = await self.adapter.translate("GROUP_MESSAGE_CREATE", self.data("CARD", content="预览不当聊天",
            ark_data={**preview, "fields": {"title": "真实标题", "desc": "卡片文字", "jump_url": "https://example.org"}}))
        self.assertEqual(event["raw_message"], "预览不当聊天\n真实标题\n卡片文字\nhttps://example.org")
        embed = {"title": "展示标题", "description": "正式描述", "prompt": "平台预览",
                 "fields": [{"name": "项目", "value": "数值"}]}
        event = await self.adapter.translate("GROUP_MESSAGE_CREATE", self.data("EMBED", content="展示标题",
            author={"id": "BOT", "bot": True, "username": "群友204"}, embeds=[embed]))
        self.assertEqual(event["raw_message"], "展示标题\n正式描述\n项目\n数值")
        self.assertIsNone(await self.adapter.translate("GROUP_AT_MESSAGE_CREATE", self.data("EMBED_PREVIEW",
            content="", embeds=[{"prompt": "平台预览"}])))

    async def enqueue(self, text="answer"):
        event = await self.adapter.translate("GROUP_AT_MESSAGE_CREATE", self.data())
        self.store.attach_turn("turn", str(event["message_id"]))
        pending = {"trigger_id": "turn", "reply_id": "r1", "session_key": "group:850245632",
                   "next_segment": 0, "segments": [text]}
        (self.base / "state/delivery_state.json").write_text(json.dumps({"pending": pending}), encoding="utf-8")
        return event

    async def test_passive_authority_does_not_force_visible_quote(self):
        await self.enqueue()
        result = await self.adapter.send("group:850245632", "answer")
        self.assertEqual(result["status"], "success")
        body = self.api.call.call_args.args[2]
        self.assertEqual(body["msg_id"], "INPUT_1")
        self.assertEqual(body["msg_seq"], 1)
        self.assertNotIn("message_reference", body)
        duplicate = await self.adapter.send("group:850245632", "answer")
        self.assertEqual(duplicate["status"], "unknown")
        self.assertEqual(self.api.call.await_count, 1)

    async def test_allowed_normal_send_keeps_body_and_avoids_passive_reply_metadata(self):
        self.adapter.cfg['allow_proactive']=True
        async def response(method,path,*args):
            return {'allow_proactive_msg':True} if method=='GET' else {'id':'PLAIN_REPLY'}
        self.api.call.side_effect=response
        event=await self.enqueue()
        result=await self.adapter.send('group:850245632','answer')
        self.assertEqual(result['status'],'success')
        body=self.api.call.call_args.args[2]
        self.assertEqual(body,{'msg_type':0,'content':'answer'})
        self.assertEqual(self.store.message(str(event['message_id']))['seq'],0)

    async def test_permission_hot_updates_between_replies_and_is_read_once_per_batch(self):
        self.adapter.cfg['allow_proactive']=True
        allowed=False
        reads=[]
        posts=[]
        async def response(method,path,*args):
            if method=='GET':
                reads.append(path)
                return {'allow_proactive_msg':allowed}
            posts.append(args[0])
            return {'id':'REPLY_'+str(len(posts))}
        self.api.call.side_effect=response
        await self.enqueue('first')
        path=self.base/'state/delivery_state.json'
        pending=json.loads(path.read_text())['pending']
        pending['segments']=['first','second']
        path.write_text(json.dumps({'pending':pending}))
        self.assertEqual((await self.adapter.send('group:850245632','first'))['status'],'success')
        pending['next_segment']=1
        path.write_text(json.dumps({'pending':pending}))
        self.assertEqual((await self.adapter.send('group:850245632','second'))['status'],'success')
        self.assertEqual(len(reads),1)
        self.assertEqual([r['msg_seq'] for r in posts],[1,2])
        allowed=True
        pending.update(reply_id='next-reply',next_segment=0,segments=['third'])
        path.write_text(json.dumps({'pending':pending}))
        self.assertEqual((await self.adapter.send('group:850245632','third'))['status'],'success')
        self.assertEqual(len(reads),2)
        self.assertEqual(posts[-1],{'msg_type':0,'content':'third',
                                   'message_reference':{'message_id':'REFIDX_INPUT_1'}})

    async def test_permission_lookup_failure_preserves_passive_reply(self):
        self.adapter.cfg['allow_proactive']=True
        async def response(method,path,*args):
            if method=='GET':raise transport.OfficialError('HTTP_403')
            return {'id':'PASSIVE_REPLY'}
        self.api.call.side_effect=response
        await self.enqueue()
        self.assertEqual((await self.adapter.send('group:850245632','answer'))['status'],'success')
        self.assertEqual(self.api.call.call_args.args[2]['msg_id'],'INPUT_1')

    async def test_ambiguous_send_is_not_retried_and_never_changes_to_active(self):
        await self.enqueue()
        self.api.call.side_effect = transport.OfficialError("TIMEOUT", ambiguous=True)
        self.assertEqual((await self.adapter.send("group:850245632", "answer"))["status"], "unknown")
        self.assertEqual((await self.adapter.send("group:850245632", "answer"))["status"], "unknown")
        self.assertEqual(self.api.call.await_count, 1)

    async def test_overall_send_timeout_is_unknown_and_reserved_request_cannot_repeat(self):
        await self.enqueue()
        async def stalled(*_): await asyncio.sleep(30)
        self.api.call.side_effect = stalled
        result = await self.adapter.send('group:850245632', 'answer', timeout=.01)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual((await self.adapter.send('group:850245632', 'answer'))['status'], 'unknown')
        self.assertEqual(self.api.call.await_count, 1)

    async def test_expired_source_foreign_session_and_arbitrary_sticker_fail_without_post(self):
        await self.enqueue()
        self.assertEqual((await self.adapter.send("group:123", "answer"))["status"], "rejected")
        self.now += 300
        self.assertEqual((await self.adapter.send("group:850245632", "answer"))["status"], "rejected")
        self.api.call.assert_not_awaited()

    async def test_explicit_target_is_quoted_only_after_intervening_message(self):
        event = await self.enqueue()
        text = f"[CQ:reply,id={event['message_id']}]answer"
        path = self.base / "state/delivery_state.json"
        state = json.loads(path.read_text())
        state["pending"]["segments"] = [text]
        path.write_text(json.dumps(state))
        await self.adapter.translate('GROUP_MESSAGE_CREATE',self.data('INTERVENING',author={'id':'MEMBER_2'}))
        result = await self.adapter.send("group:850245632", text)
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.api.call.call_args.args[2]["message_reference"], {"message_id": "REFIDX_INPUT_1"})

    async def test_late_intervening_message_auto_quotes_only_first_bubble(self):
        await self.enqueue('first')
        path=self.base/'state/delivery_state.json'
        pending=json.loads(path.read_text())['pending']
        pending['segments']=['first','second']
        path.write_text(json.dumps({'pending':pending}))
        # Arrives after the model/outbox have finished, during the permission lookup.
        self.adapter.cfg['allow_proactive']=True
        async def response(method,url,*args):
            if method=='GET':
                self.store.remember('late','late-ref',self.now,group='GROUP_TEST',user_id='other')
                return {'allow_proactive_msg':True}
            return {'id':'sent'+str(pending['next_segment'])}
        self.api.call.side_effect=response
        self.assertEqual((await self.adapter.send('group:850245632','first'))['status'],'success')
        self.assertEqual(self.api.call.call_args.args[2]['message_reference'],{'message_id':'REFIDX_INPUT_1'})
        pending['next_segment']=1
        path.write_text(json.dumps({'pending':pending}))
        self.assertEqual((await self.adapter.send('group:850245632','second'))['status'],'success')
        self.assertNotIn('message_reference',self.api.call.call_args.args[2])

    async def test_cold_opener_never_quotes_historical_target(self):
        event=await self.enqueue()
        self.store.remember('later','r',self.now,group='GROUP_TEST',user_id='other')
        path=self.base/'state/delivery_state.json'
        pending=json.loads(path.read_text())['pending']
        pending.update(is_proactive=True,reply_target_id=str(event['message_id']))
        path.write_text(json.dumps({'pending':pending}))
        self.assertEqual((await self.adapter.send('group:850245632','answer'))['status'],'success')
        self.assertNotIn('message_reference',self.api.call.call_args.args[2])

    async def test_local_sticker_is_uploaded_then_sent_as_standalone_media(self):
        from PIL import Image
        image = self.base / 'stickers/hakimi_ok.png'
        Image.new('RGB', (2,2), 'white').save(image)
        text = '[CQ:image,file=' + image.as_uri() + ',sub_type=1]'
        await self.enqueue(text)
        self.api.upload_image.return_value = 'FILE_INFO'
        result = await self.adapter.send('group:850245632', text)
        self.assertEqual(result['status'], 'success')
        self.api.upload_image.assert_awaited_once_with('GROUP_TEST', image)
        body = self.api.call.call_args.args[2]
        self.assertEqual(body['msg_type'], 7)
        self.assertEqual(body['media'], {'file_info':'FILE_INFO'})
        self.assertNotIn('content', body)

    async def test_official_image_upload_uses_base64_without_auto_send(self):
        image = self.base / 'stickers/small.png'
        image.write_bytes(b'offline-upload-fixture')
        api = transport.OfficialAPI('190592927', 'not-used')
        with patch.object(api, 'call', new=AsyncMock(return_value={'file_info':'FILE_INFO'})) as call:
            self.assertEqual(await api.upload_image('GROUP_TEST', image), 'FILE_INFO')
        self.assertFalse(call.call_args.args[2]['srv_send_msg'])
        self.assertEqual(call.call_args.args[2]['file_data'], 'b2ZmbGluZS11cGxvYWQtZml4dHVyZQ==')

    async def test_token_expiry_string_and_single_refresh_with_no_message_retry(self):
        api = transport.OfficialAPI("190592927", "secret-not-logged", clock=lambda: self.now)
        with patch.object(api, "_request", return_value={"access_token": "token-not-logged", "expires_in": "7200"}) as call:
            await asyncio.gather(api.token(), api.token(), api.token())
            self.assertEqual(call.call_count, 1)
            self.now += 7190
            await api.token()
            self.assertEqual(call.call_count, 2)

    async def test_token_http_200_business_error_is_not_accepted(self):
        api = transport.OfficialAPI("190592927", "secret")
        class Response:
            status = 200
            def read(self, _): return b'{"code":100016,"message":"secret provider body"}'
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with patch.object(api.opener, "open", return_value=Response()):
            with self.assertRaises(transport.OfficialError) as exc:
                api._request("POST", "/app/getAppAccessToken", {})
        self.assertEqual(str(exc.exception), "API_100016")
        self.assertNotIn("secret", str(exc.exception))
        self.assertNotIn("secret", exc.exception.detail)
        for result, status, expected in [
            ({"code": "unsafe response", "msg": "secret"}, 200, "API_UNKNOWN"),
            ({"err_code": "40093003", "bizMessage": "secret"}, 400, "HTTP_40093003"),
            ({"err_code": "unsafe response", "message": "secret"}, 503, "HTTP_503"),
        ]:
            body = json.dumps(result).encode()
            if status == 200:
                with patch.object(Response, "read", return_value=body), patch.object(api.opener, "open", return_value=Response()):
                    with self.assertRaises(transport.OfficialError) as error:
                        api._request("POST", "/v2/groups/FIXTURE/messages")
            else:
                from urllib.error import HTTPError
                rejection = HTTPError("https://api.bot.qq.com", status, "secret", {}, io.BytesIO(body))
                with patch.object(api.opener, "open", side_effect=rejection):
                    with self.assertRaises(transport.OfficialError) as error:
                        api._request("POST", "/v2/groups/FIXTURE/messages")
            self.assertEqual(error.exception.code, expected)
            self.assertEqual(error.exception.ambiguous, status >= 500)
            self.assertEqual(error.exception.endpoint, "/v2/groups/<target>/messages")
            self.assertNotIn("secret", error.exception.detail)
            self.assertNotIn("unsafe response", error.exception.detail)

    async def test_gateway_identifies_ready_dispatch_and_duplicate_event_without_real_network(self):
        self.api.token.return_value = 'local-test-token'
        self.api.call.return_value = {'url': 'wss://api.bot.qq.com/websocket/'}
        sent, statuses = [], []
        frames = [
            {'op':10, 'd':{'heartbeat_interval':1000}},
            {'op':0, 's':1, 't':'READY', 'd':{'session_id':'local-session','user':{'id':'BOT_SELF'}}},
            {'op':0, 's':2, 't':'GROUP_AT_MESSAGE_CREATE', 'd':self.data()},
            {'op':0, 's':3, 't':'GROUP_AT_MESSAGE_CREATE', 'd':self.data()},
        ]
        class Socket:
            async def recv(self):
                if frames:
                    return json.dumps(frames.pop(0))
                await asyncio.sleep(30)
            async def send(self, value): sent.append(json.loads(value))
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        queue = asyncio.Queue()
        with patch.object(transport.websockets, 'connect', return_value=Socket()):
            task = asyncio.create_task(self.adapter.gateway(queue, lambda connected,error: statuses.append(connected)))
            event = await asyncio.wait_for(queue.get(), 2)
            await asyncio.sleep(.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(event['group_id'], 850245632)
        self.assertTrue(queue.empty())
        self.assertEqual(sent[0]['op'], 2)
        self.assertEqual(sent[0]['d']['intents'], 1 << 25)
        self.assertIn(True, statuses)

    async def test_gateway_missing_heartbeat_ack_disconnects_instead_of_hanging(self):
        self.api.token.return_value = 'local-test-token'
        self.api.call.return_value = {'url':'wss://api.bot.qq.com/websocket/'}
        self.api.clock = lambda: 1000
        self.api._expires = 10000
        sent, errors = [], []
        class Socket:
            hello = True
            async def recv(self):
                if self.hello:
                    self.hello = False
                    return json.dumps({'op':10,'d':{'heartbeat_interval':1000}})
                await asyncio.sleep(30)
            async def send(self, value): sent.append(json.loads(value))
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        with patch.object(transport.websockets, 'connect', return_value=Socket()):
            task = asyncio.create_task(self.adapter.gateway(asyncio.Queue(), lambda connected,error: errors.append(error)))
            deadline = time.monotonic()+3
            while 'HEARTBEAT_ACK_MISSING' not in errors and time.monotonic()<deadline:
                await asyncio.sleep(.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIn('HEARTBEAT_ACK_MISSING', errors)
        self.assertEqual([row['op'] for row in sent], [2,1])


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix="official_runtime_")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.runtime = fixture_root(self.base)

    def test_snapshot_reuses_rules_but_never_copies_other_groups_state_or_credentials(self):
        cfg = json.loads((self.runtime / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["bot_profile"]["active_groups"], ["850245632"])
        self.assertFalse(cfg["bot_profile"]["allow_private_chat"])
        self.assertEqual(cfg["user_bias"]["users"], {})
        self.assertEqual((self.runtime / "style_examples.jsonl").read_bytes(), (repo_file("style_examples.jsonl")).read_bytes())
        self.assertEqual((self.runtime / "style_guide.md").read_bytes(), (repo_file("style_guide.md")).read_bytes())
        self.assertFalse((self.runtime / "state/outbox.jsonl").exists())
        self.assertFalse((self.runtime / "state/session_memory.jsonl").exists())
        self.assertFalse((self.runtime / "state/official_appsecret.dpapi").exists())
        # Code is no longer copied into the runtime; the packaged module is the only copy.
        self.assertFalse((self.runtime / "reply_format.py").exists())
        self.assertIn("MAX_REPLY_SEGMENTS = 5", repo_file("reply_format.py").read_text(encoding="utf-8"))

    @unittest.skip('uses private chat fixtures that are not published')

    def test_normal_operation_removes_test_budget_without_resetting_bindings_or_memories(self):
        store = transport.RouteStore(self.runtime / 'state/official_routes.sqlite3')
        store.set('group_openid', 'BOUND_GROUP')
        control.migrate_memory(self.base)
        notes = (self.runtime / 'state/session_memory.jsonl').read_bytes()
        checkpoint = self.runtime / 'state/delivery_state.json'
        checkpoint.write_text('{"pending":{},"completed_ids":["old_receipt"]}')
        cfg = json.loads((self.base / 'official_test_config.json').read_text(encoding='utf-8'))
        cfg.update(operation_mode='normal', allow_proactive=True)
        (self.base / 'official_test_config.json').write_text(json.dumps(cfg), encoding='utf-8')
        control.prepare(self.base)
        actual = json.loads((self.runtime / 'config.json').read_text(encoding='utf-8'))
        original = json.loads((self.base / 'config.json').read_text(encoding='utf-8'))
        self.assertEqual(actual['official_operation_mode'], 'normal')
        self.assertEqual(actual['fallback'], original['fallback'])
        self.assertEqual(store.get('group_openid'), 'BOUND_GROUP')
        self.assertEqual((self.runtime / 'state/session_memory.jsonl').read_bytes(), notes)
        self.assertIn('old_receipt', checkpoint.read_text())

    def test_old_online_marker_is_reported_offline(self):
        marker = self.runtime / 'state/official_worker.json'
        marker.write_text(json.dumps({'at':1000, 'online':True, 'bound':True}))
        self.assertFalse(control.worker_health(self.base, now=1020)['online'])
        self.assertEqual(control.worker_health(self.base, now=1020)['health'], 'stale')
        self.assertTrue(control.worker_health(self.base, now=1003)['online'])

    def test_dsh_child_does_not_inherit_official_secret(self):
        from hakimi.models import fallback_runner
        import os
        with patch.dict(os.environ, {'HAKIMI_OFFICIAL_APPSECRET':'never-inherit-this'}):
            env = fallback_runner._child_environment(self.runtime, {'shim':Path('dsh.ps1')},
                {'api_key':'offline-provider-key', 'base_url':'https://api.deepseek.com'})
        self.assertNotIn('HAKIMI_OFFICIAL_APPSECRET', env)
        self.assertEqual(env['DSH_PERMISSION_MODE'], 'read-only')

    def test_rejected_app_change_does_not_replace_runtime_configuration(self):
        store = transport.RouteStore(self.runtime / 'state/official_routes.sqlite3')
        store.set('group_openid', 'BOUND_GROUP')
        before = (self.runtime / 'official_lane_config.json').read_bytes()
        cfg = json.loads((self.base / 'official_test_config.json').read_text(encoding='utf-8'))
        cfg['app_id'] = '1905925928'
        (self.base / 'official_test_config.json').write_text(json.dumps(cfg), encoding='utf-8')
        with self.assertRaises(transport.OfficialError):
            control.prepare(self.base)
        self.assertEqual((self.runtime / 'official_lane_config.json').read_bytes(), before)

    @unittest.skip('uses private chat fixtures that are not published')

    def test_bound_group_memory_mapping_is_scoped_idempotent_and_does_not_infer_people(self):
        with self.assertRaises(transport.OfficialError):
            control.migrate_memory(self.base)
        store = transport.RouteStore(self.runtime / "state/official_routes.sqlite3")
        store.set("group_openid", "TEST")
        self.assertEqual(control.migrate_memory(self.base), 3)
        self.assertEqual(control.migrate_memory(self.base), 0)
        notes = [json.loads(line) for line in (self.runtime / "state/session_memory.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({row["session_key"] for row in notes}, {"group:850245632"})
        self.assertEqual(store.get("member_mapping", {}), {})
        self.assertNotIn("808639025", json.dumps(notes))

    @unittest.skip('uses private chat fixtures that are not published')

    def test_new_group_requires_fresh_binding_and_imports_only_its_review(self):
        store = transport.RouteStore(self.runtime / 'state/official_routes.sqlite3')
        store.set('group_openid', 'OLD_GROUP')
        cfg = json.loads((self.base / 'official_test_config.json').read_text(encoding='utf-8'))
        cfg['test_group_number'] = '8355220284'
        (self.base / 'official_test_config.json').write_text(json.dumps(cfg), encoding='utf-8')
        with self.assertRaises(transport.OfficialError):
            control.prepare(self.base)
        archived = self.base / 'archived_old_runtime'
        self.runtime.rename(archived)
        control.prepare(self.base)
        fresh = transport.RouteStore(self.runtime / 'state/official_routes.sqlite3')
        self.assertIsNone(fresh.get('group_openid'))
        with self.assertRaises(transport.OfficialError):
            control.migrate_memory(self.base)
        fresh.set('group_openid', 'NEW_GROUP')
        self.assertEqual(control.migrate_memory(self.base), 2)
        self.assertEqual(control.migrate_memory(self.base), 0)
        rows = [json.loads(line) for line in (self.runtime / 'state/session_memory.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual({r['session_key'] for r in rows}, {'group:8355220284'})
        self.assertEqual(control.reviewed_memory(self.base).get('pending_members'), [])
        self.assertEqual(transport.RouteStore(archived / 'state/official_routes.sqlite3').get('group_openid'), 'OLD_GROUP')
        profiles = json.loads((self.runtime / 'group_profiles.json').read_text(encoding='utf-8'))
        self.assertEqual(profiles['groups']['8355220284']['profile'], 'lore_club')

    @unittest.skip('uses private chat fixtures that are not published')

    def test_explicit_member_mapping_requires_observed_identity_and_rejects_conflicts(self):
        store = transport.RouteStore(self.runtime / "state/official_routes.sqlite3")
        store.set("group_openid", "TEST")
        member = store.actor("MEMBER_TEST")
        control.map_member("808639025", member, self.base)
        notes = (self.runtime / 'state/session_memory.jsonl').read_text(encoding='utf-8')
        self.assertIn(member, notes)
        self.assertIn('颅骨超声', notes)
        control.prepare(self.base)
        restored = json.loads((self.runtime / 'config.json').read_text(encoding='utf-8'))
        self.assertIn(member, restored['user_bias']['users'])
        with self.assertRaises(transport.OfficialError):
            control.map_member("8314279225", member, self.base)
        with self.assertRaises(transport.OfficialError):
            control.map_member("8314279225", "123456789", self.base)

    @unittest.skip('uses private chat fixtures that are not published')

    def test_live_memory_updates_do_not_expand_review_and_tampered_snapshot_is_rejected(self):
        store = transport.RouteStore(self.runtime / 'state/official_routes.sqlite3')
        store.set('group_openid', 'TEST')
        with (self.base / 'memory.md').open('a', encoding='utf-8') as stream:
            stream.write('\nnew unreviewed memory must not be imported\n')
        self.assertEqual(control.migrate_memory(self.base), 3)
        self.assertNotIn('unreviewed', (self.runtime / 'state/session_memory.jsonl').read_text(encoding='utf-8'))
        snapshot = self.base / 'state/memory_migration_sources/group_850245632_20261023.md'
        snapshot.write_bytes(b'tampered source')
        control.migrate_memory(self.base)
        self.assertFalse(store.get('memory_mapping')['reviewed_snapshot_valid'])

    def test_waiter_returns_only_official_paths_and_keeps_production_context_untouched(self):
        from hakimi.bridge.turn_coordinator import TurnCoordinator
        sentinel = self.base / "state/active_context.json"
        sentinel.write_text('{"production":"unchanged"}')
        coordinator = TurnCoordinator(self.runtime)
        coordinator.publish({"session_key": "group:850245632", "recent_context_messages": []})
        marker = self.runtime / "state/official_worker.json"
        marker.write_text(json.dumps({"at": time.time(), "online": True, "bound": True}))
        prompt = official_waiter.try_official_claim(self.base)
        self.assertIsNotNone(prompt)
        self.assertIn(str(self.runtime / "qq_cli.py"), prompt)
        self.assertNotIn("python qq_cli.py", prompt)
        self.assertEqual(sentinel.read_text(), '{"production":"unchanged"}')
        self.assertTrue((self.runtime / "state/active_context.json").exists())
        self.assertIsNone(official_waiter.try_official_claim(self.base))

    def test_stale_or_unbound_official_worker_cannot_claim(self):
        from hakimi.bridge.turn_coordinator import TurnCoordinator
        coordinator = TurnCoordinator(self.runtime)
        coordinator.publish({"session_key": "group:850245632"})
        path = self.runtime / "state/official_worker.json"
        for data in ({"at": time.time()-20, "online": True, "bound": True},
                     {"at": time.time(), "online": True, "bound": False}):
            path.write_text(json.dumps(data))
            self.assertIsNone(official_waiter.try_official_claim(self.base))
        self.assertEqual(coordinator.snapshot()["status"], "queued")

    def test_real_reused_engine_primary_tools_and_fallback_with_fake_official_api(self):
        result = subprocess.run([sys.executable, str(ROOT / 'tests/official_lane_scenario.py'), str(self.runtime)],
                                cwd=self.runtime, capture_output=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace'))
        self.assertIn(b'OFFICIAL_OFFLINE_END_TO_END_OK', result.stdout)
        self.assertIn(b'OFFICIAL_MULTIGROUP_PRIMARY_AND_DSH_OK', result.stdout)

    def test_new_group_at_replies_even_when_group_info_is_forbidden(self):
        result = subprocess.run([sys.executable, str(ROOT/'tests/official_lane_scenario.py'),str(self.runtime),'--no-group-info'],
                                cwd=self.runtime,capture_output=True,timeout=25)
        self.assertEqual(result.returncode,0,result.stdout.decode('utf-8',errors='replace')+result.stderr.decode('utf-8',errors='replace'))
        self.assertIn(b'OFFICIAL_MULTIGROUP_PRIMARY_AND_DSH_OK',result.stdout)

    def test_cold_group_can_start_without_a_recent_passive_source_and_save_real_reply(self):
        result=subprocess.run([sys.executable,str(ROOT/'tests/official_lane_scenario.py'),str(self.runtime),'--cold-topic'],
                              cwd=self.runtime,capture_output=True,timeout=25)
        self.assertEqual(result.returncode,0,result.stdout.decode('utf-8',errors='replace')+result.stderr.decode('utf-8',errors='replace'))
        self.assertIn(b'OFFICIAL_COLD_TOPIC_NO_RECENT_SOURCE_OK',result.stdout)


if __name__ == "__main__":
    unittest.main()
