import importlib.util
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace, ModuleType
from unittest.mock import patch
import sys


def load_bridge():
    path = importlib.util.find_spec("hakimi.bridge.bridge_daemon").origin
    spec = importlib.util.spec_from_file_location("test_live_bridge", path)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    return bridge


class BridgeFollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.bridge = load_bridge()
        self.bridge.BASE_DIR = Path(self.temp.name)
        self.bridge.INBOX_LOG = self.bridge.BASE_DIR / "inbox"
        self.bridge.PENDING_TRIGGER = self.bridge.BASE_DIR / "pending"
        self.bridge.WAITING_FLAG = self.bridge.BASE_DIR / "waiting"
        self.bridge.load_config = lambda: self.cfg
        self.cfg = {"bot_profile": {"persona": "test"}, "user_bias": {}, "fallback": {"enabled": True}}
        self.bridge.session_is_allowed = lambda *args: True
        self.bridge.adaptive_enabled = lambda cfg: False
        self.bridge._prewarm_mention_context = lambda *args: None
        self.bridge.cold_settings = lambda cfg: {}
        self.bridge.record_interaction = lambda *args, **kwargs: None
        self.coordinator = SimpleNamespace(available_lane=lambda probe: None,
                                           publish=self.publish, for_lane=lambda lane: self,
                                           lane_ids=lambda: ("",))
        self.bridge.coordinator_for = lambda *args: self.coordinator
        self.state = {}
        self.published = []
        self.result = {"accepted": True, "trigger_id": "accepted", "conversation_lane": ""}
        self.now = time.time() - 1
        self.real_modules = {name: importlib.import_module(name) for name in (
            "hakimi.bridge.message_lifecycle", "hakimi.bridge.image_initiative",
            "hakimi.bridge.reply_format", "hakimi.bridge.conversation_judge",
            "hakimi.memory.memory_store", "hakimi.bridge.interaction_policy",
            "hakimi.bridge.bot_registry", "hakimi.core.owner",
        )}
        self.module("hakimi.bridge.message_lifecycle", entry=lambda *args, **kwargs: None,
                    record=lambda *args, **kwargs: None)
        self.module("hakimi.bridge.image_initiative", image_candidates=lambda *args: [], decide=None)
        self.module("hakimi.bridge.reply_format", group_reply_rules=lambda *args: {})
        self.module("hakimi.bridge.conversation_judge", mention_context=lambda entry, rows, *args, **kwargs: rows)
        self.module("hakimi.memory.memory_store", store=lambda *args: SimpleNamespace(
            topic_signature=lambda *args: [], observe=lambda *args: None))
        self.module("hakimi.bridge.interaction_policy", ignored=lambda *args, **kwargs: False,
                    ignored_text=lambda *args: False, known_ai=lambda *args: False)
        self.module("hakimi.bridge.bot_registry", cooling=lambda *args: False)
        self.module("hakimi.core.owner", local_terms=lambda *args: ())

    def module(self, name, **fields):
        fake = ModuleType(name)
        fake.__dict__.update(fields)
        self.enterContext(patch.dict(sys.modules, {name: fake}))

    def snapshot(self):
        return self.state

    def publish(self, payload, *, replace_trigger_id=None):
        self.published.append((payload, replace_trigger_id))
        return self.result

    def row(self, mid, at=None, uid="1", session="group:7"):
        return {"session_key": session, "user_id": uid, "message_id": str(mid),
                "text": f"完整原文 {mid}", "received_at": self.now if at is None else at}

    def enqueue(self, row, prior=None):
        self.bridge.enqueue_directed(row["session_key"], row, 5, [], self.cfg, prior)

    def test_local_followup_collects_separate_directed_questions(self):
        old = self.row(1)
        self.enqueue(old)
        candidate = self.bridge.unanswered_directed("group:7", "1", self.cfg)
        self.assertIsNone(self.bridge.unanswered_directed("group:7", "9", self.cfg))
        self.assertIsNone(self.bridge.unanswered_directed("group:9", "1", self.cfg))
        self.enqueue(self.row(2, self.now + .01), candidate)
        independent = self.row(3, self.now + .02)
        self.enqueue(independent)
        ambient = self.row(4, self.now + .03, uid="other")
        self.bridge.engine.context_windows["group:7"] = [old, independent, ambient]
        entries = self.bridge.engine.trigger_queue
        self.assertEqual([[r["message_id"] for r in e["direct_messages"]] for e in entries],
                         [["1", "2"], ["3"]])
        self.coordinator.available_lane = lambda probe: ""
        self.bridge.flush_trigger_queue_if_waiting()
        first, replaces = self.published[0]
        self.assertIsNone(replaces)
        self.assertEqual(first["pending_explicit_question_ids"], ["1", "2"])
        self.assertEqual(first["reply_target_message_id"], "2")
        self.assertTrue(first["merged_followup"])
        self.assertEqual([r["message_id"] for r in first["recent_context_messages"]], ["1", "2", "4"])
        self.assertEqual(entries[0]["direct_messages"][0]["message_id"], "3")

    def test_durable_match_can_replace_while_lanes_busy_and_no_wait_flag(self):
        self.cfg["fallback"]["enabled"] = False
        self.state = {"trigger_id": "old", "status": "primary_active", "payload": {
            "session_key": "group:7", "wake_mode": "即时唤醒(被@/私聊)",
            "pending_explicit_question_ids": ["1", "2", "3"], "reply_target_message_id": "3",
            "recent_context_messages": [self.row(mid) for mid in (1, 2, 3)],
        }}
        self.bridge.restore_directed_turns(self.coordinator)
        prior = self.bridge.unanswered_directed("group:7", "1", self.cfg)
        self.enqueue(self.row(4, self.now + .02), prior)
        payload, replaces = self.published[0]
        self.assertEqual(replaces, "old")
        self.assertEqual(payload["pending_explicit_question_ids"], ["1", "2", "3", "4"])
        self.assertEqual(payload["reply_target_message_id"], "4")
        self.assertEqual([row["text"] for row in payload["recent_context_messages"]],
                         [f"完整原文 {mid}" for mid in (1, 2, 3, 4)])

    def test_committed_while_processing_preserves_new_input_for_normal_admission(self):
        old = self.row(1)
        self.coordinator.available_lane = lambda probe: ""
        self.result = {"accepted": True, "trigger_id": "old", "conversation_lane": ""}
        self.enqueue(old)
        self.state = {"trigger_id": "old", "status": "primary_active"}
        prior = self.bridge.unanswered_directed("group:7", "1", self.cfg)
        self.result = {"accepted": False, "reason": "replacement_unavailable"}
        self.enqueue(self.row(2, self.now + .02), prior)
        entry, = self.bridge.engine.trigger_queue
        self.assertNotIn("replace_trigger_id", entry)
        self.assertEqual([r["message_id"] for r in entry["direct_messages"]], ["2"])
        self.result = {"accepted": True, "trigger_id": "new", "conversation_lane": ""}
        self.bridge.flush_trigger_queue_if_waiting()
        payload, replaces = self.published[-1]
        self.assertIsNone(replaces)
        self.assertEqual(payload["pending_explicit_question_ids"], ["2"])

    def test_ingress_checks_native_only_for_same_author_in_same_session(self):
        observed = []
        def native(old, new):
            observed.append((old, new))
            return new.endswith("那里也一样吗？")
        self.module("hakimi.bridge.followup_native", is_continuation=native)
        def record(event, cfg, cleaned, images, memory, addressed):
            row = self.row(event["message_id"], session=f"group:{event['group_id']}", uid=event["user_id"])
            row["text"] = cleaned
            self.bridge.engine.msg_lookup[str(event["message_id"])] = row
            return addressed, [], cleaned
        self.bridge.engine.record_and_score = record
        self.bridge.engine.arm_study_attention = lambda *args, **kwargs: None
        self.bridge.direct_attention = lambda event, cfg: event["raw_message"].startswith(("@", "[CQ:at,qq=100]"))
        self.bridge.format_clean_message = lambda raw, *args: raw
        async def immediate_thread(fn, *args):
            return fn(*args)
        self.bridge.asyncio = SimpleNamespace(to_thread=immediate_thread)
        def send(mid, text, uid="1", gid="7"):
            event = {"post_type": "message", "message_type": "group", "group_id": gid,
                     "self_id": "100", "user_id": uid, "message_id": mid, "raw_message": text}
            with self.assertRaises(StopIteration):
                self.bridge.process_incoming_event(event).send(None)
        send(1, "普通聊天")
        send(2, "@哈基米 日本如何？")
        send(3, "那里也一样吗？", uid="2")
        send(4, "那里也一样吗？", gid="8")
        send(5, "[CQ:at,qq=2] 那里也一样吗？")
        send(6, "那里也一样吗？")
        send(7, "[CQ:at,qq=100] 那里也一样吗？")
        send(8, "另外问，韩国的首都是什么？")
        send(9, "[CQ:reply,id=2][CQ:at,qq=2] 那里也一样吗？")
        send(10, "[CQ:reply,id=2] 那里也一样吗？", uid="2")
        send(11, "[CQ:reply,id=2] 那里也一样吗？")
        self.assertEqual(observed, [("日本如何？", "那里也一样吗？"),
                                   ("日本如何？\n那里也一样吗？", "那里也一样吗？"),
                                   ("日本如何？\n那里也一样吗？\n那里也一样吗？", "另外问，韩国的首都是什么？"),
                                   ("日本如何？\n那里也一样吗？\n那里也一样吗？", "那里也一样吗？")])
        self.assertEqual([[r["message_id"] for r in e["direct_messages"]]
                          for e in self.bridge.engine.trigger_queue], [["2", "6", "7", "11"], ["8"]])

    @unittest.skipUnless(importlib.util.find_spec("hakimi.bridge._followup_rules"), "optional C++ module")
    def test_real_native_ingress_supersedes_an_owned_turn_and_preserves_independent_turns(self):
        from hakimi.bridge.followup_native import is_continuation
        from hakimi.bridge.turn_coordinator import TurnCoordinator

        self.enterContext(patch.dict(sys.modules, self.real_modules))
        self.assertEqual(is_continuation.__module__, "hakimi.bridge._followup_rules")
        coordinator = TurnCoordinator(self.bridge.BASE_DIR)
        self.bridge.coordinator_for = lambda *args: coordinator
        self.cfg["topic_bias"] = {}

        async def immediate_thread(fn, *args):
            return fn(*args)

        self.bridge.asyncio = SimpleNamespace(to_thread=immediate_thread)

        def send(mid, text):
            event = {"post_type": "message", "message_type": "private", "self_id": "100",
                     "user_id": "1", "message_id": mid, "raw_message": text}
            with self.assertRaises(StopIteration) as result:
                self.bridge.process_incoming_event(event).send(None)
            self.assertEqual(result.exception.value, {"handled": True})

        def claim():
            state = coordinator.snapshot()
            payload = state["payload"]
            owner = coordinator.claim_primary(state["trigger_id"])
            self.assertIsNotNone(owner)
            return payload, owner["token"]

        send(101, "帮我写个活动邀请")
        original, old_token = claim()
        self.assertEqual(original["pending_explicit_question_ids"], ["101"])
        send(102, "用英文，语气轻松点")
        self.assertEqual(coordinator.submit_reply(old_token, ["stale"], "gemini")["reason"], "stale_owner")
        merged, new_token = claim()
        self.assertEqual(merged["pending_explicit_question_ids"], ["101", "102"])
        self.assertEqual(merged["reply_target_message_id"], "102")
        self.assertEqual([row["text"] for row in merged["recent_context_messages"]],
                         ["帮我写个活动邀请", "用英文，语气轻松点"])
        self.assertTrue(coordinator.submit_reply(new_token, ["combined"], "gemini")["accepted"])

        send(103, "请问东京是日本的首都吗？")
        first, first_token = claim()
        send(104, "另外问，三加四等于几？")
        self.assertEqual(coordinator.snapshot()["trigger_id"], first["trigger_id"])
        self.assertEqual(first["pending_explicit_question_ids"], ["103"])
        self.assertTrue(coordinator.submit_reply(first_token, ["first"], "gemini")["accepted"])
        self.bridge.flush_trigger_queue_if_waiting()
        second, second_token = claim()
        self.assertEqual(second["pending_explicit_question_ids"], ["104"])
        self.assertTrue(coordinator.submit_reply(second_token, ["second"], "gemini")["accepted"])

        records = [json.loads(line) for line in coordinator.outbox_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["trigger_id"] for row in records],
                         [merged["trigger_id"], first["trigger_id"], second["trigger_id"]])


if __name__ == "__main__":
    unittest.main()
