"""Offline V5 tests: social opportunity, reflection scheduler, Terra, budget, layout."""
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hakimi.ops import cloud_service
from hakimi.console import memory_console
from hakimi.memory import memory_lifecycle
from hakimi.social import memory_social
from hakimi.memory import personal_review
from hakimi.tools import qq_cli
from hakimi.ops import terra_budget
from hakimi.ops import terra_supervision
from hakimi.models.agy_session import AgySession
from hakimi.memory.memory_store import store
from hakimi.bridge.turn_coordinator import TurnCoordinator
from _repo import repo_file


class TempBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix="v5_")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        (self.base / "state").mkdir()
        self.store = store(str(self.base))
        self.now = 2_000_000_000.0

    def chat(self, session, user, text, message_id, at, targets=(), reply_to="", self_id=""):
        self.store.note({
            "session_key": session, "kind": "chat", "user_id": str(user), "text": text,
            "message_id": str(message_id), "received_at": float(at),
            "direct_targets": list(targets), "reply_to": str(reply_to), "self_id": str(self_id),
        })
        with self.store.db() as db:
            row = db.execute("SELECT id FROM records WHERE kind='chat' AND origin=? ORDER BY id DESC LIMIT 1",
                             (str(message_id),)).fetchone()
        return int(row[0])


class SocialOpportunityTests(TempBase):
    def test_meaningful_interaction_has_current_evidence_and_cooldown(self):
        mid = self.chat("group:1", "101", "@哈基米 说好了，下次一起完成音频实验", "m1", self.now - 60,
                        targets=["999"], self_id="999")
        target = {"user_id": "101", "message_id": "m1", "text": "@哈基米 说好了，下次一起完成音频实验"}
        opportunity = memory_lifecycle.social_opportunity(
            "group:1", [target], target, self.base, self.now)
        self.assertIsNotNone(opportunity)
        self.assertEqual(opportunity["evidence_memory_ids"], ["M" + str(mid)])
        self.assertLessEqual(len(opportunity["evidence"]), memory_lifecycle.MAX_SOCIAL_EVIDENCE)
        self.assertEqual(opportunity["target_id"], "101")
        self.assertIn("memory.social", opportunity["guidance"])
        self.assertIn("逐字", opportunity["guidance"])
        # Same target/evidence is suppressed by cooldown/dedup.
        self.assertIsNone(memory_lifecycle.social_opportunity(
            "group:1", [target], target, self.base, self.now + 60))
        # Ordinary chatter never creates an impression prompt.
        plain = {"user_id": "101", "message_id": "m1", "text": "@哈基米 晚上好呀"}
        self.assertIsNone(memory_lifecycle.social_opportunity(
            "group:2", [plain], plain, self.base, self.now + 70000))

    def test_private_evidence_is_not_used_for_group_opportunity(self):
        self.chat("private:101", "101", "说好了，我答应你下次一起完成", "p1", self.now - 60)
        target = {"user_id": "101", "message_id": "p1", "text": "说好了，我答应你下次一起完成"}
        self.assertIsNone(memory_lifecycle.social_opportunity(
            "group:1", [target], target, self.base, self.now))


