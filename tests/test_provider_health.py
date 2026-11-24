import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from hakimi.models.provider_health import ProviderHealth
from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.tools import qq_cli
from hakimi.tools.bot_tools import ToolError


class ProviderHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve()
        self.now=time.time()
        self.coordinator=TurnCoordinator(self.base,clock=lambda:self.now)
        self.health=ProviderHealth(self.base,clock=lambda:self.now)
        self.number=0

    def takeover(self,coordinator=None):
        c=coordinator or self.coordinator
        self.number+=1
        c.publish({"trigger_id":f"trigger-{self.number}","session_key":"group:123","created_at":self.now})
        primary=c.claim_primary()
        c.report_failure(primary["token"],"unavailable")
        fallback=c.claim_fallback()
        self.assertIsNotNone(fallback)
        c.complete(fallback["token"],"no_reply")
        self.now+=1
        return primary

    def test_three_distinct_takeovers_share_fast_state_between_lanes_and_restarts(self):
        official=self.base/"official_test_runtime"
        official.mkdir()
        other=TurnCoordinator(official,clock=lambda:self.now)
        self.takeover()
        self.takeover(other)
        self.assertFalse(self.health.snapshot()["fast"])
        self.takeover()
        self.assertTrue(ProviderHealth(official).snapshot()["fast"])
        policy=TurnCoordinator(self.base,clock=lambda:self.now)._primary_timing()
        self.assertLessEqual(policy["grace_seconds"]+policy["confirmation_seconds"],11)
        self.assertLessEqual(policy["idle_seconds"]+policy["confirmation_seconds"],21)

    def test_fast_queued_turn_takes_over_in_seconds_without_changing_generation_ttl(self):
        for _ in range(3):self.takeover()
        self.coordinator.publish({"session_key":"group:123","created_at":self.now})
        self.now+=6
        self.assertIsNone(self.coordinator.claim_fallback())
        self.now+=3
        claim=self.coordinator.claim_fallback()
        self.assertIsNotNone(claim)
        self.assertAlmostEqual(claim["deadline"]-self.now,120)

    def test_verified_late_reply_restores_slow_and_is_still_rejected(self):
        for _ in range(3):old=self.takeover()
        self.coordinator.publish({"session_key":"group:123","created_at":self.now})
        self.assertTrue(self.coordinator.snapshot()["primary_policy"]["fast_fallback"])
        self.assertFalse(self.coordinator.submit_reply(old["token"],["late text"],"gemini")["accepted"])
        self.assertFalse(self.health.snapshot()["fast"])
        self.now+=9
        self.assertIsNone(self.coordinator.claim_fallback())
        self.assertGreater(self.coordinator.snapshot()["primary_grace_deadline"],self.now+15)
        self.assertLessEqual(self.coordinator.snapshot()["primary_grace_deadline"],self.now+45)
        self.assertFalse((self.base/"state/outbox.jsonl").exists())

    def test_unknown_wrong_scope_and_replayed_late_proofs_cannot_reset_new_fast_mode(self):
        for _ in range(3):old=self.takeover()
        self.assertFalse(self.health.late_reply("f"*32,"group:123"))
        self.assertFalse(self.health.late_reply(old["token"],"group:456"))
        self.assertTrue(self.health.snapshot()["fast"])
        self.assertTrue(self.health.late_reply(old["token"],"group:123"))
        for _ in range(3):self.takeover()
        self.assertFalse(self.health.late_reply(old["token"],"group:123"))
        self.assertTrue(self.health.snapshot()["fast"])
        self.assertNotIn(old["token"].encode(),self.health.path.read_bytes())

    def test_primary_success_breaks_consecutive_takeovers(self):
        for _ in range(2):self.takeover()
        self.coordinator.publish({"session_key":"group:123","created_at":self.now})
        primary=self.coordinator.claim_primary()
        self.coordinator.complete(primary["token"],"no_reply")
        self.assertEqual(self.health.snapshot()["streak"],0)
        self.takeover()
        self.assertFalse(self.health.snapshot()["fast"])

    def test_cli_late_reply_can_signal_recovery_even_after_context_expired(self):
        for _ in range(3):old=self.takeover()
        args=SimpleNamespace(session="group:123",turn=old["token"],text="这条已经迟到了",reply_to="",sticker="")
        with patch.object(qq_cli,"BASE_DIR",self.base),self.assertRaises(ToolError):
            qq_cli.cmd_reply(args)
        self.assertFalse(self.health.snapshot()["fast"])
        self.assertFalse((self.base/"state/outbox.jsonl").exists())

    def test_operator_pause_is_shared_and_uses_fast_wait_without_guessing_quota(self):
        self.coordinator.publish({"session_key":"group:123","created_at":self.now})
        self.health.pause_primary()
        self.assertIsNone(self.coordinator.claim_fallback())
        self.now+=8
        self.assertIsNone(self.coordinator.claim_fallback())
        self.now+=3
        claim=self.coordinator.claim_fallback()
        self.assertEqual(claim["takeover_reason"],"primary_grace_expired")
        shared=ProviderHealth(self.base/"official_test_runtime",clock=lambda:self.now).snapshot()
        self.assertTrue(shared["primary_paused"])
        self.assertTrue(shared["fast"])
        self.assertEqual(shared["mode"],"auto")
        self.assertNotIn("quota",shared["signal"])

    def test_claim_alone_does_not_signal_return_and_primary_can_always_claim_auto_queue(self):
        self.health.pause_primary()
        self.coordinator.publish({"session_key":"group:123","created_at":self.now})
        primary=self.coordinator.claim_primary()
        self.assertIsNotNone(primary)
        self.assertTrue(self.health.snapshot()["primary_paused"])
        self.assertTrue(self.health.snapshot()["fast"])
        self.coordinator.report_failure(primary["token"],"unavailable")
        fallback=self.coordinator.claim_fallback()
        self.coordinator.complete(fallback["token"],"no_reply")
        self.coordinator.publish({"session_key":"group:123","created_at":self.now})
        self.assertIsNotNone(self.coordinator.claim_primary())

    def test_late_real_reply_exits_takeover_without_replacing_current_dsh_reply(self):
        self.health.pause_primary()
        self.coordinator.publish({"session_key":"group:123","created_at":self.now})
        primary=self.coordinator.claim_primary()
        self.coordinator.report_failure(primary["token"],"unavailable")
        fallback=self.coordinator.claim_fallback()
        self.assertIsNotNone(fallback)
        self.assertFalse(self.coordinator.submit_reply(primary["token"],["Gemini迟到的回复"],"gemini")["accepted"])
        self.assertFalse(self.health.snapshot()["primary_paused"])
        self.assertFalse(self.health.snapshot()["fast"])
        self.assertEqual(self.coordinator.snapshot()["status"],"fallback_active")
        self.assertTrue(self.coordinator.submit_reply(fallback["token"],["仍发送dsh的本轮回复"],"dsh")["accepted"])
        records=[json.loads(line) for line in self.coordinator.outbox_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["provider"] for r in records],["dsh"])
        other=TurnCoordinator(self.base/"official_test_runtime",clock=lambda:self.now)
        other.publish({"session_key":"group:123","created_at":self.now})
        self.assertGreater(other.snapshot()["primary_grace_deadline"]-self.now,15)
        self.assertLessEqual(other.snapshot()["primary_grace_deadline"]-self.now,45)
        self.assertIsNone(other.claim_fallback())
        self.assertIsNotNone(other.claim_primary())

    def test_successful_recovery_clears_pause_and_returns_fallback_to_standby(self):
        self.health.pause_primary()
        self.coordinator.publish({"session_key":"group:123","created_at":self.now})
        primary=self.coordinator.claim_primary()
        self.assertTrue(self.coordinator.submit_reply(primary["token"],["恢复后的本轮回复"],"gemini")["accepted"])
        self.assertFalse(self.health.snapshot()["primary_paused"])
        self.assertFalse(self.health.snapshot()["fast"])
        self.coordinator.publish({"session_key":"group:123","created_at":self.now})
        self.assertIsNone(self.coordinator.claim_fallback())
        self.assertIsNotNone(self.coordinator.claim_primary())

    def test_operator_modes_do_not_steal_an_active_dsh_reply(self):
        self.health.set_mode("dsh")
        self.coordinator.publish({"session_key":"group:123","created_at":self.now})
        self.assertIsNone(self.coordinator.claim_primary())
        fallback=self.coordinator.claim_fallback()
        self.assertEqual(fallback["takeover_reason"],"operator_mode_dsh")
        self.health.set_mode("gemini")
        self.assertIsNone(self.coordinator.claim_primary())
        self.assertTrue(self.coordinator.submit_reply(fallback["token"],["本轮继续完成"],"dsh")["accepted"])
        self.coordinator.publish({"session_key":"group:123","created_at":self.now})
        self.now+=300
        self.assertIsNone(self.coordinator.claim_fallback())
        self.assertIsNotNone(self.coordinator.claim_primary())
        self.health.set_mode("auto")
        self.assertEqual(self.health.snapshot()["mode"],"auto")

    def test_legacy_database_reads_defaults_and_migrates_on_control_write(self):
        self.health.path.parent.mkdir(parents=True)
        db=sqlite3.connect(self.health.path)
        try:
            db.executescript("CREATE TABLE health(id INTEGER PRIMARY KEY,streak INTEGER,fast INTEGER,epoch INTEGER,changed REAL,signal TEXT); INSERT INTO health VALUES(1,0,0,0,0,'initial');")
        finally:
            db.close()
        self.assertEqual(self.health.snapshot()["mode"],"auto")
        self.health.pause_primary()
        self.assertTrue(self.health.snapshot()["primary_paused"])
        self.health.resume_primary()
        self.assertFalse(self.health.snapshot()["fast"])


if __name__=="__main__":unittest.main()
