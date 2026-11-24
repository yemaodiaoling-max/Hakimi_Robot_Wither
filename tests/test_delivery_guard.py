"""Offline delivery regression tests; all checkpoint files live in temp directories."""
import asyncio
import json
import selectors
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hakimi.bridge.delivery_guard import DeliveryGuard


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="delivery_guard_", dir=Path(__file__).resolve().parent)
        self.assertTrue(Path(self.tmp.name).resolve().is_relative_to(Path(__file__).resolve().parent))
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.now = 1000.0
        self.outbox = self.path / "outbox.jsonl"
        self.checkpoint = self.path / "delivery_state.json"
        self.guard = self.make_guard()
        self.counter = 0

    def make_guard(self):
        return DeliveryGuard(self.checkpoint, self.outbox, clock=lambda: self.now)

    def enqueue(self, text="test reply", session="group:123", **extra):
        self.counter += 1
        record = {"reply_id": f"r{self.counter}", "session_key": session,
                  "segments": [text], "created_at": self.now, **extra}
        with self.outbox.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")

    def send_success(self):
        self.assertIsNotNone(self.guard.next_message())
        self.guard.begin_attempt()
        self.guard.finish_attempt("success")

    def test_baseline_does_not_replay_legacy_records(self):
        self.enqueue()
        self.checkpoint.unlink()
        guard = self.make_guard()
        self.assertIsNone(guard.next_message())
        self.assertEqual(guard.state["last_event"]["status"], "legacy_baseline")

    def test_auto_quote_target_survives_delivery_checkpoint_restart(self):
        self.enqueue('reply',reply_target_id='71')
        self.assertIsNotNone(self.guard.next_message())
        restored=self.make_guard()
        self.assertEqual(restored.pending['reply_target_id'],'71')

    def test_offline_pending_survives_restart_without_becoming_success(self):
        self.enqueue()
        self.assertEqual(self.guard.next_message(), ("group:123", "test reply"))
        self.guard.note_offline()
        guard = self.make_guard()
        self.assertEqual(guard.next_message(), ("group:123", "test reply"))
        self.assertFalse(guard.state["attempts"])
        self.assertEqual(guard.state["last_event"]["status"], "offline_queued")

    def test_crash_during_send_does_not_replay_and_pauses(self):
        self.enqueue(segments=["one", "two"])
        self.guard.next_message()
        self.guard.begin_attempt()
        guard = self.make_guard()
        self.assertIsNone(guard.next_message())
        self.assertEqual(guard.state["last_event"]["status"], "unknown_after_restart")
        self.assertGreater(guard.state["paused_until"], self.now)

    def test_acknowledged_first_segment_resumes_only_second(self):
        self.enqueue(segments=["one", "two"])
        self.send_success()
        guard = self.make_guard()
        self.assertEqual(guard.next_message(), ("group:123", "two"))

    def test_same_turn_bubbles_skip_session_gap_but_next_turn_waits(self):
        self.enqueue(segments=['first bubble', 'second bubble'])
        self.send_success()
        self.send_success()
        self.enqueue('next turn')
        self.now += 3
        self.assertIsNone(self.guard.next_message())
        self.now += 12
        self.assertEqual(self.guard.next_message(), ('group:123', 'next turn'))

    def test_global_round_cap_does_not_split_one_reply(self):
        self.guard.configure({'max_messages_per_minute': 1})
        self.enqueue(segments=['one', 'two', 'three'])
        self.send_success()
        self.guard = self.make_guard()
        self.guard.configure({'max_messages_per_minute': 1})
        self.send_success()
        self.send_success()
        self.enqueue('another round', session='group:456')
        self.now += 4
        self.assertIsNone(self.guard.next_message())
        self.now += 56
        self.assertEqual(self.guard.next_message(), ('group:456', 'another round'))

    def test_timeout_discards_rest_and_does_not_retry(self):
        self.enqueue(segments=["one", "two"])
        self.guard.next_message()
        self.guard.begin_attempt()
        self.guard.finish_attempt("unknown")
        self.assertFalse(self.guard.pending)
        self.assertIsNone(self.guard.next_message())
        self.assertEqual(self.guard.state["last_event"]["status"], "unknown")

    def test_platform_rejection_stops_new_sends_until_pause_expires(self):
        self.enqueue()
        self.guard.next_message()
        self.guard.begin_attempt()
        self.guard.finish_attempt("rejected")
        self.enqueue("another", session="group:456")
        self.assertIsNone(self.guard.next_message())
        self.assertTrue(self.guard.pending)
        self.assertGreater(self.guard.state["paused_until"], self.now)

    def test_duplicate_with_different_reply_reference_is_suppressed(self):
        self.enqueue("[CQ:reply,id=1] same answer")
        self.send_success()
        self.now += 16
        self.enqueue("[CQ:reply,id=2]same  answer")
        self.assertIsNone(self.guard.next_message())
        self.assertEqual(self.guard.state["last_event"]["status"], "duplicate_content")

    def test_global_and_per_session_limits(self):
        self.enqueue("one")
        self.send_success()
        self.enqueue("two", session="group:456")
        self.assertIsNone(self.guard.next_message())
        self.now += 4
        self.send_success()
        self.enqueue("three")
        self.now += 4
        self.assertIsNone(self.guard.next_message())
        self.now += 8
        self.assertEqual(self.guard.next_message(), ("group:123", "three"))

    def test_global_minute_cap_spans_sessions(self):
        self.guard.configure({"max_messages_per_minute": 2})
        for index in range(2):
            self.enqueue(str(index), session=f"group:{index + 1}")
            self.send_success()
            self.now += 4
        self.enqueue("capped", session="group:3")
        self.assertIsNone(self.guard.next_message())
        self.now += 60
        self.assertIsNotNone(self.guard.next_message())

    def test_partial_line_waits_and_bad_json_is_isolated(self):
        self.outbox.write_bytes(b'{"session_key":')
        self.assertIsNone(self.guard.next_message())
        self.assertEqual(self.guard.state["cursor"], 0)
        with self.outbox.open("ab") as stream:
            stream.write(b" broken}\n")
        self.assertIsNone(self.guard.next_message())
        self.assertEqual(self.guard.state["last_event"]["status"], "malformed_record")
        self.enqueue("good")
        self.assertEqual(self.guard.next_message(), ("group:123", "good"))

    def test_expired_and_oversize_records_are_not_sent(self):
        self.enqueue(created_at=self.now - 601)
        self.assertIsNone(self.guard.next_message())
        self.assertEqual(self.guard.state["last_event"]["status"], "expired")
        self.enqueue("x" * 4001)
        self.assertIsNone(self.guard.next_message())
        self.assertEqual(self.guard.state["last_event"]["status"], "invalid_payload")

    def test_completed_reply_id_survives_restart(self):
        self.enqueue(reply_id="same")
        self.send_success()
        self.now += 1000
        self.enqueue("new text", reply_id="same")
        guard = self.make_guard()
        self.assertIsNone(guard.next_message())
        self.assertEqual(guard.state["last_event"]["status"], "duplicate_reply_id")

    def test_corrupt_checkpoint_fails_closed(self):
        self.checkpoint.write_text("{broken", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.make_guard()

    def test_truncated_outbox_is_not_replayed(self):
        self.enqueue()
        self.send_success()
        self.outbox.write_text("", encoding="utf-8")
        self.assertIsNone(self.guard.next_message())
        self.assertEqual(self.guard.state["last_event"]["status"], "outbox_replaced_review_required")

    def test_non_finite_policy_and_timestamp_are_not_trusted(self):
        self.guard.configure({"ack_timeout_seconds": float("nan"), "max_messages_per_minute": float("inf")})
        self.assertEqual(self.guard.policy["ack_timeout_seconds"], 12)
        self.assertEqual(self.guard.policy["max_messages_per_minute"], 8)
        self.enqueue(created_at=float("nan"))
        self.assertIsNotNone(self.guard.next_message())
        self.now += 601
        self.assertIsNone(self.guard.next_message())
        self.assertEqual(self.guard.state["last_event"]["status"], "expired")


class OfflineSelector(selectors.BaseSelector):
    """Timer-only test selector: Windows' normal loop opens a socketpair."""
    def register(self, *args, **kwargs):
        raise AssertionError("real I/O is forbidden in offline delivery tests")

    def unregister(self, *args, **kwargs):
        raise AssertionError("no real descriptors were registered")

    def select(self, timeout=None):
        if timeout:
            time.sleep(min(timeout, 0.05))
        return []

    def get_map(self):
        return {}


class OfflineEventLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        super().__init__(selector=OfflineSelector())

    def _make_self_pipe(self):
        pass

    def _close_self_pipe(self):
        pass

    def _write_to_self(self):
        pass


class OneBotAckTests(unittest.IsolatedAsyncioTestCase):
    loop_factory = OfflineEventLoop
    async def asyncSetUp(self):
        from hakimi.bridge import bridge_daemon
        self.bridge = bridge_daemon
        self.bridge.ACTIVE_WS_CLIENTS.clear()
        self.bridge.PENDING_ACKS.clear()

    async def asyncTearDown(self):
        self.bridge.ACTIVE_WS_CLIENTS.clear()
        self.bridge.PENDING_ACKS.clear()

    def socket(self, status="ok", acknowledge=True):
        bridge = self.bridge

        class FakeSocket:
            def __init__(self):
                self.sent = []

            async def send(self, raw):
                payload = json.loads(raw)
                self.sent.append(payload)
                if acknowledge:
                    bridge.resolve_onebot_ack({"echo": payload["echo"], "status": status,
                        "retcode": 0 if status == "ok" else 100,
                        "data": {"message_id": 42}}, self)

        return FakeSocket()

    async def test_single_connection_and_ack_success(self):
        old, new = self.socket(), self.socket()
        self.bridge.ACTIVE_WS_CLIENTS.update({old: 1, new: 2})
        result = await self.bridge.send_via_onebot("group:123", "hello")
        self.assertEqual(result, {"status": "success", "message_id": 42})
        self.assertEqual(len(old.sent), 0)
        self.assertEqual(len(new.sent), 1)
        self.assertFalse(self.bridge.PENDING_ACKS)

    async def test_no_ack_is_unknown_and_not_retried(self):
        socket = self.socket(acknowledge=False)
        self.bridge.ACTIVE_WS_CLIENTS[socket] = 1
        result = await self.bridge.send_via_onebot("group:123", "hello", timeout=0.01)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(len(socket.sent), 1)

    async def test_failed_ack_is_rejected(self):
        socket = self.socket(status="failed")
        self.bridge.ACTIVE_WS_CLIENTS[socket] = 1
        result = await self.bridge.send_via_onebot("group:123", "hello")
        self.assertEqual(result["status"], "rejected")

    async def test_disconnected_does_not_report_success(self):
        result = await self.bridge.send_via_onebot("group:123", "hello")
        self.assertEqual(result["status"], "offline")

    async def test_ack_from_different_socket_is_ignored(self):
        owner, stranger = self.socket(), self.socket()
        future = asyncio.get_running_loop().create_future()
        self.bridge.PENDING_ACKS["one"] = (future, owner)
        resolved = self.bridge.resolve_onebot_ack({"echo": "one", "status": "ok", "retcode": 0}, stranger)
        self.assertFalse(resolved)
        self.assertFalse(future.done())
        future.cancel()

    async def test_incoming_ids_are_deduplicated_per_session(self):
        cfg = {"bot_profile": {"self_qq": 99}, "topic_bias": {}, "user_bias": {},
               "lurker_schedule": {"instant_wake_on_at_or_private": False}}
        event = {"post_type": "message", "message_type": "group", "group_id": 123,
                 "user_id": 22, "message_id": 1, "raw_message": "example"}
        async def inline_thread(function, *args):
            return function(*args)
        with tempfile.TemporaryDirectory(prefix="delivery_incoming_", dir=Path(__file__).parent) as folder:
            self.assertTrue(Path(folder).resolve().is_relative_to(Path(__file__).resolve().parent))
            history = Path(folder) / "inbox.jsonl"
            with patch.object(self.bridge, "load_config", return_value=cfg), \
                    patch.object(self.bridge, "INBOX_LOG", history), \
                    patch.object(self.bridge, "engine", self.bridge.DynamicHakimiEngine()), \
                    patch.object(self.bridge, "RECENT_EVENT_IDS", self.bridge.OrderedDict()), \
                    patch.object(self.bridge.asyncio, "to_thread", inline_thread):
                await self.bridge.process_incoming_event(event)
                await self.bridge.process_incoming_event(event)
                await self.bridge.process_incoming_event({**event, "group_id": 456})
                self.assertEqual(len(history.read_text(encoding="utf-8").splitlines()), 2)


class MessageIsolationTests(unittest.TestCase):
    def setUp(self):
        from hakimi.bridge import bridge_daemon
        self.bridge = bridge_daemon

    def test_text_cannot_grant_image_access(self):
        paths = []
        self.bridge.format_clean_message("image_paths: [E:/private.jpg]", "99", 1, {}, paths, "group:123")
        self.assertEqual(paths, [])

    def test_only_cached_image_paths_are_collected(self):
        with tempfile.TemporaryDirectory(prefix="delivery_images_", dir=Path(__file__).parent) as folder:
            self.assertTrue(Path(folder).resolve().is_relative_to(Path(__file__).resolve().parent))
            jpg = Path(folder) / "cached.jpg"
            gif = jpg.with_suffix(".gif")
            jpg.write_bytes(b"test")
            gif.write_bytes(b"GIF89a")
            paths = []
            with patch.object(self.bridge, "cache_qq_image_to_jpg", return_value=jpg.as_posix()) as cache:
                self.bridge.format_clean_message("[CQ:image,file=x]", "99", 7, {}, paths, "group:123")
            self.assertEqual(paths, [jpg.as_posix(), gif.as_posix()])
            self.assertIn("group:123", cache.call_args.args[2])

    def test_cross_group_reference_content_and_paths_are_not_exposed(self):
        lookup = {"5": {"nickname": "person", "text": "private example", "session_key": "group:456",
                        "image_paths": ["E:/private.jpg"]}}
        paths = []
        remote = {"data": {"group_id": 456, "raw_message": "private example"}}
        with patch.object(self.bridge, "onebot_http_post", return_value=remote):
            text = self.bridge.format_clean_message("[CQ:reply,id=5]", "99", 1, lookup, paths, "group:123")
        self.assertNotIn("private example", text)
        self.assertEqual(paths, [])

    def test_same_group_reference_retains_trusted_paths(self):
        lookup = {"5": {"nickname": "person", "text": "example", "session_key": "group:123",
                        "image_paths": ["E:/cache/image.jpg"]}}
        paths = []
        text = self.bridge.format_clean_message("[CQ:reply,id=5]", "99", 1, lookup, paths, "group:123")
        self.assertIn("example", text)
        self.assertEqual(paths, ["E:/cache/image.jpg"])

    def test_unknown_groups_are_excluded_from_random_peek(self):
        engine = self.bridge.DynamicHakimiEngine()
        cfg = {"bot_profile": {"random_peek_groups": [123]}, "group_weights": {}, "lurker_schedule": {}}
        engine.unread_counts = {"group:456": 20}
        engine.window_interest_score = {"group:456": 99}
        self.assertEqual(engine.pick_session_for_random_peek(cfg), (None, 0.0, []))


if __name__ == "__main__":
    unittest.main()