class TerraProvenanceMigrationTests(unittest.TestCase):
    def test_old_v4_table_is_rebuilt_without_losing_versions_or_indexes(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix="v5_migrate_") as directory:
            path = Path(directory) / "social.sqlite3"
            old = memory_social.SCHEMA.replace(
                "CHECK (provenance IN ('gemini_manual', 'dsh_assisted', 'terra_assisted'))",
                "CHECK (provenance IN ('gemini_manual', 'dsh_assisted'))")
            old = old.replace(" evidence_times TEXT NOT NULL DEFAULT '[]',\n expires_at REAL NOT NULL DEFAULT 0,\n", "")
            with closing(sqlite3.connect(path)) as db:
                db.executescript(old)
                db.execute(
                    "INSERT INTO social_assessments(request_id,subject_id,target_id,author,provider,provenance,"
                    "impression,reason,affinity,closeness,confidence,relationship_type,evidence_record_ids,"
                    "evidence_dates,original_record_id,created_at,session,version,supersedes_id,superseded_by,"
                    "state,seed_id) VALUES ('r1','hakimi','101','gemini','gemini','gemini_manual',"
                    "'旧印象','',42,NULL,NULL,'关系印象','[]','[]',NULL,1,'group:1',1,NULL,NULL,'superseded','')")
                db.execute(
                    "INSERT INTO social_assessments(request_id,subject_id,target_id,author,provider,provenance,"
                    "impression,reason,affinity,closeness,confidence,relationship_type,evidence_record_ids,"
                    "evidence_dates,original_record_id,created_at,session,version,supersedes_id,superseded_by,"
                    "state,seed_id) VALUES ('r2','hakimi','101','gemini','gemini','gemini_manual',"
                    "'新印象','',55,NULL,NULL,'关系印象','[]','[]',NULL,2,'group:1',2,1,NULL,'active','')")
                memory_social.ensure_schema(db)
                rows = db.execute("SELECT id,version,state,supersedes_id,affinity FROM social_assessments ORDER BY id").fetchall()
                self.assertEqual(rows, [(1, 1, "superseded", None, 42), (2, 2, "active", 1, 55)])
                sql = db.execute("SELECT sql FROM sqlite_master WHERE name='social_assessments'").fetchone()[0]
                self.assertIn("terra_assisted", sql)
                indexes = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='social_assessments'")}
                self.assertEqual(indexes, {"social_assessments_request", "social_assessments_subject",
                                           "social_assessments_target", "social_assessments_pair",
                                           "social_assessments_original", "social_assessments_session"})
                db.execute("INSERT INTO social_assessments(request_id,subject_id,target_id,author,provider,provenance,"
                           "impression,reason,relationship_type,evidence_record_ids,evidence_dates,created_at,session) "
                           "VALUES ('r3','hakimi','102','terra','terra','terra_assisted','观察','','关系印象','[]','[]',3,'group:1')")
                db.commit()

    def test_failed_rebuild_rolls_back_and_never_drops_a_preexisting_backup(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix="v5_migrate_fail_") as directory:
            path = Path(directory) / "social.sqlite3"
            old = memory_social.SCHEMA.replace(
                "CHECK (provenance IN ('gemini_manual', 'dsh_assisted', 'terra_assisted'))",
                "CHECK (provenance IN ('gemini_manual', 'dsh_assisted'))")
            old = old.replace(" evidence_times TEXT NOT NULL DEFAULT '[]',\n expires_at REAL NOT NULL DEFAULT 0,\n", "")
            with closing(sqlite3.connect(path)) as db:
                db.executescript(old)
                db.execute(
                    "INSERT INTO social_assessments(request_id,subject_id,target_id,author,provider,provenance,"
                    "impression,reason,relationship_type,evidence_record_ids,evidence_dates,created_at,session) "
                    "VALUES ('old','hakimi','101','gemini','gemini','gemini_manual','原样保留','','群友','[]','[]',1,'group:1')")
                db.execute("CREATE TABLE social_assessments_v4_backup(payload TEXT)")
                db.execute("INSERT INTO social_assessments_v4_backup VALUES ('不得删除')")
                db.commit()

                def deny_insert(action, arg1, arg2, dbname, source):
                    return (sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT
                            and arg1 == "social_assessments" else sqlite3.SQLITE_OK)
                db.set_authorizer(deny_insert)
                with self.assertRaises(sqlite3.DatabaseError):
                    memory_social.ensure_schema(db)
                db.set_authorizer(None)
                self.assertEqual(db.execute(
                    "SELECT impression FROM social_assessments WHERE request_id='old'").fetchone()[0], "原样保留")
                self.assertEqual(db.execute(
                    "SELECT payload FROM social_assessments_v4_backup").fetchone()[0], "不得删除")
                self.assertNotIn("terra_assisted",
                                 db.execute("SELECT sql FROM sqlite_master WHERE name='social_assessments'").fetchone()[0])


