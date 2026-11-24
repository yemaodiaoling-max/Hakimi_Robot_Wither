"""Session isolation, provenance, retention and prompt regressions with synthetic speech."""
import asyncio
import gc
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from hakimi.memory.memory_store import MemoryStore
from hakimi.models import search_routing, web_access
from hakimi.models.fallback_prompt import fallback_prompt
from hakimi.prompt.prompt_context import build_context, render_vertex_prompt
from hakimi.voice import delivery, store
from test_delivery_guard import OfflineEventLoop


class VoicePrivacyTests(unittest.TestCase):
    group = "group:65100"
    other = "group:65101"
    user = "7717"
    private = "private:" + user

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        env = patch.dict(os.environ, HAKIMI_BASE=str(self.base), HAKIMI_ROOT=str(self.base),
                         HAKIMI_RUNTIME=str(self.base), HAKIMI_EMBEDDING_DISABLE="1")
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(gc.collect)
        (self.base / "state").mkdir()
        (self.base / "voice_config.json").write_text('{"enabled":true}', encoding="utf-8")
        self.now = time.time()
        self.memory = MemoryStore(self.base)
        self.memory.next_prune = time.monotonic() + 3600

    def row(self, session, mid, text, **attrs):
        return dict(session_key=session, message_id=mid, text=text, user_id=self.user,
                    nickname="样本", received_at=self.now-2, **attrs)

    def payload(self, session, rows):
        return dict(session_key=session, trigger_id="fixture-"+rows[-1]["message_id"], created_at=self.now,
                    recent_context_messages=rows, reply_target_message_id=rows[-1]["message_id"])

    def receipt(self, session, mid, speech):
        event = dict(message_type=session.split(":")[0], group_id=session.split(":")[1], user_id=self.user,
                     message_id=mid, raw_message="", _voice_received_at=self.now-2)
        self.assertTrue(store.enqueue(self.base, event)["accepted"])
        self.assertTrue(store.enqueue(self.base, event)["duplicate"])
        job = store.claim(self.base)
        store.finish(self.base, job, text=speech, duration=2)
        row = self.row(session, mid, f"[语音消息，voice.read message_id={mid}]", channel="voice")
        self.memory.observe(row)
        store.acknowledge(self.base, session, mid)
        with self.memory.db() as db:
            rid = db.execute("SELECT id FROM records WHERE session=? AND origin=? AND channel='voice'",
                             (session, mid)).fetchone()[0]
        return job, row, rid

    def test_user_in_two_groups_and_private_can_only_recall_source_session(self):
        for ordinal, (source, term) in enumerate(((self.group, "红外光谱标定样品"), (self.private, "蓝耳翠鸟迁徙周期"))):
            mid = str(901+ordinal)
            _, _, rid = self.receipt(source, mid, "计划复核"+term+"与本地统计值。")
            for session in (self.group, self.other, self.private):
                for cross in (False, True):
                    with self.subTest(source=source, session=session, cross=cross):
                        expected = source == session
                        direct = store.read(self.base, session, mid)
                        self.assertEqual(term in direct.get("text", ""), expected)
                        record = self.memory.read("M"+str(rid), session, [self.user], cross_groups=cross, source="message")
                        self.assertEqual(bool(record), expected)
                        if expected:
                            self.assertIn(term, record["text"])
                            self.assertEqual(record["text_kind"], "automatic_transcription")
                        target = self.row(session, "970", "你还记得吗？")
                        hits = self.memory.retrieve(session, [target], target, query=term, cross_groups=cross,
                                                    budget=1500, limit=6)
                        self.assertEqual(any(term in line for line in hits), expected)

    def test_stale_worker_and_original_cleanup(self):
        job, _, rid = self.receipt(self.group, "903", "标记 octagonreference 已经确认。")
        store.finish(self.base, job, text="迟到的工作结果")
        self.assertIsNone(store.ready(self.base))
        self.assertIn("octagonreference", store.read(self.base, self.group, "903")["text"])
        self.assertEqual([hit["message_id"] for hit in store.search(self.base,self.group,"octagonreference")], ["903"])
        for state, expires, text in (("deleted",0,"占位"), ("active",self.now-1,"占位"), ("active",0,"")):
            with self.subTest(state=state, expires=expires, text=text):
                with self.memory.db() as db:
                    db.execute("UPDATE records SET state=?,expires=?,text=? WHERE id=?",(state,expires,text,rid))
                self.assertIn("error", store.read(self.base,self.group,"903"))
                self.assertFalse(store.search(self.base,self.group,"octagonreference"))
                self.assertIsNone(self.memory.read("M"+str(rid),self.group,[self.user],source="message"))
        store.prune(self.base)
        with store.database(self.base) as db:
            self.assertFalse(db.execute("SELECT text FROM jobs WHERE session=? AND mid=?", (self.group,"903")).fetchone()[0])

    def test_rendered_transcript_keeps_real_tail_and_skips_rolling_memory(self):
        mid = "904"
        speech = "我喜欢 nocturnalopening。"+("普通段落，"*1400)+"中间的镀锌铌合金。"+("普通段落，"*20)+"末行 realtailmarker。"
        _, row, _ = self.receipt(self.group, mid, speech)
        self.assertEqual(store.read(self.base,self.group,mid,6000,6000)["text"], speech[6000:])
        self.assertTrue(store.search(self.base,self.group,"镀锌铌合金"))
        for session, rows, expected in (
            (self.group,[row],True),
            (self.group,[self.row(self.group,"905","上一条是什么？",reply_to=mid)],True),
            (self.other,[self.row(self.other,"906","上一条是什么？",reply_to=mid)],False),
        ):
            for render in (render_vertex_prompt, fallback_prompt):
                with self.subTest(session=session, renderer=render.__name__):
                    prompt = render(self.payload(session,rows),self.base)
                    self.assertEqual("nocturnalopening" in prompt, expected)
                    self.assertEqual("realtailmarker" in prompt, expected)
        others = [self.row(self.group,str(920+i),"普通话题") for i in range(16)]
        build_context(self.payload(self.group,[row]+others[:11]),self.base)
        build_context(self.payload(self.group,others[-12:]),self.base)
        with self.memory.db() as db:
            self.assertFalse(db.execute("SELECT 1 FROM memory_originals WHERE text LIKE '%nocturnalopening%'").fetchone())
        for snapshot in (self.base/"state"/"rolling_context").glob("*.json"):
            self.assertNotIn("nocturnalopening",snapshot.read_text(encoding="utf-8"))

    def test_spoken_network_opt_out_prevents_prefetch_and_tool_budget_reservation(self):
        (self.base/"search_routing.json").write_text('{"enabled":true}',encoding="utf-8")
        _, row, _ = self.receipt(self.group,"907",("普通内容。"*1400)+"别联网，按已有材料回答。")
        payload = self.payload(self.group,[row])
        with patch.object(web_access,"run") as request:
            attempted = search_routing.prepare(payload,self.base,judge=Mock(return_value={"search":True}))
            self.assertFalse(attempted["network_allowed"])
            request.assert_not_called()
        with patch.object(web_access.web_sources,"reserve_access") as budget:
            result = web_access.run("web.search",{"query":"样本"},3,self.base,payload,transport=Mock())
            self.assertEqual(result["error"],"WEB_DISABLED_FOR_THIS_TURN")
            budget.assert_not_called()
        self.assertFalse(build_context(payload,self.base)["network_policy"]["allowed"])

    def deliver_once(self, callback):
        job = store.ready(self.base)
        self.assertIsNotNone(job)
        with patch.object(store, "ready", side_effect=[job, asyncio.CancelledError()]):
            with asyncio.Runner(loop_factory=OfflineEventLoop) as runner:
                with self.assertRaises(asyncio.CancelledError):
                    runner.run(delivery.deliver(self.base,callback))

    def test_failed_audio_logs_a_reason_and_preserves_only_native_text(self):
        from hakimi.console.message_log import recent_messages
        cases = [
            ("954", {"enabled":True}, "", "未听清"),
            ("955", {"enabled":False}, "同时打的文字", "未启用"),
            ("956", {"enabled":True,"max_pending":0}, "另一条文字", "队列已满"),
        ]
        for mid, config, typed, expected_reason in cases:
            with self.subTest(mid=mid):
                (self.base/"voice_config.json").write_text(json.dumps(config),encoding="utf-8")
                event = dict(message_type="group", group_id=self.group.split(":")[1], user_id=self.user,
                             sender={"nickname":"测试"+mid}, message_id=mid, raw_message=typed,
                             _voice_received_at=self.now, _voice_attachments=[{"url":"sensitive-fixture-url"}])
                accepted = store.enqueue(self.base,event)["accepted"]
                if accepted:
                    store.finish(self.base,store.claim(self.base),error="NO_CLEAR_SPEECH")
                process_event = AsyncMock(return_value={"handled":True})
                self.deliver_once(process_event)
                if typed:
                    process_event.assert_awaited_once()
                    forwarded = process_event.await_args.args[0]
                    self.assertEqual(forwarded["raw_message"],typed)
                    self.assertNotIn("_voice_semantic",forwarded)
                else:
                    process_event.assert_not_awaited()
                entries = [r for r in recent_messages(self.base,self.group) if r["actor"]=="测试"+mid]
                self.assertEqual(len(entries),1)
                self.assertIn(expected_reason,entries[0]["decision"])
                self.assertNotIn("sensitive-fixture-url",str(entries))
                self.assertIn("error",store.read(self.base,self.group,mid))

    def test_transcript_is_readable_at_the_instant_a_wake_is_published(self):
        from hakimi.bridge import bridge_daemon as bridge
        mid = "908"
        event = dict(post_type="message", message_type="group", group_id=self.group.split(":")[1],
                     self_id=9000000000000000001, user_id=self.user, message_id=mid,
                     sender={"nickname":"样本"}, raw_message="问题", _voice_received_at=self.now)
        store.enqueue(self.base,event)
        store.finish(self.base,store.claim(self.base),text="别联网。 atomicwakefixture")
        engine = Mock()
        engine.msg_lookup = {}
        engine.conversation_scheduler.groups = {}
        engine.unread_counts = {}
        engine.window_interest_score = {}
        engine.record_and_score.side_effect = lambda event, cfg, clean, *args: (True, [], clean)
        observed = []

        def capture_wake(*args):
            clean = engine.record_and_score.call_args.args[2]
            self.assertNotIn("atomicwakefixture",clean)
            self.assertIn("voice.read",clean)
            payload = self.payload(self.group,[self.row(self.group,mid,clean)])
            ctx = build_context(payload,self.base)
            observed.append(("atomicwakefixture" in str(ctx),ctx["network_policy"]["allowed"]))

        config = {"bot_profile": {}, "topic_bias": {"ignored_keywords":[]}}
        with patch.multiple(bridge, BASE_DIR=self.base, INBOX_LOG=self.base/"state"/"chat_history.jsonl",
                            RECENT_EVENT_IDS={}, engine=engine, load_config=Mock(return_value=config),
                            session_is_allowed=Mock(return_value=True), enqueue_wake_beacon=capture_wake,
                            record_interaction=Mock(), cold_settings=Mock(return_value={})), \
             patch("hakimi.bridge.interaction_policy.ignored", return_value=False):
            self.deliver_once(bridge.process_incoming_event)
        self.assertEqual(observed,[(True,False)])
        with self.memory.db() as db:
            count = db.execute("SELECT COUNT(*) FROM records WHERE session=? AND origin=? AND kind='chat'",
                               (self.group,mid)).fetchone()[0]
        self.assertEqual(count,1)


if __name__ == "__main__":
    unittest.main()
