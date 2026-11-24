"""Behavioral simulations, no QQ, model requests or production state writes."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hakimi.bridge import bridge_daemon as bridge
from hakimi.bridge.conversation_scheduler import ConversationScheduler, features
from tests.test_delivery_guard import OfflineEventLoop


def config():
    return {"conversation_scheduler": {"enabled": True, "mention_buffer_seconds": 0, "busy_group_sensitivity": {"100": 1.8}},
            "bot_profile": {"self_qq": 99, "random_peek_groups": ["100", "200"],
                            "low_frequency_groups": ["300"], "name": "offline", "persona": "offline"},
            "participation_policy": {"study_groups": ["300"]}, "topic_bias": {}, "user_bias": {},
            "lurker_schedule": {}, "fallback": {"enabled": True}}


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.s = ConversationScheduler()
        self.cfg = config()
        self.g = "group:100"

    def add(self, text="海祇岛的圣土化从哪些游戏文本可以核对？", at=1000, mid=1, **kw):
        self.s.observe(self.g, text, "22", mid, at, **kw)

    def reply(self):
        self.add()
        self.s.offered_turn(self.g, 1050, "initial")
        self.s.consume(self.g)
        self.s.sent_reply(self.g, "海祇岛圣土化可以查世界任务文本", 1070, 123, "initial")
        self.s.stop_listening(self.g)  # These tests isolate ordinary participation scoring.

    def test_mention_is_immediate_even_with_pressure_and_peek_disabled(self):
        self.reply()
        self.cfg["lurker_schedule"]["enable_random_window_peek"] = False
        self.add("请帮忙看这张图片", at=1071, mid=2, mentioned=True, image=True)
        decision = self.s.decide(self.g, self.cfg, 1071)
        self.assertTrue(decision.ready)
        self.assertEqual((decision.mode, decision.wait), ("mention", 0))

    def test_default_mention_collects_one_second_without_sliding(self):
        del self.cfg['conversation_scheduler']['mention_buffer_seconds']
        self.add(at=1000, mentioned=True)
        self.assertEqual(self.s.decide(self.g,self.cfg,1000).wait,1)
        self.add('补充条件',at=1000.3,mid=2)
        self.add('再问一句',at=1000.5,mid=3,mentioned=True)
        self.assertFalse(self.s.decide(self.g,self.cfg,1000.9).ready)
        self.assertTrue(self.s.decide(self.g,self.cfg,1001).ready)
        self.s.consume(self.g)
        self.add(at=1010,mid=4,mentioned=True)
        self.assertEqual(self.s.decide(self.g,self.cfg,1010).wait,1)

    def test_collecting_mention_reserves_priority_over_ready_background(self):
        self.cfg['conversation_scheduler']['mention_buffer_seconds']=4
        self.s.observe('group:200','这个数学题怎么解？','33','old',800)
        self.add(at=1000,mentioned=True)
        self.assertEqual(self.s.choose(self.cfg,1000.5),(None,None))
        self.assertEqual(self.s.choose(self.cfg,1001)[0],self.g)

    def test_ignored_group_cannot_be_reenabled_by_a_mention(self):
        self.add(mentioned=True)
        self.cfg["bot_profile"]["ignored_groups"] = ["100"]
        self.assertFalse(self.s.decide(self.g, self.cfg, 1001).ready)

    def test_unaddressed_question_accumulates_then_becomes_eligible(self):
        self.add()
        first = self.s.decide(self.g, self.cfg, 1000)
        self.assertFalse(first.ready)
        self.assertGreater(first.wait, 0)
        self.assertTrue(self.s.decide(self.g, self.cfg, 1120).ready)

    def test_busy_other_groups_cannot_starve_a_quiet_study_groups_new_input(self):
        self.s.global_calls, self.s.global_at = 30, 1000
        self.s.observe("group:300", "这一步的条件还得再核对一下", "22", "study-1", 1000)
        decision = self.s.decide("group:300", self.cfg, 1120)
        self.assertTrue(decision.ready)
        self.assertEqual(decision.mode, "batch")
        self.s.offered_turn("group:300", 1120, "study-turn")
        self.s.consume("group:300")
        self.s.outcome({"status": "completed", "trigger_id": "study-turn",
                        "payload": {"session_key": "group:300"}, "outcome": "no_reply"}, 1121)
        self.s.observe("group:300", "这一步的条件还得再核对一下", "22", "study-2", 1122)
        self.assertFalse(self.s.decide("group:300", self.cfg, 1130).ready)

    def test_study_fairness_does_not_turn_pure_acknowledgements_into_model_calls(self):
        for i, text in enumerate(("哈哈", "谢谢", "[QQ表情]")):
            self.s.observe("group:300", text, "22", i, 1000+i)
        self.assertFalse(self.s.decide("group:300", self.cfg, 1120).ready)

    def test_busy_conversation_decreases_score_and_delays_unsolicited_turn(self):
        self.add()
        quiet = self.s.decide(self.g, self.cfg, 1040)
        for i in range(1, 40):
            self.s.observe(self.g, "海祇岛剧情文本仍有其他解释", str(i % 6), i+1, 1000+i)
        busy = self.s.decide(self.g, self.cfg, 1040)
        self.assertGreater(busy.activity, quiet.activity)
        self.assertGreater(busy.wait, quiet.wait)
        # Compare equal-size content batches to isolate active humans from utility.
        self.s.group(self.g).pending.clear()
        self.add()
        same_content_busy = self.s.decide(self.g, self.cfg, 1040)
        self.assertLess(same_content_busy.score, quiet.score)

    def test_related_followup_is_faster_than_unrelated_topic(self):
        self.reply()
        self.add("海祇岛圣土化的世界任务文本具体是哪篇？", at=1080, mid=2)
        related = self.s.decide(self.g, self.cfg, 1080)
        self.assertEqual(related.mode, "continuation")
        self.s.consume(self.g)
        self.add("今晚吃火锅还是去吃烧烤？", at=1080, mid=3)
        unrelated = self.s.decide(self.g, self.cfg, 1080)
        self.assertEqual(unrelated.mode, "batch")
        self.assertGreater(related.score, unrelated.score)

    def test_warm_related_interlocutor_reaches_model_before_ordinary_chatter_buries_it(self):
        self.reply()
        self.s.observe(self.g,'我才是群主好吧','22','during-generation',1063)
        self.s.observe(self.g,'锐评一下群友','33','other-person',1091)
        self.s.observe(self.g,'图片','33','picture',1095,image=True)
        self.s.observe(self.g,'其实我是你爹','22','related-banter',1098)
        decision=self.s.decide(self.g,self.cfg,1102)
        self.assertEqual(decision.mode,'continuation')
        self.assertGreaterEqual(decision.score,.45)
        self.assertLessEqual(decision.wait,8)
        self.assertTrue(self.s.decide(self.g,self.cfg,1110).ready)

    def test_topic_change_does_not_inherit_old_relevance(self):
        self.reply()
        self.add("海祇岛圣土化的世界任务文本具体是哪篇？", at=1080, mid=2)
        self.add("今晚吃火锅还是去吃烧烤？", at=1081, mid=3)
        self.assertEqual(self.s.decide(self.g, self.cfg, 1082).mode, "batch")

    def test_reference_to_own_message_counts_only_in_same_group(self):
        self.reply()
        self.add("能展开讲讲吗？", at=1080, mid=2, reply_to=123)
        self.assertEqual(self.s.decide(self.g, self.cfg, 1081).mode, "continuation")
        self.s.observe("group:200", "能展开讲讲吗？", "22", 3, 1080, reply_to=123)
        self.assertEqual(self.s.decide("group:200", self.cfg, 1081).mode, "batch")

    def test_four_confirmed_turns_start_hard_cooldown(self):
        self.reply()
        for i in range(3):
            self.s.sent_reply(self.g, "回复", 1090+i*20, 200+i)
        self.add("海祇岛圣土化的世界任务具体是哪篇？", at=1131, mid=8)
        decision = self.s.decide(self.g, self.cfg, 1132)
        self.assertFalse(decision.ready)
        self.assertEqual(decision.basis, "dialogue_cooldown")
        self.add("请回答", at=1133, mid=9, mentioned=True)
        self.assertTrue(self.s.decide(self.g, self.cfg, 1133).ready)
        self.s.sent_reply(self.g, "定向回答", 1134)
        self.assertEqual(self.s.group(self.g).listen_round, 0)

    def test_study_followup_uses_relevance_instead_of_two_hour_block(self):
        self.g = "group:300"
        self.reply()
        self.add("海祇岛圣土化的世界任务文本具体是哪篇？", at=1080, mid=2)
        decision = self.s.decide(self.g, self.cfg, 1130)
        self.assertEqual(decision.mode, "continuation")
        self.assertTrue(decision.ready)

    def test_faces_and_plain_acknowledgements_do_not_trigger_a_model(self):
        for i, text in enumerate(["[CQ:face,id=1]", "[表情:/惊讶]", "好的", "哈哈"]):
            self.add(text, at=1000+i, mid=i)
        self.assertFalse(self.s.decide(self.g, self.cfg, 1100).ready)

    def test_rolling_buffer_cannot_postpone_the_first_pending_receipt(self):
        self.add()
        for i in range(100):
            self.add("还在讨论剧情任务中的不同线索", at=1001+i, mid=100+i)
        self.assertEqual(self.s.group(self.g).pending_since, 1000)
        self.assertEqual(len(self.s.group(self.g).pending), 64)

    def test_real_silence_adds_pressure_but_timeout_does_not_claim_disinterest(self):
        self.add()
        self.s.offered_turn(self.g, 1050)
        self.s.outcome({"status": "completed", "trigger_id": "one", "outcome": "no_reply",
                        "payload": {"session_key": self.g}}, 1070)
        self.assertEqual(self.s.group(self.g).skips, 1)
        self.s.outcome({"status": "completed", "trigger_id": "two", "outcome": "fallback_timeout",
                        "payload": {"session_key": self.g}}, 1071)
        self.assertEqual(self.s.group(self.g).skips, 1)

    def test_pressure_and_topic_survive_restart_without_plaintext_messages(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            self.s.restore(folder, 1000)
            self.reply()
            before = self.s.decide(self.g, self.cfg, 1080).pressure
            raw = self.s.path.read_text(encoding="utf-8")
            self.assertNotIn("海祇岛", raw)
            restored = ConversationScheduler()
            restored.restore(folder, 1080)
            self.assertTrue(restored.healthy)
            self.assertEqual(restored.group(self.g).own_ids, self.s.group(self.g).own_ids)
            self.assertEqual(restored.group(self.g).anchor, features("海祇岛圣土化可以查世界任务文本"))
            self.assertEqual(restored.group(self.g).reply_users, self.s.group(self.g).reply_users)
            self.assertEqual(restored._pressures(restored.group(self.g), 1080), self.s._pressures(self.s.group(self.g), 1080))

    def test_corrupt_checkpoint_suppresses_unsolicited_but_allows_mentions(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder)/"state"/"conversation_scheduler.json"
            path.parent.mkdir()
            path.write_text('{"version":999}', encoding="utf-8")
            self.s.restore(folder, 1000)
            self.add()
            self.assertFalse(self.s.decide(self.g, self.cfg, 1100).ready)
            self.add(at=1101, mid=2, mentioned=True)
            self.assertTrue(self.s.decide(self.g, self.cfg, 1101).ready)

    def test_delayed_delivery_anchors_the_delivered_turn_not_a_newer_offer(self):
        self.add("海祇岛的圣土化有什么文本依据？")
        self.s.offered_turn(self.g, 1050, "first")
        self.s.consume(self.g)
        self.add("两个矩阵乘法应该如何计算？", at=1055, mid=2)
        self.s.offered_turn(self.g, 1060, "second")
        self.s.consume(self.g)
        self.s.sent_reply(self.g, "可以查这篇任务对白。", 1070, 123, "first")
        self.assertEqual(self.s.group(self.g).source, features("海祇岛的圣土化有什么文本依据？"))
        self.assertIn("second", self.s.offer_sources)

    def test_elliptical_followup_from_the_person_answered_can_continue(self):
        self.reply()
        self.add("那为什么不能这样？", at=1080, mid=2)
        decision = self.s.decide(self.g, self.cfg, 1080)
        self.assertEqual(decision.mode, "continuation")
        self.assertEqual(decision.basis, "contextual_followup")
        later = self.s.decide(self.g, self.cfg, 1140)
        self.assertTrue(later.ready)
        self.assertEqual(later.mode, "continuation")

    def test_waiting_itself_cannot_erase_a_received_followups_relevance(self):
        self.reply()
        self.add("圣土化真的能变好吗？", at=1080, mid=2)
        early = self.s.decide(self.g, self.cfg, 1080)
        later = self.s.decide(self.g, self.cfg, 1160)
        self.assertEqual(early.mode, "continuation")
        self.assertEqual(later.relevance, early.relevance)
        self.assertEqual(later.mode, "continuation")
        self.assertTrue(later.ready)

    def test_ack_after_a_question_does_not_quarter_its_relevance(self):
        self.reply()
        self.add("那为什么不能这样？", at=1080, mid=2)
        before = self.s.decide(self.g, self.cfg, 1080)
        self.add("哈哈", at=1081, mid=3)
        self.add("好的", at=1082, mid=4)
        after = self.s.decide(self.g, self.cfg, 1083)
        self.assertEqual(after.relevance, before.relevance)
        self.assertEqual(after.mode, "continuation")

    def test_new_topic_or_addressing_another_member_does_not_inherit_fast_followup(self):
        self.reply()
        for text, extra in [("换个话题，怎么做红烧排骨？", {}),
                            ("那晚上吃火锅还是烧烤？", {}),
                            ("那你再展开讲讲？", {"addressed_other": True}),
                            ("海祇岛圣土化是什么？", {"addressed_other": True})]:
            with self.subTest(text=text):
                self.s.consume(self.g)
                self.add(text, at=1080, mid=2, **extra)
                decision = self.s.decide(self.g, self.cfg, 1081)
                self.assertEqual(decision.mode, "batch")
                self.assertFalse(decision.ready)

    def test_new_related_image_from_same_person_is_a_candidate_not_generic_image_text(self):
        self.reply()
        marker = "[群友发图(可用view_file看图): E:/private/example.jpg]"
        self.assertEqual(features(marker), set())
        self.add(marker, at=1080, mid=2, image=True)
        decision = self.s.decide(self.g, self.cfg, 1080)
        self.assertEqual(decision.mode, "continuation")
        self.assertEqual(decision.basis, "image_followup")

    def test_message_during_generation_does_not_implicitly_reply_to_unseen_answer(self):
        self.reply()
        self.add("那为什么不能这样？", at=1060, mid=2)
        self.assertNotEqual(self.s.decide(self.g, self.cfg, 1080).basis, "contextual_followup")

    def test_explicit_reply_to_an_older_own_message_survives_ordinary_wait(self):
        self.reply()
        self.add("能展开讲讲吗？", at=1700, mid=2, reply_to=123)
        early = self.s.decide(self.g, self.cfg, 1700)
        late = self.s.decide(self.g, self.cfg, 1750)
        self.assertEqual(early.basis, "explicit_reference")
        self.assertEqual(late.relevance, early.relevance)
        self.assertEqual(late.mode, "continuation")

    def test_offer_uses_the_actual_model_visible_input_to_capture_audience(self):
        self.add("旧讨论", mid=1)
        actual = [{"user_id": "33", "text": "这个问题怎么解决？"}, {"user_id": "SELF", "text": "自己的旧回复"}]
        self.s.offered_turn(self.g, 1050, "visible", messages=actual)
        self.s.sent_reply(self.g, "先分析条件", 1070, 123, "visible")
        from hakimi.bridge.conversation_scheduler import actor
        self.assertEqual(self.s.group(self.g).reply_users, {actor("33")})
        self.assertEqual(self.s.group(self.g).source, features("这个问题怎么解决？"))

    def test_decision_audit_explains_wait_and_readiness_without_chat_text(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            self.s.restore(folder, 1000)
            self.reply()
            self.add("那为什么不能这样？", at=1080, mid=2)
            decision = self.s.decide(self.g, self.cfg, 1080)
            self.s.audit_decision(self.g, decision, 1080)
            self.s.audit_decision(self.g, decision, 1081)
            self.s.audit_decision(self.g, self.s.decide(self.g, self.cfg, 1140), 1140, "selection")
            path = Path(folder)/"state"/"conversation_decisions.jsonl"
            raw = path.read_text(encoding="utf-8")
            rows = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual([row["gate"] for row in rows], ["waiting_batch", "ready"])
            self.assertEqual(rows[-1]["basis"], "contextual_followup")
            self.assertNotIn("为什么", raw)
            self.assertNotIn("user_id", raw)


class AdaptiveBridgeTests(unittest.IsolatedAsyncioTestCase):
    loop_factory = OfflineEventLoop

    def setUp(self):
        self.engine = bridge.DynamicHakimiEngine()
        self.cfg = config()

    def test_receipt_selects_mention_without_sampling_or_model_calls(self):
        event = {"message_type": "group", "group_id": 100, "self_id": 99, "user_id": 22,
                 "message_id": 1, "raw_message": "[CQ:at,qq=99] 请帮忙讲解一下"}
        with patch.object(bridge.random, "random", side_effect=AssertionError("no sampling")):
            instant, _, _ = self.engine.record_and_score(event, self.cfg)
        self.assertTrue(instant)
        self.assertTrue(self.engine.study_schedule_event.is_set())

    def test_buffered_mention_keeps_original_target_and_includes_supplement(self):
        self.cfg['conversation_scheduler']['mention_buffer_seconds']=4
        event={'message_type':'group','group_id':100,'self_id':99,'user_id':22,
               'message_id':71,'raw_message':'[CQ:at,qq=99] 帮我看看这个条件'}
        with patch.object(bridge.time,'time',return_value=1000):
            self.assertTrue(self.engine.record_and_score(event,self.cfg)[0])
        with patch.object(bridge.time,'time',return_value=1002):
            self.engine.record_and_score(dict(event,message_id=72,raw_message='补充：自变量是正数'),self.cfg)
        coor=Mock()
        coor.snapshot.return_value={'status':'idle'}
        coor.publish.return_value={'accepted':True,'trigger_id':'buffered'}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            with patch('hakimi.bridge.conversation_judge.mention_context', side_effect=lambda entry, rows, *a, **kw: rows), \
                    patch.object(bridge,'engine',self.engine),patch.object(bridge,'load_config',return_value=self.cfg), \
                    patch.object(bridge,'coordinator_for',return_value=coor), \
                    patch.object(bridge,'PENDING_TRIGGER',Path(folder)/'pending.json'), \
                    patch.object(bridge.time,'time',return_value=1004):
                bridge.enqueue_wake_beacon('group:100','即时唤醒(被@/私聊)',5,[],self.cfg)
        payload=coor.publish.call_args.args[0]
        self.assertEqual(payload['reply_target_message_id'],'71')
        self.assertEqual(payload['pending_explicit_question_ids'],['71'])
        self.assertTrue(any(str(row['message_id'])=='72' for row in payload['recent_context_messages']))

    def test_ack_does_not_clear_messages_received_during_generation(self):
        self.engine.conversation_scheduler.observe("group:100", "上一轮的问题", "22", 1, 1000)
        self.engine.clear_unread("group:100")
        self.engine.conversation_scheduler.observe("group:100", "生成期间新问题", "22", 2, 1010)
        self.engine.unread_counts["group:100"] = 1
        with patch.object(bridge.time, "time", return_value=1020):
            self.engine.mark_replied("group:100", self.cfg, reply_text="上一轮的回复", message_id=33)
        self.assertEqual(self.engine.unread_counts["group:100"], 1)
        self.assertEqual(len(self.engine.conversation_scheduler.group("group:100").pending), 1)
        self.assertNotIn("group:100", self.engine.cooldown_until)

    async def test_busy_turn_keeps_unread_without_selection_or_offer(self):
        self.engine.conversation_scheduler.observe("group:100", "数学问题怎么解决？", "22", 1, 1000)
        coordinator = Mock()
        coordinator.snapshot.return_value = {"status": "primary_active"}
        class StopLoop(Exception):
            pass
        async def stop(awaitable, **_kw):
            awaitable.close()
            raise StopLoop
        with patch.object(bridge, "engine", self.engine), patch.object(bridge, "load_config", return_value=self.cfg), \
                patch.object(bridge, "coordinator_for", return_value=coordinator), \
                patch.object(self.engine.conversation_scheduler, "choose", side_effect=AssertionError("busy cannot choose")), \
                patch.object(bridge, "flush_trigger_queue_if_waiting"), \
                patch.object(bridge.asyncio, "wait_for", side_effect=stop):
            with self.assertRaises(StopLoop):
                await bridge.adaptive_conversation_loop()
        self.assertEqual(self.engine.conversation_scheduler.group("group:100").calls, 0)
        self.assertEqual(len(self.engine.conversation_scheduler.group("group:100").pending), 1)

    async def test_cold_topic_does_not_overtake_message_arriving_during_permission_check(self):
        cold=Mock();cold.due.return_value='group:100'
        async def allowed(_session):
            self.engine.conversation_scheduler.observe('group:200','请解释一下这个问题','22','new',1000,mentioned=True)
            return True
        with patch.object(bridge,'engine',self.engine),patch.object(bridge,'load_config',return_value=self.cfg), \
                patch.object(bridge,'ACTIVE_WS_CLIENTS',{'official':1}),patch.object(bridge,'cold_topic_allowed',side_effect=allowed), \
                patch.object(bridge.time,'time',return_value=1000),patch.object(bridge,'enqueue_wake_beacon') as offer:
            await bridge.maybe_schedule_cold_topic(self.cfg,cold)
        offer.assert_not_called()

    def test_dispatch_rechecks_value_and_charges_only_an_accepted_turn(self):
        self.engine.conversation_scheduler.observe("group:300", "这个数学问题如何求解？", "22", 1, 1000)
        self.engine.unread_counts["group:300"] = 1
        self.engine.last_replied_at["group:300"] = 1190  # Legacy two-hour gate must not run.
        self.engine.context_windows["group:300"] = [{"session_key": "group:300", "user_id": "22", "text": "数学求助"}]
        self.engine.trigger_queue.append({"session_key": "group:300", "mode": "上下文积累判断",
            "score": 1, "reasons": [], "group_theme_info": None})
        coor = Mock()
        coor.snapshot.return_value = {"status": "idle"}
        coor.publish.side_effect = [{"accepted": False}, {"accepted": True, "trigger_id": "offline"}]
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            with patch.object(bridge, "engine", self.engine), patch.object(bridge, "load_config", return_value=self.cfg), \
                    patch.object(bridge, "coordinator_for", return_value=coor), \
                    patch.object(bridge, "PENDING_TRIGGER", Path(folder)/"pending.json"), \
                    patch.object(bridge.time, "time", return_value=1200), \
                    patch.object(bridge, "reserve_exploration", side_effect=AssertionError("no legacy quota")), \
                    patch.object(bridge, "study_reply_allowed", side_effect=AssertionError("no legacy gate")):
                bridge.flush_trigger_queue_if_waiting()
                self.assertEqual(self.engine.conversation_scheduler.group("group:300").calls, 0)
                self.assertEqual(len(self.engine.trigger_queue), 1)
                bridge.flush_trigger_queue_if_waiting()
        self.assertEqual(self.engine.conversation_scheduler.group("group:300").calls, 1)
        self.assertFalse(self.engine.conversation_scheduler.group("group:300").pending)
        self.assertFalse(self.engine.trigger_queue)

    def test_mention_avoids_promotional_keyword_filter_when_user_is_allowed(self):
        self.cfg["topic_bias"] = {"ignored_keywords": ["转发"]}
        event = {"message_type": "group", "group_id": 100, "self_id": 99, "user_id": 22,
                 "message_id": 1, "raw_message": "[CQ:at,qq=99] 我转发的这个说法靠谱吗？"}
        instant, _, text = self.engine.record_and_score(event, self.cfg)
        self.assertTrue(instant)
        self.assertIn("说法", text)

    def test_queued_mention_survives_later_fast_group_messages(self):
        from collections import deque
        from hakimi.prompt.prompt_context import build_context
        question = {"session_key": "group:100", "message_id": 71, "user_id": "22",
                    "text": "请解释这道题的证明为什么成立？", "received_at": 1000, "time": "12:00:00"}
        self.engine.context_windows["group:100"] = deque([question], maxlen=20)
        with patch.object(bridge, "engine", self.engine), patch.object(bridge, "flush_trigger_queue_if_waiting"):
            bridge.enqueue_wake_beacon("group:100", "即时唤醒(被@/私聊)", 5, [], self.cfg)
        for i in range(40):
            self.engine.context_windows["group:100"].append({"session_key": "group:100", "message_id": 100+i,
                "user_id": "33", "text": "另一个话题"*300, "time": "12:01:00"})
        self.assertFalse(any(row["message_id"] == 71 for row in self.engine.context_windows["group:100"]))
        coor = Mock()
        coor.snapshot.return_value = {"status": "idle"}
        coor.publish.return_value = {"accepted": True, "trigger_id": "offline"}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            with patch.object(bridge, "engine", self.engine), patch.object(bridge, "load_config", return_value=self.cfg), \
                    patch.object(bridge, "coordinator_for", return_value=coor), \
                    patch.object(bridge, "PENDING_TRIGGER", Path(folder)/"pending.json"), \
                    patch.object(bridge.time, "time", return_value=1100):
                bridge.flush_trigger_queue_if_waiting()
        payload = coor.publish.call_args.args[0]
        self.assertEqual(payload["pending_explicit_question_ids"], ["71"])
        context = build_context(payload, now=1100)
        messages = context["recent_context_messages_untrusted"]
        self.assertTrue(any(row["message_id"] == "71" for row in messages))
        self.assertLessEqual(sum(len(row["text"]) for row in messages), 6000)
        self.assertLessEqual(len(messages), 15)

    def test_a_related_elliptical_followup_actually_publishes_a_continuation_turn(self):
        scheduler = self.engine.conversation_scheduler
        scheduler.observe("group:100", "海祇岛的圣土化有什么文本依据？", "22", 1, 1000)
        scheduler.offered_turn("group:100", 1050, "initial")
        scheduler.consume("group:100")
        scheduler.sent_reply("group:100", "海祇岛圣土化可以查世界任务文本", 1070, 123, "initial")
        scheduler.observe("group:100", "那为什么不能这样？", "22", 2, 1080)
        self.engine.unread_counts["group:100"] = 1
        self.engine.context_windows["group:100"] = [{"session_key": "group:100", "user_id": "22", "text": "那为什么不能这样？"}]
        self.engine.trigger_queue.append({"session_key": "group:100", "mode": "相关话题续聊", "score": 1,
                                          "reasons": [], "group_theme_info": None})
        coordinator = Mock()
        coordinator.snapshot.return_value = {"status": "idle"}
        coordinator.publish.return_value = {"accepted": True, "trigger_id": "followup"}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            with patch.object(bridge, "engine", self.engine), patch.object(bridge, "load_config", return_value=self.cfg), \
                    patch.object(bridge, "coordinator_for", return_value=coordinator), \
                    patch.object(bridge, "PENDING_TRIGGER", Path(folder)/"pending.json"), \
                    patch.object(bridge.time, "time", return_value=1140):
                bridge.flush_trigger_queue_if_waiting()
        self.assertEqual(coordinator.publish.call_args.args[0]["wake_mode"], "相关话题续聊")
        self.assertEqual(scheduler.group("group:100").last_offer, 1140)
        self.assertFalse(scheduler.group("group:100").pending)

    def test_restart_restores_an_unhandled_mention_as_priority_input(self):
        row = {"session_key": "group:100", "message_id": "71", "user_id": "22", "nickname": "offline",
               "received_at": 1000, "time": "12:00:00", "text": "@哈基米 能再解释一下吗", "image_paths": []}
        from collections import OrderedDict
        with patch("study_recovery.recover_study_messages", return_value={"messages": [row], "last_replied_at": {}}), \
                patch.object(bridge, "RECENT_EVENT_IDS", OrderedDict()):
            self.engine.restore_recent_study_unread(self.cfg, now=1005)
        decision = self.engine.conversation_scheduler.decide("group:100", self.cfg, 1005)
        self.assertEqual(decision.mode, "mention")
        self.assertTrue(decision.ready)


if __name__ == "__main__":
    unittest.main()


class DialogueContinuationTests(unittest.TestCase):
    def test_dialogue_continues_then_stops(self):
        s, cfg, session, now = ConversationScheduler(), config(), "group:100", 1000.0
        s.offered_turn(session, now, "t0", messages=[{"user_id": "u1", "text": "猫和狗哪个更聪明"}])
        s.sent_reply(session, "猫聪明，狗只是听话", now+8, "m0", "t0")
        now += 8
        ready_count = 0
        for index, text in enumerate(["那猫为什么还要被铲屎官使唤", "所以猫其实是在养人对吧", "那你自己呢你算猫还是机器人", "也就是说你也在养我们"]):
            now += 12
            s.observe(session, text, "u1", f"x{index}", now)
            decision = next((d for d in (s.decide(session, cfg, now+dt) for dt in range(0, 90, 5)) if d.ready), None)
            if decision is None:
                break
            ready_count += 1
            wake = now+5
            s.offered_turn(session, wake, f"t{index+1}", messages=[{"user_id": "u1", "text": text}])
            s.consume(session)
            s.sent_reply(session, "好", wake+8, f"r{index}", f"t{index+1}")
            now = wake+8
        self.assertEqual(ready_count, 3)  # follows up, but never becomes a flood


class UnifiedWindowTests(unittest.TestCase):
    def test_any_group_wake_waits_for_final_delivery_then_fixed_45_seconds(self):
        for mode in ("", "相关话题续聊", "冷启动", "即时唤醒(被@/私聊)"):
            with self.subTest(mode=mode):
                s = ConversationScheduler()
                s.offered_turn("group:100", 1000, "initial", wake_mode=mode)
                s.sent_reply("group:100", "海祇岛任务文本", 1001, "123", "initial")
                self.assertEqual(s.decide("group:100", config(), 1100).basis, "awaiting_reply_delivery")
                s.observe_reply("group:100", 1100)
                s.delivery_finished("group:100", 1102)
                self.assertEqual(s.group("group:100").listen_due, 1147)
                self.assertEqual(s.decide("group:100", config(), 1146).wait, 1)

    def test_filtered_window_scores_only_selected_messages(self):
        s, session = ConversationScheduler(), "group:100"
        s.sent_reply(session, "海祇岛任务文本", 1000, "123")
        s.delivery_finished(session, 1000)
        relevant = {"received_at": 1010, "user_id": "22", "message_id": "1",
                    "text": "海祇岛任务文本具体是哪篇？", "reply_to": "123"}
        ack = dict(relevant, message_id="2", text="哈哈", reply_to="")
        s.observe(session, relevant["text"], "22", "1", 1010, reply_to="123")
        self.assertTrue(s.score_window_messages(session, config(), 1045, [relevant]).ready)
        self.assertFalse(s.score_window_messages(session, config(), 1045, [ack]).ready)

    def test_cooldown_survives_restart(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            s = ConversationScheduler()
            s.restore(folder, 1000)
            for at in (1001, 1010, 1020, 1030):
                s.sent_reply("group:100", "回复", at)
            restored = ConversationScheduler()
            restored.restore(folder, 1040)
            self.assertTrue(restored.healthy)
            self.assertEqual(restored.group("group:100").cooldown_until, 1270)


class EarlyWindowTests(unittest.TestCase):
    def prepare(self):
        s=ConversationScheduler();session='group:100'
        s.sent_reply(session,'海祇岛任务文本',1000,'123')
        s.delivery_finished(session,1000)
        return s,session

    def test_quiet_window_refines_before_scoring_and_freezes_input(self):
        s,key=self.prepare()
        s.observe(key,'这个消息即使原始评分很低也先梳理','22','1',1002)
        self.assertFalse(s.decide(key,config(),1005).ready)
        with patch.object(s,'score_window_messages',side_effect=AssertionError('premature scoring')):
            self.assertTrue(s.decide(key,config(),1006).ready)
        self.assertEqual(s.group(key).listen_cutoff,1006)
        s.observe(key,'后续补充不能触发第二次梳理','22','2',1007)
        self.assertTrue(s.decide(key,config(),1008).ready)
        self.assertEqual(s.group(key).listen_cutoff,1006)
        self.assertFalse(s.score_window_messages(key,config(),1008,[
            {'received_at':1007,'user_id':'22','message_id':'2','text':'为什么？','reply_to':'123'}]).ready)

    def test_busy_window_checks_every_15_seconds_only_with_new_input(self):
        s,key=self.prepare()
        for at in range(1001,1015):
            s.observe(key,'讨论中的补充内容','22',str(at),at)
            self.assertFalse(s.decide(key,config(),at).ready)
        self.assertTrue(s.decide(key,config(),1015).ready)
        s.group(key).listen_next_check=1030
        s.reject_window_batch(key,1015,1018)
        self.assertFalse(s.decide(key,config(),1030).ready)  # no new input, no repeat
        s.observe(key,'新一条追问','22','new',1031)
        self.assertTrue(s.decide(key,config(),1031).ready)
        s.group(key).listen_next_check=1046
        s.reject_window_batch(key,1031,1034)
        s.observe(key,'晚来的消息','22','late',1040)
        self.assertFalse(s.decide(key,config(),1040).ready)
        self.assertFalse(s.decide(key,config(),1045).ready)
        self.assertEqual(s.group(key).listen_round,0)
        other,key=self.prepare()
        other.observe(key,'谢谢','22','ack',1002)
        self.assertFalse(other.decide(key,config(),1045).ready)
        self.assertEqual(other.group(key).listen_round,0)

    def test_selected_followup_survives_intervening_messages(self):
        s,key=self.prepare()
        chosen=[]
        for i,text in enumerate(['引用上一条追问','补充条件甲','补充条件乙','补充条件丙']):
            row={'received_at':1001+i,'user_id':'22','message_id':str(i),'text':text,
                 'reply_to':'123' if i==0 else ''}
            chosen.append(row)
            s.observe(key,text,'22',str(i),1001+i,reply_to=row['reply_to'])
        s.decide(key,config(),1007)
        refined=s.score_window_messages(key,config(),1007,chosen)
        ordinary=s._score_rows(key,config(),1007,list(s.group(key).pending))
        self.assertGreater(refined.relevance,.4)
        self.assertLess(ordinary.relevance,.1)
        self.assertTrue(refined.ready)

    def test_restart_does_not_retry_started_refinement(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            s=ConversationScheduler();s.restore(folder,999)
            s.sent_reply('group:100','回复',1000,'123');s.delivery_finished('group:100',1000)
            s.observe('group:100','追问细节','22','1',1001)
            s.decide('group:100',config(),1006)
            s.group('group:100').listen_review_started=True;s.save()
            recovered=ConversationScheduler();recovered.restore(folder,1007)
            self.assertTrue(recovered.healthy)
            self.assertEqual(recovered.group('group:100').listen_round,0)
