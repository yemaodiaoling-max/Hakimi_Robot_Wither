"""Function entry and timed conversation behavior, with no external messages."""
import argparse
from collections import OrderedDict
import contextlib
import io
import json
import re
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hakimi.bridge import bridge_daemon as bridge
from hakimi.core.context_window import select_context
from hakimi.bridge.conversation_scheduler import ConversationScheduler
from hakimi.models.fallback_supervisor import fallback_prompt
from hakimi.prompt.prompt_context import activate_context, build_context, render_prompt
from hakimi.tools import qq_cli
from hakimi.prompt.tool_catalog import BY_NAME, register_cli
from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.tools import feeding
from test_delivery_guard import OfflineEventLoop

CFG = {"conversation_scheduler": {"enabled": True, "mention_buffer_seconds": 0}, "bot_profile": {}, "lurker_schedule": {}}
SESSION = "group:100"


class ListeningTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = ConversationScheduler()

    def reply(self, at=1000, mode="即时唤醒(被@/私聊)", trigger="first", session=SESSION):
        self.scheduler.offered_turn(session, at-10, trigger, wake_mode=mode,
            messages=[{"user_id": "22", "message_id": "question", "received_at": at-10,
                       "text": "请解释矩阵乘法这一步"}])
        self.scheduler.consume(session)
        self.scheduler.sent_reply(session, "我刚才解释了那一步", at, 100, trigger)

    def observe(self, at=1005, text="矩阵乘法这一步具体为什么这样？", mid="new"):
        self.scheduler.observe(SESSION, text, "22", mid, at)

    def test_clock_starts_after_bubble_batch_and_scores_related_window(self):
        self.reply()
        self.observe()
        self.assertFalse(self.scheduler.decide(SESSION, CFG, 1010).ready)
        self.scheduler.delivery_finished(SESSION, 1010)
        self.assertEqual(self.scheduler.group(SESSION).listen_due, 1055)
        self.assertFalse(self.scheduler.decide(SESSION, CFG, 1015.9).ready)
        decision = self.scheduler.decide(SESSION, CFG, 1016)
        self.assertTrue(decision.ready)
        self.assertEqual(decision.mode, "listening")
        self.assertEqual(decision.basis, 'ready_for_refinement')
        selected=[{'received_at':1005,'user_id':'22','message_id':'new',
                   'text':'矩阵乘法这一步具体为什么这样？'}]
        final=self.scheduler.score_window_messages(SESSION, CFG, 1016, selected)
        self.assertGreaterEqual(final.score, .45)
        self.assertGreater(final.relevance, .22)

    def test_fixed_window_and_four_turn_cooldown(self):
        for index in range(4):
            at = 1000+index*60
            self.reply(at, "即时唤醒(被@/私聊)" if index == 0 else "被@后监听窗口", f"turn{index}")
            self.scheduler.delivery_finished(SESSION, at+2)
            if index < 3:
                self.assertEqual(self.scheduler.group(SESSION).listen_due, at+47)
            else:
                state = self.scheduler.group(SESSION)
                self.assertEqual(state.listen_round, 0)
                self.assertEqual(state.cooldown_until, at+240)
                self.observe(at+3)
                self.assertEqual(self.scheduler.decide(SESSION, CFG, at+48).basis, "dialogue_cooldown")

    def test_generation_time_followup_is_preserved_after_context_cutoff(self):
        self.reply()
        self.observe(at=995, mid="during-generation")
        self.scheduler.delivery_finished(SESSION, 1002)
        self.assertEqual(self.scheduler.group(SESSION).listen_started, 990)
        self.assertIn("during-generation", self.scheduler.listening_context(SESSION, 1047)["new_message_ids"])
        self.assertTrue(self.scheduler.decide(SESSION, CFG, 1047).ready)

    def test_selected_ack_cannot_borrow_background_question_value(self):
        self.reply()
        self.observe()
        self.scheduler.delivery_finished(SESSION, 1002)
        ack = {"user_id": "33", "message_id": "ack", "received_at": 1006, "text": "哈哈"}
        self.assertFalse(self.scheduler.score_window_messages(SESSION, CFG, 1047, [ack]).ready)

    def test_new_mention_preempts_window_and_resets_after_its_reply(self):
        self.reply()
        self.scheduler.delivery_finished(SESSION, 1001)
        self.scheduler.observe(SESSION, "@哈基米 新问题", "33", "at", 1002, mentioned=True)
        self.assertEqual(self.scheduler.decide(SESSION, CFG, 1002).mode, "mention")
        self.reply(1010, trigger="new-at")
        self.scheduler.delivery_finished(SESSION, 1011)
        self.assertEqual(self.scheduler.group(SESSION).listen_due, 1056)

    def test_empty_window_and_explicit_skip_end_listening(self):
        self.reply()
        self.scheduler.delivery_finished(SESSION, 1001)
        self.assertFalse(self.scheduler.decide(SESSION, CFG, 1046).ready)
        self.assertEqual(self.scheduler.group(SESSION).listen_round, 0)
        self.reply(1100)
        self.scheduler.offered_turn(SESSION, 1146, "followup", wake_mode="被@后监听窗口")
        self.scheduler.outcome({"status": "completed", "trigger_id": "followup", "outcome": "no_reply",
                                "payload": {"session_key": SESSION}}, 1147)
        self.assertEqual(self.scheduler.group(SESSION).listen_round, 0)

    def test_delivery_failure_without_a_new_sent_reply_does_not_rearm(self):
        self.reply()
        self.scheduler.offered_turn(SESSION, 1046, "followup", wake_mode="被@后监听窗口")
        self.scheduler.delivery_finished(SESSION, 1050)
        self.assertEqual(self.scheduler.group(SESSION).listen_round, 0)
        self.reply(session="private:22")
        self.assertEqual(self.scheduler.group("private:22").listen_round, 0)

    def test_restart_preserves_deadline_and_recovers_only_unprocessed_same_group_messages(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            base = Path(folder)
            self.scheduler.restore(base, 980)
            self.reply()
            self.scheduler.delivery_finished(SESSION, 1001)
            restored = ConversationScheduler()
            restored.restore(base, 1008)
            self.assertTrue(restored.healthy)
            self.assertEqual(restored.group(SESSION).listen_due, 1046)
            rows = [{"session_key": group, "message_id": str(index), "received_at": at,
                     "user_id": "22", "text": "矩阵乘法这一步具体怎么解释？"} for index, (group, at) in enumerate(
                         ((SESSION, 985), (SESSION, 1005), ("group:200", 1006)))]
            inbox = base/"state/inbox.jsonl"
            inbox.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            engine = bridge.DynamicHakimiEngine()
            engine.conversation_scheduler = restored
            with patch.object(bridge, "INBOX_LOG", inbox), patch.object(bridge, "load_config", return_value=CFG):
                engine.restore_listening_unread(1008)
                engine.restore_listening_unread(1009)
            self.assertEqual([row[2] for row in restored.group(SESSION).pending], ["1"])
            self.assertEqual(engine.unread_counts[SESSION], 1)
            self.assertTrue(restored.decide(SESSION, CFG, 1046).ready)

    def test_window_prefers_new_chat_and_two_own_bubbles_in_existing_twelve_row_budget(self):
        rows = [{"session_key": SESSION, "message_id": str(i), "user_id": "SELF" if i in (30,31) else "22",
                 "text": str(i)} for i in range(50)]
        rows.append({"session_key": "group:200", "message_id": "other", "text": "别群"})
        window = {"new_message_ids": [str(i) for i in range(32,50)], "round": 1, "casual_length_scale": .8}
        data = {"session_key": SESSION, "recent_context_messages": rows, "listening_window": window}
        selected, target = select_context(data)
        self.assertEqual([r["message_id"] for r in selected], ["30", "31"]+[str(i) for i in range(40,50)])
        self.assertEqual(target["message_id"], "49")
        context = build_context(data)
        self.assertEqual(len(context["recent_context_messages_untrusted"]), 12)


class CallableToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        now = time.time()
        self.payload = {"session_key": SESSION, "created_at": now, "wake_mode": "被@后监听窗口",
                        "recent_context_messages": [{"session_key": SESSION, "message_id": "101",
                            "user_id": "22", "text": "讨论微积分", "nickname": "测试成员"}],
                        "listening_window": {"round": 2, "wait_seconds": 30, "casual_length_scale": .67,
                                             "new_message_ids": ["101"]}}
        self.coordinator = TurnCoordinator(self.base)
        self.coordinator.publish(self.payload)
        self.claim = self.coordinator.claim_primary()
        activate_context(self.claim["payload"], self.base, claim=self.claim)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, value in {"BASE_DIR": self.base, "STATE_DIR": self.base/"state",
                            "SESSION_MEMORY_FILE": self.base/"state/session_memory.jsonl",
                            "OUTBOX_FILE": self.base/"state/outbox.jsonl"}.items():
            stack.enter_context(patch.object(qq_cli, name, value))

    def call(self, name, **data):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            qq_cli.cmd_call(SimpleNamespace(call_name=name, session=SESSION, turn=self.claim["token"], **data))
        return json.loads(output.getvalue())["result"]

    def test_function_schemas_parse_values_without_json_escaping(self):
        parser = argparse.ArgumentParser()
        register_cli(parser.add_subparsers(dest="cmd"), qq_cli.cmd_call)
        args = parser.parse_args(["call", "calculator.evaluate", "--session", SESSION, "--turn", "token",
                                  "--expression", "(2+3)*4"])
        self.assertEqual(args.expression, "(2+3)*4")
        args = parser.parse_args(["call", "context.read", "--session", SESSION, "--turn", "token", "--limit", "12"])
        self.assertEqual(args.limit, 12)
        for item in BY_NAME.values():
            self.assertIn("inputSchema", item)
        self.assertNotIn("shell.execute", BY_NAME)

    def test_actual_calculation_and_lazy_schema_lookup(self):
        self.assertEqual(self.call("calculator.evaluate", expression="(2+3)*4")["data"]["value"], 20)
        schema = self.call("tools.list", tool_name="video.watch")
        self.assertEqual(schema["inputSchema"]["required"], ["url"])
        self.assertEqual(self.call("images.list"), {"images": []})

    def test_video_uses_existing_processor_and_exact_url(self):
        with patch.object(qq_cli, "run_tool", return_value={"conclusion": "视频观察"}) as run:
            result = self.call("video.watch", url="https://b23.tv/example")
        self.assertEqual(result["conclusion"], "视频观察")
        self.assertEqual(run.call_args.args[:3], ("bilibili_video", SESSION, {"url": "https://b23.tv/example"}))

    def test_person_memory_write_then_query_and_no_write_during_query(self):
        self.call("memory.remember", kind="person", user_id="22", key="称呼", note="希望称呼小树")
        path = self.base/"state/session_memory.jsonl"
        before = path.read_bytes()
        result = self.call("memory.search", query="怎么称呼")
        self.assertIn("小树", json.dumps(result, ensure_ascii=False))
        self.assertEqual(before, path.read_bytes())

    def test_memory_read_returns_original_and_does_not_cross_scope(self):
        from hakimi.memory.memory_store import store
        index=store(str(self.base))
        now=time.time()
        index.observe({'session_key':SESSION,'user_id':'22','message_id':'old',
                       'received_at':now-60,'text':'微积分复习要先弄清导数的定义'})
        index.observe({'session_key':'private:33','user_id':'33','message_id':'secret',
                       'received_at':now-60,'text':'微积分私人内容'})
        result=self.call('memory.search',query='微积分导数')
        mid=re.search(r'\[M([0-9]+)\]',json.dumps(result)).group(1)
        original=self.call('memory.read',memory_id='M'+mid)
        self.assertEqual(original['text'],'微积分复习要先弄清导数的定义')
        with index.db() as db:
            secret=db.execute("SELECT id FROM records WHERE origin='secret'").fetchone()[0]
        with self.assertRaises(qq_cli.ToolError):
            self.call('memory.read',memory_id='M'+str(secret))

    def test_reply_alias_uses_same_coordinator_and_rejects_completed_or_foreign_turn(self):
        with self.assertRaises(qq_cli.ToolError):
            qq_cli.cmd_call(SimpleNamespace(call_name="images.list", session=SESSION, turn="foreign"))
        self.call("reply.send", text="第一条|第二条")
        state = self.coordinator.snapshot()
        self.assertEqual((state["status"], state["outcome"]), ("completed", "replied"))
        with self.assertRaises(qq_cli.ToolError):
            self.call("memory.remember", note="迟到结果")

    def test_skip_uses_existing_lifecycle(self):
        self.call("reply.skip")
        self.assertEqual(self.coordinator.snapshot()["outcome"], "no_reply")

    def test_actual_host_failure_goes_to_existing_fallback_lifecycle(self):
        self.call("turn.failure", reason="quota")
        self.assertEqual(self.coordinator.snapshot()["failure"]["reason"], "quota")

    def test_new_tool_manual_preserves_persona_verbatim_and_shared_listening_hint(self):
        persona = "# 原人设\n熟悉的口吻、既有昵称和分条方式完全保留\n\n"
        (self.base/"style_guide.md").write_text(persona+"## 工具与数据边界\n- 回复：旧命令详细说明\n", encoding="utf-8")
        primary = render_prompt(self.payload, self.base, self.claim)
        self.assertIn(persona, primary)
        self.assertNotIn("旧命令详细说明", primary)
        self.assertIn("call 工具名", primary)
        self.assertIn("video.watch", primary)
        for prompt in (primary, fallback_prompt(self.payload, self.base)):
            self.assertIn("0.67倍", prompt)
            self.assertIn("专业讨论与学习答疑仍完整展开", prompt)
        ordinary = {key: value for key,value in self.payload.items() if key != "listening_window"}
        self.assertNotIn("本轮日常长度偏好", render_prompt(ordinary, self.base))


class FeedingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.code = feeding.arm_binding(self.base)

    def record(self, text, mid="1", user="22", session=SESSION):
        return feeding.record_interaction(text, session, user, mid, self.base, self_id="99", now=1000)

    def bind(self):
        self.assertTrue(self.record("[CQ:at,qq=99] /绑定投喂 " + self.code, "bind"))

    def payload(self, text="你好", user="22", session=SESSION, mid="1"):
        return {"session_key": session, "recent_context_messages": [
            {"session_key": session, "message_id": mid, "user_id": user, "nickname": "同名成员", "text": text}]}

    def test_explicit_commands_and_short_natural_gifts_only(self):
        for text, result in (("/投喂罐头", ("罐头",1)), ("投喂罐头", ("罐头",1)),
                             ("/投喂 金枪鱼罐头 2", ("金枪鱼罐头",2)), ("给你一个罐头", ("罐头",1)),
                             ("投喂两罐金枪鱼罐头！", ("金枪鱼罐头",2))):
            self.assertEqual(feeding.parse_feed(text), result)
        for text in ('他说“给你一个罐头”', '不要投喂罐头', '罐头好吃吗', '/投喂罐头 -1',
                     '/投喂罐头 0', '/投喂罐头 10000', '投喂一百个罐头', '给你一个罐头\n别的话',
                     '[合并转发记录] 给你一个罐头', '[引用: /投喂罐头]'):
            self.assertIsNone(feeding.parse_feed(text), text)

    def test_one_time_binding_required_and_cannot_be_taken_over(self):
        self.assertFalse(self.record('/投喂罐头'))
        self.assertFalse(self.record('/绑定投喂 deadbeef'))
        self.bind()
        self.assertFalse(self.record('/绑定投喂 '+self.code, 'steal', user='33'))
        with self.assertRaises(ValueError):
            feeding.arm_binding(self.base)
        state = json.loads((self.base/'state/feeding.json').read_text(encoding='utf-8'))
        self.assertEqual(state['owner_user_id'], '22')
        self.assertNotIn('binding_code', state)
        self.assertEqual(state['total'], 0)

    def test_owner_only_durable_dedup_across_groups_and_same_nickname(self):
        self.bind()
        self.assertFalse(self.record('/投喂罐头', user='33'))
        self.assertTrue(self.record('[CQ:at,qq=99] /投喂 金枪鱼罐头 2'))
        before = (self.base/'state/feeding.json').read_bytes()
        self.assertFalse(self.record('/投喂 金枪鱼罐头 2'))
        self.assertEqual(before, (self.base/'state/feeding.json').read_bytes())
        self.assertTrue(self.record('给你一个罐头', mid='2', session='group:200'))
        context = build_context(self.payload(session='group:300'), self.base)['feeding']
        self.assertEqual((context['user_id'],context['total'],context['last']['item']), ('22',3,'罐头'))
        self.assertNotIn('visible_receipts', context)
        other = build_context(self.payload('给你一个罐头',user='33'),self.base)['feeding']
        self.assertEqual(other, {'status':'other_account'})

    def test_reply_own_body_counts_but_reference_body_does_not(self):
        self.bind()
        self.assertTrue(self.record('[CQ:reply,id=100] [CQ:at,qq=99] 给你一个罐头'))
        self.assertFalse(self.record('[CQ:reply,id=100]', mid='2'))
        self.assertFalse(self.record('&#91;CQ:at,qq=99&#93; /投喂罐头', mid='3'))
        self.assertFalse(self.record('[CQ:json,data={"text":"/投喂罐头"}]', mid='4'))

    def test_both_models_receive_same_receipt_and_unchanged_persona(self):
        self.bind()
        self.record('/投喂 金枪鱼罐头 2')
        persona = '# 原人设\n原有语言风格和分群语气不变\n'
        (self.base/'style_guide.md').write_text(persona,encoding='utf-8')
        data = self.payload('@哈基米 /投喂 金枪鱼罐头 2')
        context = build_context(data,self.base)['feeding']
        self.assertEqual(context['visible_receipts'], [
            {'message_id':'1','user_id':'22','item':'金枪鱼罐头','count':2}])
        for prompt in (render_prompt(data,self.base),fallback_prompt(data,self.base)):
            self.assertIn(persona,prompt)
            self.assertIn('不能把已收到的罐头说成空气或否认赠送',prompt)
            self.assertIn('"total":2',prompt)
            self.assertIn('金枪鱼罐头',prompt)
        self.assertEqual((self.base/'style_guide.md').read_text(encoding='utf-8'),persona)

    def test_ordinary_users_have_no_extra_prompt_and_old_or_broken_state_is_not_denial(self):
        self.assertNotIn('feeding',build_context(self.payload(),self.base))
        context=build_context(self.payload('罐头记录'),self.base)
        self.assertEqual(context['feeding']['status'],'not_bound')
        self.assertIn('无记录不代表过去从没被喂过',feeding.guidance(context))
        (self.base/'state/feeding.json').write_text('{',encoding='utf-8')
        self.assertEqual(build_context(self.payload('罐头记录'),self.base)['feeding']['status'],'unavailable')


class HybridMemoryTests(unittest.TestCase):
    def setUp(self):
        from hakimi.memory.memory_store import MemoryStore
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve()
        self.index=MemoryStore(self.base)
        self.now=time.time()

    def chat(self, text, mid='old', user='22', session='group:200', age=60):
        row={'session_key':session,'user_id':user,'message_id':mid,'received_at':self.now-age,'text':text}
        self.index.observe(row)
        return row

    def query(self, text='怎么称呼我', user='22'):
        target={'session_key':SESSION,'user_id':user,'message_id':'new','text':text}
        return self.index.retrieve(SESSION,[target],target,now=self.now)

    def test_explicit_fact_survives_raw_pruning_with_attributed_evidence(self):
        self.chat('记住，我喜欢苹果','ancient',age=10*86400)
        self.assertEqual(self.index.prune(self.now),1)
        with self.index.db() as db:
            raw=db.execute("SELECT * FROM records WHERE kind='chat' AND origin='ancient'").fetchone()
            card=db.execute("SELECT * FROM records WHERE kind='person' AND origin='ancient'").fetchone()
            self.assertEqual(raw['text'],'')
            self.assertEqual(raw['vector'],b'')
            self.assertEqual(card['verification'],'self_report')
            self.assertEqual(card['evidence'],'记住，我喜欢苹果')
            rid=card['id']
        self.assertIn('苹果',' '.join(self.query('我喜欢什么')))
        read=self.index.read(rid,SESSION,{'22'})
        self.assertFalse(read['raw_available'])
        self.assertIsNone(read['original_message'])
        self.assertEqual(read['evidence'],'记住，我喜欢苹果')

    def test_recent_original_count_cap_and_hot_cache_age(self):
        (self.base/'config.json').write_text(json.dumps({'memory_policy':{'recent_original_limit':100}}),encoding='utf-8')
        rows=[{'session_key':SESSION,'user_id':'22','message_id':str(i),'received_at':self.now-200+i,'text':'普通聊天'+str(i)} for i in range(105)]
        path=self.base/'state/chat_history.jsonl'
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
        self.index.sync()
        with self.index.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE text!=''").fetchone()[0],100)
        rows.append({**rows[-1],'message_id':'too-old','received_at':self.now-10*86400})
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
        self.assertEqual(bridge._trim_jsonl_file(path,600,650,max_age_sec=7*86400),1)
        self.assertEqual(len(path.read_text().splitlines()),105)

    def test_same_person_private_history_is_shared_and_other_accounts_stay_separate(self):
        self.chat('拓扑课程公开笔记','public')
        self.chat('拓扑课程另一个人的记录','other',user='33')
        self.chat('拓扑课程私人完整原文','private',session='private:22')
        note={'kind':'person','session_key':'private:22','person_id':'22','key':'称呼',
              'note':'希望称呼小林','created_at':self.now-30,'origin_message_id':'private'}
        self.index.note(note)
        result=' '.join(self.query('拓扑课程'))
        self.assertIn('公开笔记',result)
        self.assertIn('私人完整原文',result)
        self.assertNotIn('另一个人的记录',result)
        with self.index.db() as db:
            ids={row['origin']:row['id'] for row in db.execute("SELECT * FROM records WHERE kind='chat'")}
            nid=db.execute("SELECT id FROM records WHERE kind='person'").fetchone()[0]
        self.assertEqual(self.index.read(ids['private'],SESSION,{'22'})['text'], '拓扑课程私人完整原文')
        self.assertIsNone(self.index.read(ids['other'],SESSION,{'22'}))
        self.assertEqual(self.index.read(nid,SESSION,{'22'})['text'], '希望称呼小林')
        self.assertIsNotNone(self.index.read(nid,SESSION,{'22'},cross_groups=True))

    def test_person_corrections_shadow_older_names_and_expired_cards(self):
        base={'kind':'person','session_key':SESSION,'person_id':'22','key':'昵称','note':'旧名小树','created_at':self.now-100}
        self.index.note(base)
        self.chat('以后叫我小林','change',age=50)
        result=' '.join(self.query())
        self.assertIn('小林',result)
        self.assertNotIn('旧名小树',result)
        self.index.note({**base,'key':'称呼','note':'最新叫我阿林','created_at':self.now-10})
        result=' '.join(self.query())
        self.assertIn('阿林',result)
        self.assertNotIn('以后叫我小林',result)
        self.index.note({**base,'note':'已过期更正','created_at':self.now-5,'expires_at':self.now-1})
        result=' '.join(self.query())
        self.assertNotIn('阿林',result)
        self.assertNotIn('小林',result)

    def test_jsonl_partial_append_replacement_and_duplicate_import(self):
        path=self.base/'state/chat_history.jsonl'
        row={'session_key':SESSION,'user_id':'22','message_id':'old','received_at':self.now-60,'text':'原始记录'}
        line=json.dumps(row,ensure_ascii=False)
        path.write_text(line,encoding='utf-8')
        self.index.sync()
        with self.index.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM records').fetchone()[0],0)
        with path.open('a',encoding='utf-8') as stream:stream.write('\n')
        self.index.sync()
        temp=path.with_suffix('.tmp')
        temp.write_text(line+'\n'+json.dumps({**row,'message_id':'next','text':'第二条'})+'\n',encoding='utf-8')
        temp.replace(path)
        self.index.sync(); self.index.sync()
        with self.index.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM records').fetchone()[0],2)

    def test_graph_expands_named_topic_without_replaying_irrelevant_raw_history(self):
        self.chat('《群论》里讲到正规子群与商群','one')
        self.chat('《群论》的另一节讨论同态基本定理','two',age=40)
        self.chat('今天吃苹果','unrelated')
        result=self.query('解释正规子群')
        self.assertIn('同态基本定理',' '.join(result))
        self.assertNotIn('今天吃苹果',' '.join(result))
        self.assertLessEqual(sum(map(len,result)),600)

    def test_source_url_and_recent_provenance_stay_exact(self):
        self.index.note({'session_key':'group:200','kind':'lore','created_at':self.now-1,
            'key':'原神任务','note':'任务文本里角色的判断仍待核实','source':'https://example.org/wiki/quest'})
        result=' '.join(self.query('原神任务文本'))
        self.assertIn('https://example.org/wiki/quest',result)
        self.assertIn('待复核',result)
        self.index.note({'session_key':SESSION,'kind':'lore','created_at':self.now,'note':'不可引用资料','source':'http://example.org'})
        self.assertNotIn('不可引用资料',' '.join(self.query('不可引用资料')))

    def test_dsh_sourced_knowledge_uses_same_program_writer_and_allows_new_corrections(self):
        from hakimi.models.fallback_runner import parse_response
        from hakimi.memory.smart_memory import save_memory
        proposal={'kind':'lore','key':'线性代数','note':'同态定理笔记，结论待复核','source':'https://example.org/algebra'}
        parsed=parse_response(json.dumps({'action':'reply','text':'记下啦','memory':proposal}))
        self.assertEqual(parsed['memory'],proposal)
        self.assertTrue(save_memory(self.base,SESSION,parsed['memory'],{},provider='dsh',now=self.now-30))
        path=self.base/'state/session_memory.jsonl'
        self.assertFalse(save_memory(self.base,SESSION,parsed['memory'],{},provider='gemini',now=self.now-29))
        correction={**proposal,'note':'同态定理更正笔记，另一版本待复核'}
        self.assertTrue(save_memory(self.base,SESSION,correction,{},provider='gemini',now=self.now-20))
        self.assertTrue(save_memory(self.base,SESSION,proposal,{},provider='dsh',now=self.now-10))
        result=' '.join(self.query('同态定理'))
        self.assertIn(proposal['note'],result)
        self.assertNotIn(correction['note'],result)
        self.assertEqual(len(path.read_text(encoding='utf-8').splitlines()),3)
        invalid=parse_response(json.dumps({'action':'reply','text':'普通回复','memory':{**proposal,'source':'file:///secret'}}))
        self.assertNotIn('memory',invalid)

    def test_study_and_lore_smalltalk_share_same_short_rp_rule_for_both_models(self):
        (self.base/'style_guide.md').write_text('# 原来的人设与口吻\n日常15–50字，按原风格分条',encoding='utf-8')
        for profile in ('study_calm','lore_club'):
            (self.base/'group_profiles.json').write_text(json.dumps({'default_profile':profile,'profiles':{profile:{}}}),encoding='utf-8')
            data={'session_key':SESSION,'recent_context_messages':[{'session_key':SESSION,'message_id':'1','user_id':'22','text':'晚上吃啥'}]}
            for prompt in (render_prompt(data,self.base),fallback_prompt(data,self.base)):
                self.assertIn('学习群、考据群的闲聊也保持哈基米的RP和日常短句',prompt)
                self.assertIn('原来的人设与口吻',prompt)
                self.assertIn('日常15–50字',prompt)
                self.assertNotIn('20–150字',prompt)

    def test_different_wording_can_follow_shared_knowledge_without_direct_word_overlap(self):
        from hakimi.bridge.conversation_scheduler import features
        a='矩阵特征向量'
        b='作用以后方向不改变，只发生缩放'
        self.assertFalse(features(a)&features(b))
        self.index.note({'session_key':SESSION,'kind':'lore','created_at':self.now-30,
            'note':'《线性代数》矩阵作用于特征向量后，方向不改变，只发生缩放；比例称为特征值。',
            'source':'https://example.org/linear-algebra'})
        anchor=self.index.topic_signature(SESSION,a)
        incoming=self.index.topic_signature(SESSION,b)
        self.assertTrue(set(anchor)&set(incoming))
        scheduler=ConversationScheduler()
        scheduler.observe(SESSION,a,'22','1',self.now-20)
        scheduler.offered_turn(SESSION,self.now-15,'knowledge',wake_mode='上下文积累判断',memory_nodes=anchor)
        scheduler.consume(SESSION)
        scheduler.sent_reply(SESSION,a,self.now-10,777,'knowledge')
        scheduler.observe(SESSION,b,'33','2',self.now,memory_nodes=incoming)
        decision=scheduler.decide(SESSION,CFG,self.now)
        self.assertEqual(decision.basis,'memory_bridge')
        self.assertEqual(decision.mode,'continuation')
        self.assertGreater(decision.relevance,.22)
        self.assertEqual(self.index.topic_signature(SESSION,'晚上想吃烧烤还是火锅'),[])

    def test_transient_dialogue_bridge_is_hidden_from_memory_prompt_and_expires(self):
        nodes=self.index.dialogue_signature(SESSION,'turn','特征向量是什么',
            '矩阵作用以后方向不改变，只发生缩放，这种非零向量称为特征向量',self.now)
        self.assertTrue(nodes)
        self.assertEqual(self.query('特征向量'),[])
        self.index.prune(self.now+1801)
        with self.index.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE kind='dialogue'").fetchone()[0],0)

    def test_memory_topic_nodes_survive_scheduler_restart_and_do_not_override_mentions_or_listening(self):
        nodes=['d'+'1'*15]
        s=ConversationScheduler();s.restore(self.base,self.now-60)
        s.observe(SESSION,'初始问题','22','1',self.now-30)
        s.offered_turn(SESSION,self.now-20,'t',wake_mode='上下文积累判断',memory_nodes=nodes)
        s.consume(SESSION);s.sent_reply(SESSION,'初始回答',self.now-10,777,'t')
        restored=ConversationScheduler();restored.restore(self.base,self.now)
        self.assertTrue(restored.healthy)
        self.assertEqual(restored.group(SESSION).memory_source,set(nodes))
        restored.observe(SESSION,'换个话题，今天吃啥','33','2',self.now,memory_nodes=nodes)
        self.assertNotEqual(restored.decide(SESSION,CFG,self.now).basis,'memory_bridge')
        restored.observe(SESSION,'明确@','33','3',self.now,mentioned=True,memory_nodes=nodes)
        self.assertEqual(restored.decide(SESSION,CFG,self.now).mode,'mention')
        restored.consume(SESSION)
        state=restored.group(SESSION);state.listen_round=1;state.listen_started=self.now;state.listen_due=self.now+15
        restored.observe(SESSION,'另一个话题','44','4',self.now+2,memory_nodes=nodes)
        self.assertEqual(restored.decide(SESSION,CFG,self.now+14).basis,'collecting_window')
        self.assertEqual(restored.decide(SESSION,CFG,self.now+15).basis,'model_decides_window')


class ColdTopicTests(unittest.TestCase):
    def setUp(self):
        from hakimi.bridge.cold_topics import ColdTopics
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve()
        self.cold=ColdTopics(self.base)
        self.now=time.time()
        self.cfg={'bot_profile':{'active_groups':['100']},'group_weights':{'100':{'name':'群友181','theme':'数学与日常'}},
                  'lurker_schedule':{'proactive_topic':{'enable':True,'quiet_window_seconds':72000,'window_jitter_seconds':0}}}
        self.cold.bootstrap(self.cfg,self.now)

    def open_topic(self,at=None,trigger='cold-1'):
        at=self.now+72001 if at is None else at
        self.cold.offered(SESSION,trigger,at)
        self.cold.sent(SESSION,trigger,'最近有哪道数学题把你们卡住了','777',self.cfg,at+1)
        return at+1

    def test_quiet_window_starts_on_enable_and_new_messages_reset_it(self):
        self.assertIsNone(self.cold.due(self.cfg,self.now+71999))
        self.assertEqual(self.cold.due(self.cfg,self.now+72000),SESSION)
        self.cold.on_message(SESSION,'普通新消息','22','1',self.cfg,self.now+72000)
        self.assertIsNone(self.cold.due(self.cfg,self.now+72001))
        self.assertEqual(self.cold.due(self.cfg,self.now+144001),SESSION)

    def test_success_records_both_sides_once_in_this_group_memory(self):
        at=self.open_topic()
        self.cold.on_message(SESSION,'我还卡在三角函数那一步','22','2',self.cfg,at+60,reply_to='777')
        self.cold.on_message(SESSION,'我还卡在三角函数那一步','22','2',self.cfg,at+61,reply_to='777')
        path=self.base/'state/session_memory.jsonl'
        notes=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(notes),1)
        self.assertEqual(notes[0]['kind'],'episode')
        self.assertEqual(notes[0]['session_key'],SESSION)
        self.assertIn('数学题',notes[0]['note']);self.assertIn('三角函数',notes[0]['note'])
        self.assertIn('一次',notes[0]['note'])
        self.assertEqual(self.cold.context(SESSION,self.cfg)['recent_attempts'][0]['outcome'],'success')

    def test_no_response_is_remembered_and_next_attempt_waits_another_window(self):
        at=self.open_topic()
        self.assertIsNone(self.cold.due(self.cfg,at+7201))
        self.cold.progress({},at+72001)
        context=self.cold.context(SESSION,self.cfg)
        self.assertEqual(context['recent_attempts'][0]['outcome'],'no_response')
        self.assertIn('数学题',context['recent_attempts'][0]['topic'])
        self.assertEqual(self.cold.due(self.cfg,at+72001),SESSION)

    def test_a_reply_several_hours_later_can_still_make_the_topic_successful(self):
        at=self.open_topic()
        self.cold.on_message(SESSION,'刚看到，三角函数那题我也想问','22','late',self.cfg,at+3*3600,reply_to='777')
        self.assertEqual(self.cold.context(SESSION,self.cfg)['recent_attempts'][0]['outcome'],'success')

    def test_explicit_negative_feedback_is_not_recorded_as_a_success(self):
        at=self.open_topic()
        self.cold.on_message(SESSION,'别主动发言了','22','2',self.cfg,at+60,reply_to='777')
        self.assertFalse((self.base/'state/session_memory.jsonl').exists())
        self.assertEqual(self.cold.context(SESSION,self.cfg)['recent_attempts'][0]['outcome'],'negative')

    def test_irrelevant_messages_restart_quiet_clock_without_inventing_success(self):
        at=self.open_topic()
        self.cold.on_message(SESSION,'今晚出去吃火锅','22','2',self.cfg,at+60)
        self.assertFalse((self.base/'state/session_memory.jsonl').exists())
        self.assertIsNone(self.cold.due(self.cfg,at+72001))

    def test_restart_preserves_clock_and_failed_attempts(self):
        from hakimi.bridge.cold_topics import ColdTopics
        at=self.open_topic()
        restored=ColdTopics(self.base)
        restored.bootstrap(self.cfg,at+7201)
        self.assertIsNone(restored.due(self.cfg,at+7202))
        restored.progress({},at+72001)
        self.assertEqual(restored.context(SESSION,self.cfg)['recent_attempts'][0]['outcome'],'no_response')

    def test_restart_reuses_existing_message_index_instead_of_writing_a_second_clock(self):
        from hakimi.bridge.cold_topics import ColdTopics
        at=self.now+72001
        self.cold.on_message(SESSION,'普通新消息','22','1',self.cfg,at)
        self.cold.index.observe({'session_key':SESSION,'user_id':'22','message_id':'1','received_at':at,'text':'普通新消息'})
        restored=ColdTopics(self.base)
        self.assertIsNone(restored.due(self.cfg,at+10))
        self.assertEqual(restored.due(self.cfg,at+72001),SESSION)

    def test_skip_or_delivery_failure_does_not_create_a_memory_or_retry_storm(self):
        at=self.now+72001
        self.cold.offered(SESSION,'skip',at)
        self.cold.progress({'status':'completed','trigger_id':'skip','outcome':'no_reply'},at+10)
        self.assertEqual(self.cold.context(SESSION,self.cfg)['recent_attempts'],[])
        self.assertIsNone(self.cold.due(self.cfg,at+1000))
        self.assertFalse((self.base/'state/session_memory.jsonl').exists())

    def test_permissions_and_ignored_groups_are_rechecked_without_model_calls(self):
        at=self.now+72001
        self.cold.permission_denied(SESSION,at)
        self.assertIsNone(self.cold.due(self.cfg,at+599))
        self.assertEqual(self.cold.due(self.cfg,at+600),SESSION)
        self.cfg['bot_profile']['ignored_groups']=['100']
        self.assertIsNone(self.cold.due(self.cfg,at+601))

    def test_both_backends_get_group_history_and_failure_guidance_without_persona_changes(self):
        at=self.open_topic()
        self.cold.progress({},at+72001)
        (self.base/'style_guide.md').write_text('# 原本人设\n日常短句RP',encoding='utf-8')
        data={'session_key':SESSION,'wake_mode':'冷场主动抛话题','recent_context_messages':[],
              'cold_topic_request':self.cold.context(SESSION,self.cfg)}
        for prompt in (render_prompt(data,self.base),fallback_prompt(data,self.base)):
            self.assertIn('原本人设',prompt);self.assertIn('群友181',prompt)
            self.assertIn('no_response',prompt);self.assertIn('避开recent_attempts',prompt)
            self.assertIn('不回复历史旧消息',prompt)


