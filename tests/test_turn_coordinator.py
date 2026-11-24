"""Offline ownership and crash-recovery checks. Never invokes dsh or QQ."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hakimi.bridge.turn_coordinator import CoordinatorError, TurnCoordinator
from hakimi.bridge import turn_coordinator as coordinator_module


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.test_root = Path(__file__).resolve().parent
        self.temp = tempfile.TemporaryDirectory(prefix="turn_coord_", dir=self.test_root)
        self.base = Path(self.temp.name).resolve()
        self.assertTrue(self.base.is_relative_to(self.test_root))
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.coordinator = self.make_coordinator()

    def make_coordinator(self):
        return TurnCoordinator(self.base, clock=lambda: self.now)

    def publish(self, **extra):
        payload = {"trigger_id": "trigger-1", "session_key": "group:123", "created_at": self.now,
                   "recent_context_messages": [{"text": "synthetic test context"}], **extra}
        return self.coordinator.publish(payload)

    def primary(self):
        self.publish()
        return self.coordinator.claim_primary()

    def records(self):
        path = self.base / "state" / "outbox.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def events(self):
        path = self.base / "state" / "turn_events.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def test_reply_target_metadata_survives_commit_without_model_quote_call(self):
        self.publish(reply_target_message_id='1',recent_context_messages=[
            {'session_key':'group:123','message_id':'1','user_id':'22','text':'问题'},
            {'session_key':'group:123','message_id':'2','user_id':'33','text':'另一个问题'}])
        primary=self.coordinator.claim_primary()
        self.assertTrue(self.coordinator.submit_reply(primary['token'],['回答','补充'],'gemini')['accepted'])
        self.assertEqual(self.records()[0]['reply_target_id'],'1')
        self.assertEqual(self.records()[0]['segments'],['回答','补充'])

    def test_explicit_visible_target_overrides_default_but_other_group_does_not(self):
        for explicit,expected in [('2','2'),('3','1')]:
            self.publish(trigger_id='t'+explicit,reply_target_message_id='1',recent_context_messages=[
                {'session_key':'group:123','message_id':'1','text':'问题'},
                {'session_key':'group:123','message_id':'2','text':'问题2'},
                {'session_key':'group:456','message_id':'3','text':'其他群'}])
            primary=self.coordinator.claim_primary()
            self.assertTrue(self.coordinator.submit_reply(primary['token'],['回答'],'gemini',reply_to=explicit)['accepted'])
            self.assertEqual(self.records()[-1]['reply_target_id'],expected)

    def test_completion_audit_is_metadata_only_and_preserves_trigger_watermark(self):
        self.publish(created_at=950, wake_mode="低频尝试参与")
        self.now = 1005
        primary = self.coordinator.claim_primary()
        self.now = 1010
        self.assertTrue(self.coordinator.submit_reply(primary["token"], ["PRIVATE_REPLY_TEXT"], "gemini")["accepted"])
        event = self.events()[0]
        self.assertEqual(event, {"version": 1, "trigger_id": "trigger-1", "session_key": "group:123",
            "wake_mode": "低频尝试参与", "provider": "gemini", "outcome": "replied", "created_at": 950,
            "claimed_at": 1005, "completed_at": 1010, "takeover_reason": None})
        raw = self.coordinator.events_path.read_text(encoding="utf-8")
        for private in (primary["token"], "PRIVATE_REPLY_TEXT", "synthetic test context", "owner_token", "segments"):
            self.assertNotIn(private, raw)
        self.assertEqual(self.records()[0]["created_at"], 1010)  # Delivery time is not the recovery watermark.

    def test_custom_audit_values_are_whitelisted_not_copied_as_free_text(self):
        secret = "PRIVATE_SECRET_TOKEN with spaces"
        self.publish(trigger_id=secret, wake_mode={"text": secret})
        primary = self.coordinator.claim_primary()
        self.assertTrue(self.coordinator.report_failure(primary["token"], secret))
        fallback = self.coordinator.claim_fallback()
        self.assertTrue(self.coordinator.complete(fallback["token"], secret))
        event = self.events()[0]
        self.assertEqual(event["wake_mode"], "other")
        self.assertEqual(event["outcome"], "other")
        self.assertEqual(event["takeover_reason"], "reported:other")
        self.assertTrue(event["trigger_id"].startswith("hashed_"))
        self.assertNotIn(secret, self.coordinator.events_path.read_text(encoding="utf-8"))

    def test_no_reply_and_expiry_are_audited_and_next_turn_does_not_replace_them(self):
        primary = self.primary()
        self.coordinator.complete(primary["token"])
        self.publish(trigger_id="next", created_at=self.now - 590)
        self.now += 11
        self.coordinator.snapshot()
        events = self.events()
        self.assertEqual([e["outcome"] for e in events], ["no_reply", "expired"])
        self.assertEqual(events[1]["created_at"], 410)
        self.assertIsNone(events[1]["claimed_at"])
        self.assertIsNone(events[1]["provider"])

    def test_audit_write_failure_does_not_block_reply_and_pending_survives_next_publish(self):
        primary = self.primary()
        with patch.object(self.coordinator, "_write_turn_events", side_effect=OSError("audit unavailable")):
            self.assertTrue(self.coordinator.submit_reply(primary["token"], ["reply"], "gemini")["accepted"])
            self.assertTrue(self.publish(trigger_id="next")["accepted"])
        disk = json.loads(self.coordinator.state_path.read_text(encoding="utf-8"))
        self.assertEqual(disk["status"], "queued")
        self.assertEqual(disk["pending_turn_events"][0]["trigger_id"], "trigger-1")
        restored = self.make_coordinator()
        restored.snapshot()
        self.assertEqual([e["trigger_id"] for e in self.events()], ["trigger-1"])
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(restored.snapshot()["pending_turn_events"], [])

    def test_audit_persisted_before_pending_clear_is_reconciled_without_duplicates(self):
        primary = self.primary()
        save = self.coordinator._save
        def crash_when_clearing(state):
            if state.get("status") == "completed" and state.get("pending_turn_events") == []:
                raise OSError("crash after audit commit")
            return save(state)
        with patch.object(self.coordinator, "_save", side_effect=crash_when_clearing):
            self.assertTrue(self.coordinator.complete(primary["token"]))
        self.assertEqual(len(self.events()), 1)
        restored = self.make_coordinator()
        restored.snapshot()
        restored.snapshot()
        self.assertEqual(len(self.events()), 1)
        self.assertTrue(restored.snapshot()["audit_event_recorded"])

    def test_audit_preparation_failure_does_not_block_completion_and_can_be_recovered(self):
        primary = self.primary()
        with patch.object(self.coordinator, "_queue_turn_event", side_effect=RuntimeError("audit unavailable")):
            self.assertTrue(self.coordinator.complete(primary["token"]))
        self.assertEqual(self.events(), [])
        self.assertEqual(self.make_coordinator().snapshot()["status"], "completed")
        self.assertEqual(len(self.events()), 1)

    def test_audit_rotation_bounds_record_count_bytes_and_keeps_newest(self):
        with patch.object(coordinator_module, "MAX_TURN_EVENTS", 3), \
                patch.object(coordinator_module, "MAX_TURN_EVENTS_BYTES", 1024):
            for i in range(8):
                self.publish(trigger_id=f"turn-{i}")
                primary = self.coordinator.claim_primary()
                self.coordinator.complete(primary["token"])
                self.now += 1
        self.assertLessEqual(len(self.events()), 3)
        self.assertLessEqual(self.coordinator.events_path.stat().st_size, 1024)
        self.assertEqual(self.events()[-1]["trigger_id"], "turn-7")
        self.assertTrue(all(len(self.coordinator._record_bytes(event)) <= coordinator_module.MAX_TURN_EVENT_BYTES
                            for event in self.events()))

    def test_malformed_or_partial_audit_tail_does_not_break_new_completion(self):
        primary = self.primary()
        self.coordinator.events_path.write_bytes(b'not-json\n{"unfinished":')
        self.assertTrue(self.coordinator.complete(primary["token"]))
        self.assertEqual([e["trigger_id"] for e in self.events()], ["trigger-1"])

    def test_restored_legacy_completed_turn_gets_one_audit_before_replacement(self):
        primary = self.primary()
        self.coordinator.complete(primary["token"])
        state = json.loads(self.coordinator.state_path.read_text(encoding="utf-8"))
        state.pop("audit_event_recorded", None)
        state.pop("pending_turn_events", None)
        self.coordinator.state_path.write_text(json.dumps(state), encoding="utf-8")
        self.coordinator.events_path.unlink()
        self.assertTrue(self.publish(trigger_id="next")["accepted"])
        self.assertEqual([e["trigger_id"] for e in self.events()], ["trigger-1"])

    def test_payload_persists_and_only_one_primary_can_claim(self):
        self.assertTrue(self.publish()["accepted"])
        claim = self.make_coordinator().claim_primary("trigger-1")
        self.assertEqual(claim["provider"], "gemini")
        self.assertEqual(claim["payload"]["recent_context_messages"][0]["text"], "synthetic test context")
        self.assertIsNone(self.coordinator.claim_primary())
        self.assertTrue(self.coordinator.check_token(claim["token"], "gemini"))

    def test_busy_turn_cannot_be_overwritten(self):
        self.publish()
        result = self.publish(trigger_id="trigger-2")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "busy")
        self.assertEqual(self.coordinator.snapshot()["trigger_id"], "trigger-1")

    def test_fallback_waits_for_primary_grace(self):
        self.publish()
        self.assertIsNone(self.coordinator.claim_fallback())
        self.now += 90
        self.assertIsNone(self.coordinator.claim_fallback())
        # Suspicion persists across supervisor restarts; no timer reset.
        self.now += 9
        self.assertIsNone(self.make_coordinator().claim_fallback())
        self.now += 1
        claim = self.coordinator.claim_fallback()
        self.assertEqual(claim["takeover_reason"], "primary_grace_expired")
        self.assertIsNone(self.coordinator.claim_primary())
        self.assertIsNone(self.coordinator.claim_fallback())

    def test_primary_timeout_is_not_mislabeled_as_quota(self):
        primary = self.primary()
        self.now += 59
        self.assertIsNone(self.coordinator.claim_fallback())
        self.assertTrue(self.coordinator.check_token(primary["token"]))
        self.now += 1
        self.assertIsNone(self.coordinator.claim_fallback())
        self.now += 10
        fallback = self.coordinator.claim_fallback()
        self.assertEqual(fallback["takeover_reason"], "primary_stalled")
        self.assertFalse(self.coordinator.check_token(primary["token"]))

    def test_primary_can_claim_during_queue_confirmation(self):
        self.publish()
        self.now += 90
        self.assertIsNone(self.coordinator.claim_fallback())
        self.now += 20
        primary = self.coordinator.claim_primary()
        self.assertIsNotNone(primary)
        self.now += 20
        self.assertIsNone(self.coordinator.claim_fallback())
        self.assertNotIn("takeover_suspected_at", self.coordinator.snapshot())

    def test_primary_can_complete_during_stall_confirmation(self):
        primary = self.primary()
        self.now += 180
        self.assertIsNone(self.coordinator.claim_fallback())
        self.now += 20
        result = self.make_coordinator().submit_reply(primary["token"], ["deliberate answer"], "gemini")
        self.assertTrue(result["accepted"])
        self.now += 20
        self.assertIsNone(self.coordinator.claim_fallback())
        self.assertEqual(len(self.records()), 1)

    def test_genuine_progress_cancels_suspicion_but_cannot_extend_hard_deadline(self):
        primary = self.primary()
        original_deadline = primary["deadline"]
        self.now += 180
        self.assertIsNone(self.coordinator.claim_fallback())
        self.now += 20
        self.assertFalse(self.coordinator.record_progress("wrong-token"))
        self.assertTrue(self.make_coordinator().record_progress(primary["token"]))
        self.assertNotIn("takeover_suspected_at", self.coordinator.snapshot())
        self.now += 30
        self.assertIsNone(self.coordinator.claim_fallback())
        self.now += 130
        self.assertTrue(self.coordinator.record_progress(primary["token"]))
        self.assertEqual(self.coordinator.snapshot()["deadline"], original_deadline)
        self.now = original_deadline
        self.assertFalse(self.coordinator.record_progress(primary["token"]))
        fallback = self.make_coordinator().claim_fallback()
        self.assertEqual(fallback["takeover_reason"], "primary_hard_timeout")
        self.assertFalse(self.coordinator.record_progress(primary["token"]))
        self.assertFalse(self.coordinator.record_progress(fallback["token"]))

    def test_explicit_error_overrides_recent_progress_without_confirmation(self):
        primary = self.primary()
        self.now += 15
        self.assertTrue(self.coordinator.record_progress(primary["token"]))
        self.assertTrue(self.coordinator.report_failure(primary["token"], "auth"))
        self.assertFalse(self.coordinator.record_progress(primary["token"]))
        self.assertEqual(self.coordinator.claim_fallback()["takeover_reason"], "reported:auth")

    def test_recent_successful_latency_widens_patience_and_is_persisted(self):
        self.coordinator.state_dir.mkdir(exist_ok=True)
        rows = [{"trigger_id": f"history-{i}", "provider": "gemini", "outcome": "replied",
                 "created_at": 200 + i, "claimed_at": 300 + i, "completed_at": 520 + i}
                for i in range(3)]
        self.coordinator.events_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        self.publish()
        self.assertEqual(self.coordinator.snapshot()["primary_grace_deadline"], self.now + 40)
        primary = self.coordinator.claim_primary()
        self.assertEqual(self.coordinator.snapshot()["primary_idle_deadline"], self.now + 90)
        self.now += 210
        self.assertIsNone(self.make_coordinator().claim_fallback())
        self.assertTrue(self.coordinator.check_token(primary["token"]))

    def test_errors_stale_or_malformed_history_cannot_inflate_patience(self):
        self.coordinator.state_dir.mkdir(exist_ok=True)
        self.now = 100000
        rows = [
            {"provider": "dsh", "outcome": "replied", "created_at": 99000, "claimed_at": 99100, "completed_at": 99400},
            {"provider": "gemini", "outcome": "fallback_error", "created_at": 99000, "claimed_at": 99100, "completed_at": 99400},
            {"provider": "gemini", "outcome": "replied", "created_at": 100, "claimed_at": 200, "completed_at": 500},
            {"provider": "gemini", "outcome": "replied", "created_at": 99000, "claimed_at": True, "completed_at": 99400},
        ] * 3
        self.coordinator.events_path.write_text("broken\n[]\n" + "\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        self.publish()
        primary = self.coordinator.claim_primary()
        self.assertEqual(self.coordinator.snapshot()["primary_idle_deadline"], self.now + 60)
        self.assertEqual(primary["deadline"], self.now + 420)

    def test_reported_quota_allows_immediate_fallback(self):
        primary = self.primary()
        self.assertTrue(self.coordinator.report_failure(primary["token"], "quota"))
        fallback = self.coordinator.claim_fallback()
        self.assertEqual(fallback["takeover_reason"], "reported:quota")
        self.assertEqual(fallback["provider"], "dsh")

    def test_old_primary_results_are_rejected_after_takeover(self):
        primary = self.primary()
        self.coordinator.report_failure(primary["token"], "model_error")
        fallback = self.coordinator.claim_fallback()
        self.assertFalse(self.coordinator.submit_reply(primary["token"], ["late primary"], "gemini")["accepted"])
        self.assertFalse(self.coordinator.complete(primary["token"]))
        self.assertFalse(self.coordinator.report_failure(primary["token"], "quota"))
        result = self.coordinator.submit_reply(fallback["token"], ["fallback reply"], "dsh")
        self.assertTrue(result["accepted"])
        self.assertEqual(self.records()[0]["segments"], ["fallback reply"])

    def test_provider_mismatch_is_rejected(self):
        primary = self.primary()
        self.assertFalse(self.coordinator.submit_reply(primary["token"], ["test"], "dsh")["accepted"])
        self.assertTrue(self.coordinator.submit_reply(primary["token"], ["test"], "gemini")["accepted"])

    def test_reply_completes_only_once(self):
        primary = self.primary()
        first = self.coordinator.submit_reply(primary["token"], ["one"], "gemini")
        second = self.coordinator.submit_reply(primary["token"], ["two"], "gemini")
        self.assertTrue(first["accepted"])
        self.assertFalse(second["accepted"])
        self.assertFalse(self.coordinator.complete(primary["token"]))
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.coordinator.snapshot()["outcome"], "replied")

    def test_complete_no_reply_invalidates_owner_and_releases_next_turn(self):
        primary = self.primary()
        self.assertTrue(self.coordinator.complete(primary["token"]))
        self.assertFalse(self.coordinator.check_token(primary["token"]))
        self.assertFalse(self.publish()["accepted"])
        self.assertTrue(self.publish(trigger_id="trigger-2")["accepted"])
        self.assertEqual(self.records(), [])

    def test_queued_turn_expires_using_original_creation_time(self):
        self.publish(created_at=self.now - 590)
        self.now += 11
        self.assertIsNone(self.coordinator.claim_fallback())
        self.assertEqual(self.coordinator.snapshot()["outcome"], "expired")

    def test_already_expired_payload_is_rejected(self):
        self.assertFalse(self.publish(created_at=self.now - 601)["accepted"])
        self.assertEqual(self.coordinator.snapshot()["status"], "idle")

    def test_fallback_deadline_and_failure_are_terminal(self):
        self.publish()
        self.now += 90
        self.assertIsNone(self.coordinator.claim_fallback())
        self.now += 30
        fallback = self.coordinator.claim_fallback()
        self.now += 120
        self.assertFalse(self.coordinator.check_token(fallback["token"]))
        self.assertEqual(self.coordinator.snapshot()["outcome"], "fallback_timeout")
        self.publish(trigger_id="next")
        self.now += 90
        self.assertIsNone(self.coordinator.claim_fallback())
        self.now += 30
        fallback = self.coordinator.claim_fallback()
        self.assertTrue(self.coordinator.report_failure(fallback["token"], "process_failed"))
        self.assertEqual(self.coordinator.snapshot()["outcome"], "fallback_failed")

    def test_invalid_segments_do_not_complete_turn(self):
        primary = self.primary()
        for segments in [[], [""], ["x" * 4001], ["a"] * 7, "text"]:
            self.assertFalse(self.coordinator.submit_reply(primary["token"], segments, "gemini")["accepted"])
        self.assertTrue(self.coordinator.check_token(primary["token"]))

    def test_crash_before_outbox_append_is_recovered_once(self):
        primary = self.primary()
        with patch.object(self.coordinator, "_append_bytes", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                self.coordinator.submit_reply(primary["token"], ["test"], "gemini")
        restored = self.make_coordinator()
        self.assertEqual(restored.snapshot()["outcome"], "replied")
        self.assertFalse(restored.submit_reply(primary["token"], ["duplicate"], "gemini")["accepted"])
        self.assertEqual(len(self.records()), 1)

    def test_crash_after_append_does_not_duplicate(self):
        primary = self.primary()
        with patch.object(self.coordinator, "_finish", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                self.coordinator.submit_reply(primary["token"], ["test"], "gemini")
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.make_coordinator().snapshot()["outcome"], "replied")
        self.assertEqual(len(self.records()), 1)

    def test_partial_utf8_record_is_completed_without_duplicate(self):
        primary = self.primary()
        real_append = self.coordinator._append_bytes
        def partial_append(raw):
            split = raw.index("猫".encode("utf-8")) + 1
            real_append(raw[:split])
            raise OSError("simulated partial write")
        with patch.object(self.coordinator, "_append_bytes", side_effect=partial_append):
            with self.assertRaises(OSError):
                self.coordinator.submit_reply(primary["token"], ["猫猫测试"], "gemini")
        self.make_coordinator().snapshot()
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["segments"], ["猫猫测试"])

    def test_external_complete_record_is_preserved_during_recovery(self):
        primary = self.primary()
        real_append = self.coordinator._append_bytes
        def other_writer(raw):
            real_append(b'{"reply_id":"external","segments":["preserve"]}\n')
            raise OSError("simulated crash")
        with patch.object(self.coordinator, "_append_bytes", side_effect=other_writer):
            with self.assertRaises(OSError):
                self.coordinator.submit_reply(primary["token"], ["ours"], "gemini")
        self.make_coordinator().snapshot()
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(self.records()[0]["reply_id"], "external")

    def test_partial_external_conflict_fails_closed(self):
        primary = self.primary()
        with patch.object(self.coordinator, "_append_bytes", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                self.coordinator.submit_reply(primary["token"], ["ours"], "gemini")
        self.coordinator._append_bytes(b"unrelated unfinished record")
        before = self.coordinator.outbox_path.read_bytes()
        with self.assertRaises(CoordinatorError):
            self.make_coordinator().snapshot()
        self.assertEqual(self.coordinator.outbox_path.read_bytes(), before)

    def test_corrupt_state_fails_closed(self):
        self.publish()
        self.coordinator.state_path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(CoordinatorError):
            self.make_coordinator().claim_fallback()

    def test_existing_incomplete_outbox_is_not_corrupted(self):
        primary = self.primary()
        self.coordinator._append_bytes(b'{"unfinished":')
        result = self.coordinator.submit_reply(primary["token"], ["test"], "gemini")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "outbox_incomplete_record")
        self.assertEqual(self.coordinator.outbox_path.read_bytes(), b'{"unfinished":')
        self.assertTrue(self.coordinator.check_token(primary["token"]))

    def test_cross_process_submit_commits_only_once(self):
        coordinator = TurnCoordinator(self.base)
        coordinator.publish({"session_key": "group:123"})
        primary = coordinator.claim_primary()
        script = "import hakimi; from turn_coordinator import TurnCoordinator; import sys; print(TurnCoordinator(sys.argv[1]).submit_reply(sys.argv[2], ['test'], 'gemini')['accepted'])"
        children = [subprocess.Popen([sys.executable, "-c", script, str(self.base), primary["token"]],
                    cwd=str(self.test_root.parent), env=dict(os.environ, PYTHONPATH=str(self.test_root.parent / 'src')), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0) for _ in range(4)]
        results = []
        try:
            for child in children:
                output, error = child.communicate(timeout=20)
                self.assertEqual(child.returncode, 0, error.decode("utf-8", errors="replace"))
                results.append(output.decode("utf-8").strip())
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
        self.assertEqual(results.count("True"), 1)
        self.assertEqual(len(self.records()), 1)

    def test_cross_process_claim_has_one_winner(self):
        coordinator = TurnCoordinator(self.base)
        coordinator.publish({"session_key": "group:123"})
        script = "import hakimi; from turn_coordinator import TurnCoordinator; import sys; print(bool(TurnCoordinator(sys.argv[1]).claim_primary()))"
        children = [subprocess.Popen([sys.executable, "-c", script, str(self.base)],
                    cwd=str(self.test_root.parent), env=dict(os.environ, PYTHONPATH=str(self.test_root.parent / 'src')), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0) for _ in range(4)]
        results = []
        try:
            for child in children:
                output, error = child.communicate(timeout=20)
                self.assertEqual(child.returncode, 0, error.decode("utf-8", errors="replace"))
                results.append(output.decode("utf-8").strip())
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
        self.assertEqual(results.count("True"), 1)
        self.assertEqual(results.count("False"), 3)


if __name__ == "__main__":
    unittest.main()
