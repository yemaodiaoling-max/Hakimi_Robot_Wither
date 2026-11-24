"""Shared file tools and official uploads without contacting a real QQ group."""
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from hakimi.transport import file_delivery as files
from hakimi.models.fallback_runner import parse_response, FallbackError
from hakimi.models.fallback_supervisor import fallback_prompt
from hakimi.transport.official_files import hashes, upload, put_part
from hakimi.transport.official_transport import OfficialAPI, OfficialAdapter, OfficialError, RouteStore
from hakimi.prompt.prompt_context import build_context
from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.bridge.delivery_guard import DeliveryGuard
from tests.test_delivery_guard import OfflineEventLoop


class FileToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.session = "group:123"
        self.payload = {"session_key": self.session, "recent_context_messages": [
            {"session_key": self.session, "message_id": "11", "user_id": "22", "nickname": "甲",
             "time": "12:00:00", "text": "[合并转发记录]\n乙: 原来的第一句\n丙: 原来的第二句"},
            {"session_key": "group:456", "message_id": "99", "nickname": "别群", "text": "不要导出别群"}]}
        self.coordinator = TurnCoordinator(self.base)
        self.coordinator.publish(self.payload)
        self.claim = self.coordinator.claim_primary()

    def asset(self, name, body=b"asset"):
        root = files.directory(self.session, self.base)
        root.mkdir(parents=True, exist_ok=True)
        path = root/name
        path.write_bytes(body)
        return path

    def test_supported_types_and_exact_group_scope(self):
        for name, kind in (("片段.mp4", 2), ("语音.wav", 3), ("语音.mp3", 3), ("报告.pdf", 4),
                           ("资料.docx", 4), ("表格.xlsx", 4), ("资料.zip", 4), ("压缩包.7z", 4)):
            self.asset(name)
            self.assertEqual(files.resolve(name, self.session, self.base)[1], kind)
        self.assertEqual(len(files.menu(self.session, self.base)), 8)
        with self.assertRaises(ValueError):files.resolve("片段.mp4", "group:456", self.base)
        for name in ("../secret.pdf", "C:\\secret.pdf", "CON.txt", "LPT1.txt"):
            with self.assertRaises(ValueError):files.validate_specs([name])

    def test_new_text_document_and_attachment_commit_use_same_winner(self):
        result = self.coordinator.submit_reply(self.claim["token"], [], "gemini", files=[
            {"name": "学习笔记.md", "content": "# 条件\n先看定义，再看推导"}])
        self.assertTrue(result["accepted"])
        outbox = json.loads((self.base/"state/outbox.jsonl").read_text(encoding="utf-8"))
        path, kind = files.from_segment(outbox["segments"][0], self.session, self.base)
        self.assertEqual(kind, 4)
        self.assertIn("推导", path.read_text(encoding="utf-8"))
        rejected = self.coordinator.submit_reply(self.claim["token"], [], "gemini", files=[
            {"name": "过期.md", "content": "迟到结果不能写文件"}])
        self.assertFalse(rejected["accepted"])
        self.assertFalse((path.parent/"过期.md").exists())

    def test_primary_function_entry_creates_document_and_commits_text_plus_file(self):
        import contextlib
        import io
        from types import SimpleNamespace
        from hakimi.tools import qq_cli
        from hakimi.prompt.prompt_context import activate_context
        activate_context(self.payload, self.base, claim=self.claim)
        with patch.object(qq_cli, "BASE_DIR", self.base), contextlib.redirect_stdout(io.StringIO()):
            qq_cli.cmd_call(SimpleNamespace(session=self.session, turn=self.claim["token"],
                call_name="files.send", name="结果.csv", content="x,y\n1,2", text="结果在这里"))
        outbox = json.loads((self.base/"state/outbox.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(len(outbox["segments"]), 2)
        self.assertEqual((files.directory(self.session,self.base)/"结果.csv").read_text(encoding="utf-8"), "x,y\n1,2")

    def test_primary_chat_export_function_uses_real_visible_messages(self):
        import contextlib
        import io
        from types import SimpleNamespace
        from hakimi.tools import qq_cli
        from hakimi.prompt.prompt_context import activate_context
        activate_context(self.payload, self.base, claim=self.claim)
        with patch.object(qq_cli, "BASE_DIR", self.base), contextlib.redirect_stdout(io.StringIO()):
            qq_cli.cmd_call(SimpleNamespace(session=self.session, turn=self.claim["token"],
                call_name="chat.export", message_id="11"))
        self.assertIn("原来的第一句", (files.directory(self.session,self.base)/"聊天摘录.txt").read_text(encoding="utf-8"))

    def test_late_file_tool_signals_primary_return_without_creating_or_sending(self):
        from types import SimpleNamespace
        from hakimi.tools import qq_cli
        from hakimi.prompt.prompt_context import activate_context
        activate_context(self.payload, self.base, claim=self.claim)
        self.coordinator.report_failure(self.claim["token"], "quota")
        self.coordinator.claim_fallback()
        with patch.object(qq_cli, "BASE_DIR", self.base), patch("provider_health.ProviderHealth.late_reply") as late:
            with self.assertRaises(qq_cli.ToolError):
                qq_cli.cmd_call(SimpleNamespace(session=self.session, turn=self.claim["token"],
                    call_name="files.send", name="迟到.txt", content="迟到结果不发送"))
        late.assert_called_once_with(self.claim["token"], self.session)
        self.assertFalse((files.directory(self.session,self.base)/"迟到.txt").exists())

    def test_dsh_can_send_only_file_and_does_not_gain_filesystem_tools(self):
        self.asset("片段.mp4")
        self.coordinator.report_failure(self.claim["token"], "quota")
        claim = self.coordinator.claim_fallback()
        response = parse_response('{"action":"reply","text":"","files":["片段.mp4"]}')
        self.assertTrue(self.coordinator.submit_reply(claim["token"], [], "dsh", files=response["files"])["accepted"])
        (self.base/"style_guide.md").write_text("原来的人设", encoding="utf-8")
        prompt = fallback_prompt(self.payload, self.base)
        self.assertIn("available_files", prompt)
        self.assertIn("片段.mp4", prompt)
        self.assertIn("files表示现有文件名或{name,content}生成文档", prompt)
        for bad in ({"action":"silent","text":"","files":["片段.mp4"]},
                    {"action":"reply","text":"","files":["../secret.mp4"]}):
            with self.assertRaises(FallbackError):parse_response(json.dumps(bad))

    def test_chat_export_keeps_original_speakers_order_and_scope(self):
        result = self.coordinator.submit_reply(self.claim["token"], [], "gemini", files=[
            {"name":"聊天摘录.txt", "export":True, "message_id":"11"}])
        self.assertTrue(result["accepted"])
        content = (files.directory(self.session, self.base)/"聊天摘录.txt").read_text(encoding="utf-8")
        self.assertIn("甲", content)
        self.assertLess(content.index("乙:"), content.index("丙:"))
        self.assertNotIn("不要导出别群", content)
        self.assertIn("节选", content)

    def test_changed_queued_file_is_rejected_and_does_not_turn_into_a_new_file(self):
        path = self.asset("报告.pdf")
        segment = files.stage(["报告.pdf"], self.payload, self.base)[0]
        path.write_bytes(b"changed")
        with self.assertRaises(ValueError):files.from_segment(segment, self.session, self.base)

    def test_live_limits_update_without_reimport_and_overflow_does_not_write(self):
        self.asset("片段.mp4")
        config = self.base/"file_send_config.json"
        config.write_text('{"max_mb":{"video":0}}', encoding="utf-8")
        with self.assertRaises(ValueError):files.resolve("片段.mp4", self.session, self.base)
        config.write_text('{"max_mb":{"video":1}}', encoding="utf-8")
        self.assertEqual(files.resolve("片段.mp4", self.session, self.base)[1], 2)
        result = self.coordinator.submit_reply(self.claim["token"], ["正文"]*6, "gemini", files=[
            {"name":"没余量.txt", "content":"不能挤掉正文"}])
        self.assertFalse(result["accepted"])
        self.assertFalse((files.directory(self.session, self.base)/"没余量.txt").exists())


class UploadTests(unittest.IsolatedAsyncioTestCase):
    loop_factory = OfflineEventLoop

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()

    async def test_small_upload_types_cache_and_group_separation(self):
        path = self.base/"报告.pdf"
        path.write_bytes(b"%PDF-test")
        api = OfficialAPI("1", "fixture")
        api.call = AsyncMock(return_value={"file_info":"TOKEN", "ttl":600})
        self.assertEqual(await upload(api, "GROUP_A", path, 4), "TOKEN")
        body = api.call.call_args.args[2]
        self.assertEqual(base64.b64decode(body["file_data"]), path.read_bytes())
        self.assertFalse(body["srv_send_msg"])
        self.assertEqual(body["file_type"], 4)
        await upload(api, "GROUP_A", path, 4)
        self.assertEqual(api.call.await_count, 1)
        await upload(api, "GROUP_B", path, 4)
        self.assertEqual(api.call.await_count, 2)

    async def test_large_file_protocol_streams_parts_and_finishes_before_media_token(self):
        path = self.base/"大文件.txt"
        path.write_bytes(b"a"*(6*1024*1024))
        api = OfficialAPI("1", "fixture")
        api.call = AsyncMock(side_effect=[
            {"upload_id":"UPLOAD", "block_size":str(4*1024*1024), "parts":[
                {"index":1,"block_size":str(4*1024*1024),"presigned_url":"https://upload.example/1"},
                {"index":2,"block_size":str(2*1024*1024),"presigned_url":"https://upload.example/2"}]}, {}, {}, {"file_info":"TOKEN"}])
        with patch("official_files.put_part", return_value="MD5") as put:
            self.assertEqual(await upload(api, "GROUP", path, 4), "TOKEN")
        self.assertEqual([call.args[3] for call in put.call_args_list], [4*1024*1024, 2*1024*1024])
        requests = api.call.call_args_list
        self.assertTrue(requests[0].args[1].endswith("/upload_prepare"))
        self.assertNotIn("file_data", requests[0].args[2])
        self.assertEqual(requests[-1].args[2], {"upload_id":"UPLOAD","srv_send_msg":False})
        self.assertEqual(requests[0].args[2]["md5_10m"], hashlib.md5(path.read_bytes()).hexdigest())

    async def test_quota_rejection_does_not_fall_back_to_huge_base64(self):
        path = self.base/"大文件.zip"
        path.write_bytes(b"a"*(5*1024*1024))
        api = OfficialAPI("1", "fixture")
        api.call = AsyncMock(side_effect=OfficialError("API_40093002"))
        with self.assertRaises(OfficialError):await upload(api, "GROUP", path, 4)
        self.assertEqual(api.call.await_count, 1)

    async def test_official_queue_sends_media_for_both_providers_and_file_error_keeps_chat_available(self):
        (self.base/"state").mkdir()
        store = RouteStore(self.base/"state/routes.sqlite3")
        store.set("group_openid", "GROUP")
        api = AsyncMock()
        api.call.return_value = {"id":"ACK"}
        api.upload_file.return_value = "FILE_INFO"
        adapter = OfficialAdapter(self.base, {"test_group_number":"123","allow_proactive":False}, api, store)
        for index, provider in enumerate(("gemini", "dsh")):
            source, _ = store.remember(f"SOURCE_{index}", None, time.time(), group="GROUP")
            store.attach_turn(f"turn_{index}", source)
            root = files.directory("group:123", self.base)
            root.mkdir(parents=True, exist_ok=True)
            (root/"片段.mp4").write_bytes(b"video")
            segment = files.stage(["片段.mp4"], {"session_key":"group:123"}, self.base)[0]
            (self.base/"state/delivery_state.json").write_text(json.dumps({"pending":{
                "session_key":"group:123", "trigger_id":f"turn_{index}", "reply_id":f"reply_{index}",
                "segments":[segment], "next_segment":0, "provider":provider}}), encoding="utf-8")
            self.assertEqual((await adapter.send("group:123", segment))["status"], "success")
            self.assertEqual(api.call.call_args.args[2]["media"], {"file_info":"FILE_INFO"})
            self.assertNotIn("file_name", api.call.call_args.args[2])
        guard = DeliveryGuard(self.base/"state/guard.json", self.base/"state/outbox.jsonl")
        guard.state["pending"] = {"reply_id":"failed-file", "session_key":"group:123", "next_segment":0}
        guard.finish_attempt("file_error")
        self.assertEqual(guard.state["paused_until"], 0)
        self.assertEqual(guard.state["last_event"]["status"], "file_failed")


class MathToolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve()
        self.session='group:123'
        self.now=time.time()
        self.payload={'session_key':self.session,'created_at':self.now,'reply_target_message_id':'11',
                      'recent_context_messages':[{'session_key':self.session,'message_id':'11','user_id':'22','text':'数学问题'}]}
        self.coordinator=TurnCoordinator(self.base)
        self.coordinator.publish(self.payload)
        self.claim=self.coordinator.claim_primary()

    def test_actual_local_render_pagination_send_order_and_success_only_feedback(self):
        import contextlib,io,asyncio
        from types import SimpleNamespace
        from PIL import Image
        from hakimi.tools import qq_cli; from hakimi.tools import math_render
        from hakimi.prompt.prompt_context import activate_context
        from unittest.mock import AsyncMock
        guard=DeliveryGuard(self.base/'state/delivery_state.json',self.base/'state/outbox.jsonl',
                            {'global_min_interval_seconds':.001,'session_min_interval_seconds':.001,
                             'max_messages_per_minute':100,'max_segments_per_reply':5})
        store=RouteStore(self.base/'state/routes.sqlite3')
        store.set('group_openid','GROUP')
        source,_=store.remember('SOURCE','ref',self.now,group='GROUP',user_id='22')
        store.attach_turn(self.claim['trigger_id'],source)
        api=AsyncMock()
        posts=[]
        async def response(method,url,*args):
            if method=='GET':return {'allow_proactive_msg':True}
            posts.append(args[0])
            return {'id':'ACK'+str(len(posts))}
        api.call.side_effect=response
        api.upload_file.return_value='IMAGE_INFO'
        adapter=OfficialAdapter(self.base,{'test_group_number':'123','allow_proactive':True},api,store)
        actual_wait=math_render.wait_sent
        def deliver(base,reply_id,**_kwargs):
            async def drain():
                for _ in range(200):
                    message=guard.next_message()
                    if message:
                        guard.begin_attempt()
                        result=await adapter.send(*message)
                        guard.finish_attempt(result['status'])
                    if not guard.pending and guard.state.get('last_event',{}).get('reply_id')==reply_id:break
                    await asyncio.sleep(.002)
            with asyncio.Runner(loop_factory=OfflineEventLoop) as runner:runner.run(drain())
            actual_wait(base,reply_id,timeout=.1)
        formula='# 完整推导\n\n已知 $a\\ne 0$，可以配方：\n\n$$\\begin{aligned}x^2+\\frac ba x&=-\\frac ca\\\\x&=\\frac{-b\\pm\\sqrt{b^2-4ac}}{2a}\\end{aligned}$$\n\n'
        markdown=formula+'\n\n'.join(f'第{i}步：'+('这一步需要保留前面得到的条件，逐项代入并核对等号两边。'*6) for i in range(36))
        activate_context(self.claim['payload'],self.base,claim=self.claim)
        output=io.StringIO()
        with patch.object(qq_cli,'BASE_DIR',self.base),patch.object(math_render,'wait_sent',side_effect=deliver),contextlib.redirect_stdout(output):
            qq_cli.cmd_call(SimpleNamespace(call_name='math.send',session=self.session,turn=self.claim['token'],
                markdown=markdown,font_size=16,page_height=1000,before='我整理在图里喵',after='哪里卡住再问我'))
        self.assertEqual(output.getvalue(),'成功\n')
        self.assertGreater(api.upload_file.await_count,5)
        self.assertEqual(posts[0]['content'],'我整理在图里喵')
        self.assertEqual(posts[-1]['content'],'哪里卡住再问我')
        self.assertTrue(all('media' in row for row in posts[1:-1]))
        for call in api.upload_file.call_args_list:
            with Image.open(call.args[1]) as picture:
                self.assertEqual(picture.width,1100)
                self.assertLessEqual(picture.height,1000)
        self.assertEqual(files.menu(self.session,self.base),[])
        self.assertEqual(self.coordinator.snapshot()['outcome'],'replied')

    def test_dsh_uses_same_renderer_without_tools_or_followup_model_call(self):
        from hakimi.tools import math_render
        self.coordinator.report_failure(self.claim['token'],'quota')
        claim=self.coordinator.claim_fallback()
        result=parse_response(json.dumps({'action':'reply','text':'','math':{'markdown':'$$x^2=1$$','after':'这样就好懂啦'}}))
        with patch.object(math_render,'render',return_value=['prepared image','after']) as renderer:
            submitted=math_render.submit(self.coordinator,claim['token'],'dsh',result['math'],self.base)
        self.assertTrue(submitted['accepted'])
        renderer.assert_called_once()
        record=json.loads((self.base/'state/outbox.jsonl').read_text(encoding='utf-8'))
        self.assertTrue(record['rendered_math'])
        self.assertEqual(record['provider'],'dsh')
        with self.assertRaises(FallbackError):parse_response('{"action":"silent","text":"","math":{"markdown":"x"}}')

    def test_inline_equations_become_display_but_short_variables_stay_in_sentence(self):
        from hakimi.tools import math_render
        source=r'对于 $x$ 和 $g_{rr}$，有 $g_{rr}=\frac{1}{1-r_s/r}$，再计算 $\int_0^1 x^2\,dx$。'
        tex=math_render.markdown_tex(source)
        self.assertIn('$x$',tex)
        self.assertIn('$g_{rr}$',tex)
        self.assertIn(r'\[g_{rr}=\frac{1}{1-r_s/r}\text{，}\]',tex)
        self.assertIn(r'\[\int_0^1 x^2\,dx\text{。}\]',tex)
        self.assertFalse(math_render.prefer_display(r'r_s^2'))
        self.assertTrue(math_render.prefer_display(r'f(r)'))

    def test_display_promotion_keeps_markdown_lists_tables_headings_and_code_intact(self):
        from hakimi.tools import math_render
        source='# 条件 $a=b$\n\n- 第一步 $x^2=1$\n- 第二步 $x=1$\n\n| 条件 | 关系 |\n|---|---|\n| 已知 | $f=\\frac 12$ |\n\n`$x=2$`'
        tex=math_render.markdown_tex(source)
        self.assertEqual(tex.count(r'\item '),2)
        self.assertIn(r'\[x^2=1\]',tex)
        self.assertIn(r'\[x=1\]',tex)
        self.assertIn(r'$\displaystyle a=b$',tex)
        self.assertIn(r'$\displaystyle f=\frac 12$',tex)
        self.assertNotIn(r'\[f=',tex)
        self.assertIn(r'\$x=2\$',tex)

    def test_unmarked_unicode_equations_are_centered_without_rewriting_the_formula(self):
        from hakimi.tools import math_render
        tex=math_render.markdown_tex('恢复力 F = -(GMm / R³) · r，因此成立。\n\n`x=2`')
        from hakimi.tools.math_render import LITERAL_FONT
        self.assertIn(r'\begin{center}{\fontspec{'+LITERAL_FONT+r'}F = -(GMm / R³) · r，}\end{center}',tex)
        self.assertIn('因此成立。',tex)
        self.assertNotIn(r'\fontspec{'+LITERAL_FONT+r'}x=2',tex)
        code=math_render.markdown_tex('    x=2\n    $x=2$\n')
        self.assertNotIn('QZMATH',code)
        self.assertNotIn(r'\begin{center}',code)
        self.assertIn(r'\$x=2\$',code)
        link=math_render.markdown_tex('https://example.org/?x=2')
        self.assertNotIn(r'\begin{center}',link)
        self.assertIn('https://example.org/?x=2',link)

    def test_long_table_and_aligned_derivation_continue_onto_new_pages(self):
        from hakimi.tools import math_render
        table='| 步骤 | 条件 |\n|---|---|\n'+'\n'.join(f'| {i} | $x_{i}=1$ |' for i in range(32))
        aligned='$$\\begin{aligned}'+r'\\'.join(f'x_{i}&={i}+1' for i in range(32))+r'\end{aligned}$$'
        rows=math_render.render({'markdown':table+'\n\n'+aligned,'page_height':900,'font_size':16},self.session,self.base)
        self.assertGreater(len(rows),2)
        self.assertEqual(len(list(files.directory(self.session,self.base).glob('*.png'))),len(rows))

    def test_formula_channel_has_no_bubble_count_limit_but_normal_channel_keeps_five(self):
        pages=['formula page '+str(i) for i in range(50)]
        self.assertFalse(self.coordinator.submit_reply(self.claim['token'],pages[:6],'gemini')['accepted'])
        outbox=self.base/'state/outbox.jsonl'
        guard=DeliveryGuard(self.base/'state/delivery_state.json',outbox,{'max_segments_per_reply':5})
        self.assertTrue(self.coordinator.submit_reply(self.claim['token'],pages,'gemini',rendered_math=True)['accepted'])
        self.assertIsNotNone(guard.next_message())
        self.assertEqual(len(guard.pending['segments']),50)
        self.assertNotEqual(guard.state.get('last_event',{}).get('status'),'invalid_payload')

    def test_expired_owner_cannot_compile_or_send_and_errors_are_not_success(self):
        from hakimi.tools import math_render
        self.coordinator.complete(self.claim['token'])
        with patch.object(math_render,'render',side_effect=AssertionError('must not render')):
            self.assertFalse(math_render.submit(self.coordinator,self.claim['token'],'gemini',{'markdown':'$x$'},self.base)['accepted'])
        with self.assertRaises(ValueError):math_render.markdown_tex('$$\\input{secret}$$')
        with self.assertRaises(ValueError):math_render.validate({'markdown':'x','font_size':3})
        folder=self.base/'state/math_receipts'
        folder.mkdir()
        (folder/'failed.json').write_text('{"status":"unknown_after_restart"}',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'不要重复发送'):math_render.wait_sent(self.base,'failed',timeout=.1)


if __name__ == "__main__":
    unittest.main()