class PersonalReviewTests(TempBase):
    def setUp(self):
        super().setUp()
        # The coordinator and active-context lease use wall time.
        self.now = time.time()
        for index in range(6):
            self.chat("group:1", "101", f"@哈基米 说好了一起完成实验{index}，这次解决了",
                      f"a{index}", self.now - 1500 * (index + 1), targets=["999"], self_id="999")
        self.chat("group:2", "202", "@哈基米 谢谢你帮我整理完成，辛苦了", "b1",
                  self.now - 900, targets=["999"], self_id="999")

    def test_reply_origin_and_legacy_cq_at_without_quoted_leak(self):
        self.chat("group:9", "1000", "回答", "qq-message-77", self.now - 100, self_id="1000")
        self.chat("group:9", "101", "谢谢，弄懂了", "qq-message-78", self.now - 90,
                  reply_to="qq-message-77")
        self.chat("group:9", "102", "[CQ:at,qq=1000] 新任务完成了", "cq-at-1", self.now - 80)
        self.chat("group:9", "103", '[引用 某人 的消息(id=1): "[CQ:at,qq=1000] 旧引用"] 当前发言',
                  "quoted-1", self.now - 70)
        with self.store.db() as db:
            rows = personal_review._recent_records(db, self.now - 1000)
        events = personal_review._direct_events(rows, {"1000"})
        self.assertIn(("group:9", "101"), events)
        self.assertIn(("group:9", "102"), events)
        self.assertNotIn(("group:9", "103"), events)

    def test_auxiliary_observation_is_not_hakimis_own_impression(self):
        with self.store.db() as db:
            db.execute(
                "INSERT INTO social_assessments(request_id,subject_id,target_id,author,provider,provenance,"
                "impression,reason,relationship_type,evidence_record_ids,evidence_dates,created_at,session) "
                "VALUES ('self','hakimi','101','gemini','gemini','gemini_manual',"
                "'她自己的印象','','群友','[]','[]',?, 'group:1')", (self.now - 100,))
            db.execute(
                "INSERT INTO social_assessments(request_id,subject_id,target_id,author,provider,provenance,"
                "impression,reason,relationship_type,evidence_record_ids,evidence_dates,created_at,session) "
                "VALUES ('other','102','101','terra','terra','terra_assisted',"
                "'乙对甲的推测','','群友','[]','[]',?, 'group:1')", (self.now,))
            latest = personal_review._latest_impressions(db)
        self.assertEqual(latest[("group:1", "101")]["impression"], "她自己的印象")
        self.assertEqual(latest[("group:1", "101")]["provenance"], "gemini_manual")
        social = memory_lifecycle._latest_social("group:1", "101", self.base)
        self.assertEqual(social["last_impression"], "她自己的印象")
        self.assertIn("Gemini", social["source"])
        self.assertEqual(social["gemini_write_at"], self.now - 100)

    def test_four_hour_significant_event_bypasses_cooldown_only_when_really_new(self):
        fresh = Path(self.temp.name) / "cooldown"
        (fresh / "state").mkdir(parents=True)
        thin = store(str(fresh))
        thin.note({"session_key": "group:1", "kind": "chat", "user_id": "101",
                   "text": "@哈基米 谢谢你，这次真的完成了", "message_id": "c1",
                   "received_at": self.now - 600, "direct_targets": ["999"], "self_id": "999"})
        state = personal_review._load(fresh)
        state["people"]["101"] = {"last_reviewed": self.now - 3600,
                                  "last_significant_at": self.now - 6 * 3600}
        state["groups"]["group:1"] = {"last_reviewed": self.now - 3600}
        plan = personal_review.select_review(fresh, self.now, rng=random.Random(4), state=state)
        self.assertIsNotNone(plan)
        candidate = next(item for item in plan["candidates"] if item["person_id"] == "101")
        self.assertGreater(candidate["significant_at"], 0)

        state["people"]["101"]["last_significant_at"] = self.now - 1800
        self.assertIsNone(personal_review.select_review(
            fresh, self.now, rng=random.Random(4), state=state))

    def test_audit_rotation_is_readable_and_bounded(self):
        for index in range(1800):
            personal_review._audit(self.base, {"review_id": "r" + str(index), "group": "group:1"},
                                   "scheduled", self.now, reason="x" * 80, count=index % 3)
        path = personal_review._audit_path(self.base)
        self.assertLessEqual(path.stat().st_size, personal_review.MAX_AUDIT_BYTES)
        lines = path.read_bytes().splitlines()
        self.assertTrue(lines)
        self.assertEqual(json.loads(lines[-1].decode("utf-8"))["status"], "scheduled")

    def test_guidance_function_and_full_prompt_still_present(self):
        text = memory_lifecycle.guidance()
        self.assertIn("memory.social", text)
        self.assertIn("不每轮打分", text)
        self.assertIn("只有收到成功回执", text)
        self.assertIn("不是对方内心", text)
        from hakimi.prompt.prompt_context import render_prompt
        (self.base / "style_guide.md").write_text(
            (repo_file("style_guide.md")).read_text(encoding="utf-8"), encoding="utf-8")
        prompt = render_prompt({"session_key": "group:1", "recent_context_messages": [
            {"session_key": "group:1", "message_id": "1", "user_id": "101", "text": "记住这个"}]},
            self.base)
        self.assertIn("memory.social", prompt)
        self.assertIn("social_memory_opportunity", prompt)

    def test_internal_review_expiry_and_failure_release_real_turns(self):
        clock = [self.now]
        coordinator = TurnCoordinator(self.base, clock=lambda: clock[0])
        internal = {"session_key": "group:1", "wake_mode": "个人记忆反思",
                    "recent_context_messages": [], "internal_review": {"review_id": "r1",
                    "group": "group:1", "candidates": []}}
        self.assertTrue(coordinator.publish(internal)["accepted"])
        claim = coordinator.claim_primary()
        self.assertIsNotNone(claim)
        self.assertLessEqual(claim["deadline"], clock[0] + 90)
        clock[0] += 91
        state = coordinator.snapshot()
        self.assertEqual((state["status"], state["outcome"]), ("completed", "expired"))
        real = coordinator.publish({"session_key": "group:1", "trigger_id": "real-after-expiry",
                                    "recent_context_messages": []})
        self.assertTrue(real["accepted"])
        coordinator.complete(coordinator.claim_primary()["token"])

        self.assertTrue(coordinator.publish(internal)["accepted"])
        failed = coordinator.claim_primary()
        self.assertTrue(coordinator.report_failure(failed["token"], "model_error"))
        state = coordinator.snapshot()
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["outcome"], "no_reply")
        again = coordinator.publish({"session_key": "group:1", "trigger_id": "real-after-failure",
                                     "recent_context_messages": []})
        self.assertTrue(again["accepted"])
        self.assertFalse((self.base / "state" / "outbox.jsonl").exists())

    def test_selection_is_one_group_three_people_and_bounded_evidence(self):
        plan = personal_review.select_review(self.base, self.now, rng=random.Random(7))
        self.assertIsNotNone(plan)
        self.assertTrue(plan["group"].startswith("group:"))
        self.assertLessEqual(len(plan["candidates"]), 3)
        for candidate in plan["candidates"]:
            self.assertLessEqual(len(candidate["evidence"]), 8)
            self.assertTrue(candidate["evidence_memory_ids"])
            self.assertTrue(all(value.startswith("M") for value in candidate["evidence_memory_ids"]))
        self.assertEqual(plan["policy"]["evidence_limit"], 8)

    def test_tick_publishes_internal_turn_without_inventing_messages_or_outbox(self):
        result = personal_review.tick(self.base, self.now, publish=True)
        self.assertEqual(result["status"], "scheduled")
        coordinator = TurnCoordinator(self.base)
        state = coordinator.snapshot()
        self.assertEqual(state["status"], "queued")
        payload = state["payload"]
        self.assertEqual(payload["wake_mode"], "个人记忆反思")
        self.assertEqual(payload["recent_context_messages"], [])
        self.assertTrue(payload["internal_review"]["candidates"])
        self.assertFalse((self.base / "state" / "outbox.jsonl").exists())

        claim = coordinator.claim_primary()
        self.assertIsNotNone(claim)
        from hakimi.prompt.prompt_context import activate_context, render_prompt
        record = activate_context(payload, self.base, claim=claim)
        self.assertEqual(record["allowed_image_paths"], [])
        self.assertTrue(record["internal_review"])
        prompt = render_prompt(payload, self.base, claim, wait_after_turn=False)
        self.assertIn("不是QQ群消息", prompt)
        self.assertIn("memory.social", prompt)
        self.assertNotIn("recent_context_messages", prompt)

        # Coordinator defence in depth: even a direct call cannot create QQ output.
        submitted = coordinator.submit_reply(claim["token"], ["不应发送"], "gemini")
        self.assertFalse(submitted["accepted"])
        self.assertEqual(submitted["reason"], "internal_review_no_output")
        self.assertFalse((self.base / "state" / "outbox.jsonl").exists())

    def test_tool_guard_allows_social_and_skip_but_blocks_reply(self):
        personal_review.tick(self.base, self.now, publish=True)
        coordinator = TurnCoordinator(self.base)
        claim = coordinator.claim_primary()
        from hakimi.prompt.prompt_context import activate_context
        activate_context(coordinator.snapshot()["payload"], self.base, claim=claim, now=self.now)
        review = coordinator.snapshot()["payload"]["internal_review"]
        candidate = review["candidates"][0]
        with patch.multiple(qq_cli, BASE_DIR=self.base, STATE_DIR=self.base / "state"):
            args = SimpleNamespace(session=review["group"], turn=claim["token"], call_name="reply.send",
                                   text="不应创建发送记录", sticker=None, reply_to=None, interaction=None)
            with self.assertRaises(qq_cli.ToolError):
                qq_cli.cmd_call(args)
            self.assertFalse((self.base / "state" / "outbox.jsonl").exists())
            social = SimpleNamespace(session=review["group"], turn=claim["token"], call_name="memory.social",
                                     note="这次一起完成实验，比之前更可靠", target_id=candidate["person_id"],
                                     subject_id="hakimi", relationship_type="协作同伴",
                                     affinity=None, closeness=None, confidence=None,
                                     evidence_memory_ids=",".join(candidate["evidence_memory_ids"]))
            with redirect_stdout(io.StringIO()):
                qq_cli.cmd_call(social)
            with self.store.db() as db:
                row = db.execute("SELECT provenance,impression FROM social_assessments ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(row["provenance"], "gemini_manual")
            self.assertEqual(row["impression"], "这次一起完成实验，比之前更可靠")
            skip = SimpleNamespace(session=review["group"], turn=claim["token"], call_name="reply.skip")
            with redirect_stdout(io.StringIO()):
                qq_cli.cmd_call(skip)
            self.assertFalse((self.base / "state" / "outbox.jsonl").exists())
        self.assertFalse((self.base / "state" / "outbox.jsonl").exists())

    def test_active_real_turn_is_not_preempted(self):
        coordinator = TurnCoordinator(self.base)
        real = coordinator.publish({"session_key": "group:1", "trigger_id": "real-1",
                                    "recent_context_messages": [{"message_id": "a0", "user_id": "101", "text": "真实消息"}]})
        self.assertTrue(real["accepted"])
        result = personal_review.tick(self.base, self.now, publish=True)
        self.assertEqual(result["status"], "deferred")
        state = coordinator.snapshot()
        self.assertNotIn("internal_review", state["payload"])
        self.assertEqual(state["trigger_id"], "real-1")
        coordinator.complete(coordinator.claim_primary()["token"])

    def test_concurrent_scheduler_and_normal_turn_cannot_both_publish(self):
        coordinator = TurnCoordinator(self.base)
        barrier = threading.Barrier(2)
        outcomes = {}

        def review_worker():
            barrier.wait()
            outcomes["review"] = personal_review.tick(self.base, self.now, publish=True)

        def normal_worker():
            barrier.wait()
            outcomes["normal"] = coordinator.publish({
                "session_key": "group:1", "trigger_id": "real-concurrent",
                "recent_context_messages": []})

        threads = [threading.Thread(target=review_worker), threading.Thread(target=normal_worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        review_ok = outcomes.get("review", {}).get("status") == "scheduled"
        normal_ok = bool(outcomes.get("normal", {}).get("accepted"))
        self.assertFalse(review_ok and normal_ok)
        state = coordinator.snapshot()
        self.assertIn(state["status"], {"idle", "completed", "queued", "primary_active"})
        self.assertFalse((self.base / "state" / "outbox.jsonl").exists())


class TerraCycleTests(TempBase):
    def setUp(self):
        super().setUp()
        self.mids = []
        self.mids.append(self.chat("group:1", "101", "102 你看这个声学推导", "t1", self.now - 2 * 86400,
                                   targets=["102"]))
        self.mids.append(self.chat("group:1", "102", "101 我觉得第二步不对", "t2", self.now - 2 * 86400 + 100,
                                   targets=["101"]))
        self.mids.append(self.chat("group:1", "101", "102 我改好了，谢谢指正", "t3", self.now - 86400,
                                   targets=["102"]))
        self.mids.append(self.chat("group:1", "102", "101 现在清楚了", "t4", self.now - 3600,
                                   targets=["101"]))
        self.mids.append(self.chat("group:1", "101", "102 第三次核对也完成了", "t5", self.now - 1.5 * 86400,
                                   targets=["102"]))
        (self.base / "state" / "sent_messages.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in [
            {"provider": "dsh", "session_key": "group:1", "message_id": "d1", "at": self.now - 600,
             "text": "这个推导我分成两步讲，先看边界条件。"},
            {"provider": "dsh", "session_key": "group:1", "message_id": "d2", "at": self.now - 300,
             "text": "第二处符号我重新核对过了。"},
            {"provider": "gemini", "session_key": "group:1", "message_id": "g1", "at": self.now - 500,
             "text": "喵，这里先别急着下结论，我们把条件摆出来。"},
        ]), encoding="utf-8")
        self.budget = terra_budget.TerraBudget(self.base / "state" / "terra_watch" / "budget.json")

    def runner(self, prompt, schema):
        self.seen_prompt = prompt
        candidate = terra_supervision.relation_candidates(self.base, self.now)[0]
        evidence = candidate["evidence"][0]["memory_id"]
        return ({
            "style": {"verdict": "problem", "guidance": "少用客服式收尾，保留专业推导。", "uncertainty": "样本少"},
            "relations": [{
                "subject_id": candidate["subject_id"], "target_id": candidate["target_id"],
                "relationship_type": "讨论同伴", "note": "两人会围绕推导直接指出问题并继续核对。",
                "reason": "公开@与回复", "affinity": 88, "closeness": 77, "confidence": 60,
                "evidence_memory_ids": [evidence],
            }],
        }, {"input_tokens": 1000, "output_tokens": 200})

    def test_cycle_writes_feedback_and_terra_relation_without_qq_outbox(self):
        result = terra_supervision.run_cycle(self.base, self.now, runner=self.runner, budget=self.budget, force=True)
        self.assertEqual(result["status"], "completed")
        self.assertLessEqual(len(self.seen_prompt), 20000)
        self.assertIn("relationship_candidates", self.seen_prompt)
        feedback = self.base / "state" / "dsh_style_feedback.md"
        self.assertIn("少用客服式收尾", feedback.read_text(encoding="utf-8"))
        with self.store.db() as db:
            row = db.execute("SELECT provider,provenance,affinity,closeness,confidence,evidence_record_ids "
                             "FROM social_assessments ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((row["provider"], row["provenance"]), ("terra", "terra_assisted"))
        # Four direct events across two days is enough for numeric values.
        self.assertEqual((row["affinity"], row["closeness"], row["confidence"]), (88, 77, 60))
        report = self.budget.report(self.now)
        self.assertEqual(report["scheduled"]["calls"], 1)
        self.assertEqual(report["scheduled"]["actual"], 1200)
        self.assertFalse((self.base / "state" / "outbox.jsonl").exists())
        again = terra_supervision.run_cycle(self.base, self.now + 60, runner=self.runner,
                                            budget=self.budget)
        self.assertEqual(again["status"], "not_due")

    def test_insufficient_evidence_forces_unknown_scores(self):
        fresh = Path(self.temp.name) / "thin"
        (fresh / "state").mkdir(parents=True)
        thin = store(str(fresh))
        thin.note({"session_key": "group:2", "kind": "chat", "user_id": "303",
                   "text": "404 一起看这个吗", "message_id": "u1", "received_at": self.now - 5000,
                   "direct_targets": ["404"]})
        thin.note({"session_key": "group:2", "kind": "chat", "user_id": "404",
                   "text": "303 好呀", "message_id": "u2", "received_at": self.now - 4900,
                   "direct_targets": ["303"]})
        (fresh / "state" / "sent_messages.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in [
            {"provider": "dsh", "session_key": "group:2", "message_id": "d-thin", "at": self.now - 600,
             "text": "我看看这个。"},
            {"provider": "gemini", "session_key": "group:2", "message_id": "g-thin", "at": self.now - 500,
             "text": "喵，先问清楚条件。"},
        ]), encoding="utf-8")
        with thin.db() as db:
            other = int(db.execute("SELECT id FROM records WHERE origin='u1'").fetchone()[0])
        seen = {}

        def runner(prompt, schema):
            seen["prompt"] = prompt
            return ({
                "style": {"verdict": "normal", "guidance": "口吻正常", "uncertainty": ""},
                "relations": [{"subject_id": "303", "target_id": "404", "relationship_type": "普通群友",
                               "note": "目前只是两次公开搭话，关系待了解。", "reason": "公开@",
                               "affinity": 90, "closeness": 90, "confidence": 90,
                               "evidence_memory_ids": ["M" + str(other)]}],
            }, {"input_tokens": 10, "output_tokens": 5})
        budget = terra_budget.TerraBudget(fresh / "state" / "budget.json")
        terra_supervision.run_cycle(fresh, self.now, runner=runner, budget=budget, force=True)
        with thin.db() as db:
            row = db.execute("SELECT affinity,closeness,confidence,reason FROM social_assessments ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual((row["affinity"], row["closeness"], row["confidence"]), (None, None, None))
        self.assertIn("分数未知", row["reason"])

    def test_failure_preserves_last_feedback_and_consumes_reservation(self):
        feedback = self.base / "state" / "dsh_style_feedback.md"
        feedback.write_text("上一次有效建议\n", encoding="utf-8")

        def failing(prompt, schema):
            raise RuntimeError("model unavailable")
        result = terra_supervision.run_cycle(self.base, self.now, runner=failing,
                                             budget=self.budget, force=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(feedback.read_text(encoding="utf-8"), "上一次有效建议\n")
        report = self.budget.report(self.now)
        self.assertEqual(report["scheduled"]["actual"], terra_budget.BATCH_RESERVE)
        self.assertEqual(report["scheduled"]["calls"], 1)

    def test_no_new_work_does_not_call_model(self):
        empty = Path(self.temp.name) / "empty"
        (empty / "state").mkdir(parents=True)
        budget = terra_budget.TerraBudget(empty / "state" / "budget.json")
        called = []
        result = terra_supervision.run_cycle(empty, self.now,
                                             runner=lambda prompt, schema: called.append(1),
                                             budget=budget, force=True)
        self.assertEqual(result["status"], "skipped")
        self.assertFalse(called)
        self.assertEqual(budget.report(self.now)["scheduled"]["calls"], 0)


class InternalReviewTimeoutTests(unittest.TestCase):
    def test_generate_accepts_a_per_turn_timeout_without_changing_normal_turns(self):
        session = AgySession([], ".", {}, timeout=450)
        with patch.object(session, "start"), patch.object(session, "note"), \
                patch.object(session, "close") as closed, \
                patch("agy_session.time.monotonic", side_effect=[0, 0, 100]):
            session.process = SimpleNamespace(stdin=SimpleNamespace(write=lambda value: None,
                                                                   flush=lambda: None))
            session.selector = SimpleNamespace(select=lambda timeout: [])
            with self.assertRaises(TimeoutError):
                session.generate("prompt", SimpleNamespace(record_progress=lambda token: None),
                                 {"token": "review"}, timeout=0.05)
            closed.assert_called_once_with("error")

    def test_cloud_service_passes_a_bounded_review_timeout_only_for_reviews(self):
        seen = []

        def generate(prompt, coordinator, claim, *, timeout=None):
            seen.append(timeout)
            return {"status": "SUCCESS"}
        with patch.object(cloud_service, "_SESSION",
                          SimpleNamespace(generate=generate)):
            cloud_service.generate("prompt", SimpleNamespace(), {"payload": {}})
            cloud_service.generate("prompt", SimpleNamespace(),
                                   {"payload": {"internal_review": {"review_id": "r"}}})
        self.assertEqual(seen, [None, cloud_service.INTERNAL_REVIEW_TIMEOUT])
        self.assertLessEqual(cloud_service.INTERNAL_REVIEW_TIMEOUT, 90)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix="v5_budget_")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state" / "terra_watch" / "budget.json"
        self.path.parent.mkdir(parents=True)
        self.now = 2_000_000_000.0
        self.budget = terra_budget.TerraBudget(self.path)

    def raw(self, **values):
        value = {"day": terra_budget._day(self.now), "tokens": 0,
                 "style_calls": 0, "fault_calls": 0}
        value.update(values)
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_existing_113040_usage_migrates_without_reset_and_grows(self):
        self.raw(tokens=113040, style_calls=12)
        report = self.budget.report(self.now)
        self.assertEqual(report["used_tokens"], 113040)
        self.assertEqual(report["style_calls"], 12)
        self.assertEqual(report["effective_ceiling"], terra_budget.DAILY_BASE_TOKENS)
        receipt = self.budget.reserve("scheduled", self.now)
        report = self.budget.report(self.now)
        self.assertEqual(report["style_calls"], 13)
        self.assertEqual(report["tokens"], 113040 + terra_budget.TOKEN_RESERVATION)
        report = self.budget.settle(receipt, {"input_tokens": 1000, "output_tokens": 100},
                                    success=True, now=self.now)
        self.assertEqual(report["used_tokens"], 114140)
        self.assertEqual(report["style_calls"], 13)
        self.assertEqual(report["effective_ceiling"], terra_budget.DAILY_BASE_TOKENS)
        self.assertEqual(report["expansions"], 0)

    def test_requested_reserve_grows_by_200k_and_style_keeps_health_reserve(self):
        self.raw(tokens=490000, used_tokens=490000, style_calls=40,
                 ceiling=terra_budget.DAILY_BASE_TOKENS)
        receipt = self.budget.reserve("scheduled", self.now)
        report = self.budget.report(self.now)
        self.assertEqual(report["effective_ceiling"], 800000)
        self.assertEqual(report["expansions"], 1)
        report = self.budget.settle(receipt, {"input_tokens": 100, "output_tokens": 50},
                                    success=True, now=self.now)
        self.assertEqual(report["used_tokens"], 490150)
        # Styles may not reserve into the protected 100k health reserve.
        tokens = terra_budget.DAILY_MAX_TOKENS - terra_budget.HEALTH_RESERVE_TOKENS \
            - terra_budget.TOKEN_RESERVATION + 1
        self.raw(tokens=tokens, used_tokens=tokens, ceiling=terra_budget.DAILY_MAX_TOKENS,
                 expansions=7)
        with self.assertRaises(terra_budget.BudgetExhausted):
            self.budget.reserve("scheduled", self.now)
        # Health may use the whole effective ceiling, not a 60k/360k sub-cap.
        tokens = terra_budget.DAILY_MAX_TOKENS - terra_budget.TOKEN_RESERVATION
        self.raw(tokens=tokens, used_tokens=tokens, ceiling=terra_budget.DAILY_MAX_TOKENS)
        health = self.budget.reserve("health", self.now)
        self.assertEqual(health["amount"], terra_budget.TOKEN_RESERVATION)

    def test_style_and_health_call_limits_and_failed_usage_are_full_charges(self):
        receipts = [self.budget.reserve("scheduled", self.now, estimate=1000)
                    for _ in range(terra_budget.STYLE_CALL_LIMIT)]
        with self.assertRaises(terra_budget.BudgetExhausted):
            self.budget.reserve("scheduled", self.now, estimate=1000)
        report = self.budget.settle(receipts[0], None, success=False, now=self.now)
        self.assertEqual(report["style_calls"], terra_budget.STYLE_CALL_LIMIT)
        self.assertEqual(report["scheduled"]["actual"], 1000)

        health = [self.budget.reserve("health", self.now) for _ in range(terra_budget.FAULT_CALL_LIMIT)]
        with self.assertRaises(terra_budget.BudgetExhausted):
            self.budget.reserve("health", self.now)
        report = self.budget.settle(health[0], {"input_tokens": True, "output_tokens": 5},
                                    success=True, now=self.now)
        self.assertEqual(report["health"]["actual"], terra_budget.TOKEN_RESERVATION)
        self.assertEqual(report["health"]["calls"], terra_budget.FAULT_CALL_LIMIT)

    def test_receipt_settlement_is_idempotent_and_survives_midnight(self):
        first = self.budget.reserve("scheduled", self.now)
        settled = self.budget.settle(first, {"input_tokens": 40, "output_tokens": 2},
                                     success=True, now=self.now)
        self.assertEqual(settled["scheduled"]["actual"], 42)
        again = self.budget.settle(first, {"input_tokens": 999, "output_tokens": 999},
                                   success=True, now=self.now)
        self.assertEqual(again["scheduled"]["actual"], 42)
        self.assertEqual(again["used_tokens"], 42)

        old = self.budget.reserve("scheduled", self.now)
        tomorrow = self.now + 86400
        report = self.budget.settle(old, {"input_tokens": 600, "output_tokens": 100},
                                    success=True, now=tomorrow)
        self.assertEqual(report["day"], terra_budget._day(tomorrow))
        self.assertEqual(report["used_tokens"], 0)
        archived = self.budget.report(self.now)
        self.assertEqual(archived["day"], terra_budget._day(self.now))
        self.assertEqual(archived["used_tokens"], 42 + 700)
        self.assertEqual(archived["scheduled"]["actual"], 42 + 700)


class GraphLayoutTests(unittest.TestCase):
    def test_node_layout_has_center_mass_no_collisions_and_stable_pins(self):
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node unavailable")
        result = subprocess.run([node, str(ROOT / "tests" / "test_graph_layout.js")],
                                cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