class FeedingBridgeTests(unittest.IsolatedAsyncioTestCase):
    loop_factory = OfflineEventLoop

    async def test_ingress_records_before_wake_without_changing_mention_scheduler(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            base = Path(folder)
            code = feeding.arm_binding(base)
            cfg = {**CFG,'topic_bias':{}}
            engine = bridge.DynamicHakimiEngine()
            with contextlib.ExitStack() as stack:
                for name,value in {'BASE_DIR':base,'INBOX_LOG':base/'state/inbox.jsonl',
                                   'engine':engine,'RECENT_EVENT_IDS':OrderedDict()}.items():
                    stack.enter_context(patch.object(bridge,name,value))
                stack.enter_context(patch.object(bridge,'load_config',return_value=cfg))
                wake = stack.enter_context(patch.object(bridge,'enqueue_wake_beacon'))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                def event(mid,text,user=22):
                    return {'post_type':'message','message_type':'group','group_id':100,
                            'user_id':user,'self_id':99,'message_id':mid,'raw_message':'[CQ:at,qq=99] '+text}
                await bridge.process_incoming_event(event(1,'/绑定投喂 '+code))
                await bridge.process_incoming_event(event(2,'/投喂罐头'))
                await bridge.process_incoming_event(event(2,'/投喂罐头'))
                await bridge.process_incoming_event(event(3,'/投喂罐头',user=33))
                self.assertEqual(json.loads((base/'state/feeding.json').read_text(encoding='utf-8'))['total'],1)
                self.assertEqual(wake.call_count,3)
                self.assertEqual([call.args[1] for call in wake.call_args_list],['即时唤醒(被@/私聊)']*3)


if __name__ == "__main__":
    unittest.main()
