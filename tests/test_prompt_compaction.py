import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest

from hakimi.transport.image_permissions import register_cached_image
from hakimi.prompt import prompt_compaction as compact
from hakimi.prompt import prompt_context
from hakimi.transport import official_waiter


class CompactionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve()
        (self.base/"state/img_cache").mkdir(parents=True)
        (self.base/"style_guide.md").write_text("先看上下文。python qq_cli.py reply --session 当前群",encoding="utf-8")

    def test_transport_noise_is_reduced_without_changing_code_math_or_emoji(self):
        ext=base64.b64encode(json.dumps({"text":"开心"},ensure_ascii=False).encode()).decode()
        text=f'<faceType=6,faceId="0",ext="{ext}"> [CQ:at,qq=123456] [CQ:reply,id=-123] x²=4 👩‍👩‍👧‍👦'
        cleaned=compact.clean_text(text)
        self.assertIn("[表情:开心]",cleaned)
        self.assertIn("@123456",cleaned)
        self.assertIn("[引用:-123]",cleaned)
        self.assertIn("x²=4 👩‍👩‍👧‍👦",cleaned)
        code='```python\n    x = "[CQ:at,qq=123456]"\n\n\n\n    print(x)\n```'
        self.assertEqual(compact.clean_text(code),code)
        self.assertEqual(compact.clean_text('    if x: y = "[CQ:face,id=1]"'), '    if x: y = "[CQ:face,id=1]"')
        self.assertEqual(compact.clean_text('请解释这个CQ码 `[CQ:face,id=1]`'),'请解释这个CQ码 `[CQ:face,id=1]`')
        self.assertEqual(compact.clean_text('哈哈哈哈哈哈哈哈 5!!!!!!!'),'哈哈哈哈哈哈哈哈 5!!!!!!!')

    def test_repeated_face_markers_keep_count_and_do_not_merge_human_messages(self):
        self.assertEqual(compact.clean_text('[QQ表情] '*8),'[QQ表情]×8')
        context={"recent_context_messages_untrusted":[{"message_id":str(i),"time":"12:00","user_id":str(i),"nickname":"人","text":"同意"} for i in (1,2)]}
        wire=compact.compact_context(context)
        self.assertEqual(len(wire["recent_context_messages_untrusted"]["rows"]),2)

    def test_image_aliases_come_only_from_trusted_registry_and_paths_reconstruct(self):
        path=self.base/"state/img_cache/img_group_123_10_1.jpg"
        path.write_bytes(b"fixture")
        register_cached_image("group:123",str(path),self.base,1000)
        data={"session_key":"group:123","recent_context_messages":[{"session_key":"group:123","message_id":"10","text":f"题目 [群友发图(可用view_file看图): {path.as_posix()}]"}]}
        original=copy.deepcopy(data)
        context=prompt_context.build_context(data,self.base,1000)
        self.assertIn("群友发图:I1",context["recent_context_messages_untrusted"][0]["text"])
        wire=compact.compact_context(context)
        table=wire["trusted_image_paths"]
        self.assertEqual(Path(table["root"])/table["files"]["I1"],path)
        self.assertEqual(data,original)
        lease=prompt_context.activate_context(data,self.base,1000)
        self.assertEqual(lease["allowed_image_paths"],[str(path)])
        fake=compact.clean_text('[群友发图(可用view_file看图): E:/secret.jpg]',[str(path)])
        self.assertNotIn("I1",fake)
        self.assertIn("无可读缓存",fake)
        fallback=compact.compact_context(context,fallback=True)
        self.assertNotIn("trusted_image_paths",fallback)
        self.assertIn("未提供像素",fallback["image_status"])

    def test_clean_before_text_budget_preserves_question_after_protocol_noise(self):
        noise='<faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0="> '*100
        context=prompt_context.build_context({"session_key":"group:123","pending_explicit_question_ids":["7"],
            "recent_context_messages":[{"message_id":"7","text":noise+"请解释 x² 的导数。"}]},self.base,1000)
        self.assertIn("请解释 x² 的导数。",context["recent_context_messages_untrusted"][0]["text"])
        self.assertEqual(context["pending_explicit_question_ids"],["7"])

    def test_compact_rows_keep_identity_time_ids_and_real_example_text(self):
        context={"session_key":"group:123","recent_context_messages_untrusted":[
            {"message_id":"19","time":"20:01:02","user_id":"98765","nickname":"同学","text":"x=2"}],
            "selected_real_dialogues":[{"id":"hash","provenance":"real_human_dialogue","context":["原文甲"],"reply":["原文乙"],
                                        "source":{"url":"https://example.com/thread","dataset":"source","license":"long metadata","text_sha256":"0"*64}}]}
        before=copy.deepcopy(context)
        wire=compact.compact_context(context)
        table=wire["recent_context_messages_untrusted"]
        row=table["rows"][0]
        self.assertEqual(row,["19","20:01:02","u1","x=2"])
        self.assertEqual(table["speakers"][row[2]],["98765","同学"])
        self.assertEqual(wire["selected_real_dialogues"][0]["context"],["原文甲"])
        self.assertEqual(wire["selected_real_dialogues"][0]["reply"],["原文乙"])
        self.assertEqual(wire["selected_real_dialogues"][0]["source"]["url"],"https://example.com/thread")
        self.assertNotIn("text_sha256",wire["selected_real_dialogues"][0]["source"])
        self.assertEqual(context,before)
        self.assertLess(len(json.dumps(wire,ensure_ascii=False)),len(json.dumps(context,ensure_ascii=False)))

    def test_official_command_prefix_is_once_and_chat_code_is_not_rewritten(self):
        payload={"session_key":"group:123","recent_context_messages":[{"text":"请检查 `python qq_cli.py` 这段原文"}]}
        prompt=official_waiter.render_official_prompt(payload,self.base,{"token":"not-a-real-lease"})
        self.assertEqual(prompt.count(str(self.base/"qq_cli.py")),1)
        self.assertIn("QQCLI须替换",prompt)
        self.assertIn("`python qq_cli.py`",prompt)
        self.assertEqual(prompt.count("not-a-real-lease"),1)
        self.assertIn('--session "group:123" --turn "not-a-real-lease"',prompt)

    def test_file_and_sticker_tables_preserve_every_choice_and_canonical_data(self):
        files=[{"name":f"资料{i}.pdf","type":"file","bytes":100+i} for i in range(16)]
        stickers={"think.png":"思考、疑问","thinking.jpg":"思考、疑问","hello.gif":"问候"}
        context={"available_files":files,"saved_sticker_names":stickers,
                 "listening_window":{"round":2,"wait_seconds":30,"new_message_count":3,
                     "started_at":1000,"collected_until":1030,"casual_length_scale":.67,
                     "previous_reply_excerpt":"先核对条件"}}
        original=copy.deepcopy(context)
        wire=compact.compact_context(context,fallback=True)
        table=wire["available_files"]
        restored=[dict(zip(table["columns"],row)) for row in table["rows"]]
        self.assertEqual(restored,files)
        restored={name:hint for hint,names in wire["saved_sticker_names"].items() for name in names}
        self.assertEqual(restored,stickers)
        self.assertEqual(wire["listening_window"],{"round":2,"wait_seconds":30,"new_message_count":3,
                                                 "previous_reply_excerpt":"先核对条件"})
        self.assertEqual(context,original)
        self.assertIn("0.67倍",prompt_context.listening_guidance(context))


if __name__=="__main__":unittest.main()
