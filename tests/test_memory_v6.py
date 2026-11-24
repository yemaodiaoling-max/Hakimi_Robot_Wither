"""V6 tests: permissive subjective notes and explicit legacy identity links."""
from contextlib import closing, redirect_stdout
import http.client
import io
import json
from pathlib import Path
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

from hakimi.social import identity_links
from hakimi.console import memory_console
from hakimi.memory import memory_lifecycle
from hakimi.social import memory_social
from hakimi.memory import personal_review
from hakimi.prompt import prompt_context
from hakimi.tools import qq_cli
from hakimi.prompt import tool_catalog
from hakimi.bridge import turn_coordinator
from hakimi.memory.memory_store import content_hash, store
from hakimi.transport.official_transport import RouteStore
from _repo import repo_file


class SubjectiveNoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix="v6_a_")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        (self.base / "state").mkdir()
        self.store = store(str(self.base))
        self.store.note({"session_key": "group:1", "kind": "chat", "user_id": "101",
                         "text": "今天也在实验室", "message_id": "m1", "received_at": 1000})

    def test_pure_text_note_without_evidence_preserves_exact_text_and_hash(self):
        note = "  她今天说话有点急。\n不过笑起来很可爱，我喜欢这种坦白。  "
        receipt = memory_social.record_social(
            str(self.base), "group:1", note, "101", subject_id="hakimi",
            affinity=None, closeness=None, evidence_memory_ids=[], now=2000,
        )
        self.assertTrue(receipt["saved"])
        self.assertEqual(receipt["evidence_memory_ids"], [])
        with self.store.db() as db:
            row = db.execute("SELECT text,content_hash FROM memory_originals WHERE record_id=?",
                             (receipt["record_id"],)).fetchone()
        self.assertEqual(row["text"], note)
        self.assertEqual(row["content_hash"], content_hash(note))
        with self.store.db() as db:
            social = db.execute("SELECT * FROM social_assessments WHERE id=?",
                                (receipt["assessment_id"],)).fetchone()
        self.assertEqual(social["impression"], note)
        self.assertEqual(social["evidence_record_ids"], "[]")
        self.assertEqual(social["provenance"], "gemini_manual")
        self.assertIsNone(social["affinity"])
        self.assertIsNone(social["closeness"])

    def test_no_evidence_scores_and_non_gemini_are_rejected(self):
        base = {"affinity": None, "closeness": None, "evidence_memory_ids": []}
        cases = [
            {"affinity": 60},
            {"closeness": 60},
            {"provenance": "dsh_assisted", "provider": "dsh"},
            {"provenance": "terra_assisted", "provider": "terra"},
            {"provider": "dsh"},
            {"subject_id": "101"},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                kwargs = dict(base)
                kwargs.update(extra)
                with self.assertRaises(memory_social.SocialError) as caught:
                    memory_social.record_social(
                        str(self.base), "group:1", "无证据的主观文字", "101",
                        now=2100, **kwargs,
                    )
                self.assertIn(caught.exception.code, {"evidence_required", "invalid_pair"})

    def test_guidance_catalog_and_review_prompt_are_permissive_but_bounded(self):
        guidance = memory_lifecycle.guidance()
        self.assertIn("观察", guidance)
        self.assertIn("吐槽", guidance)
        self.assertIn("喜欢或反感", guidance)
        self.assertIn("不每轮打分", guidance)
        self.assertIn("只有收到成功回执", guidance)
        self.assertIn("不是对方内心", guidance)
        self.assertNotIn("不升级亲密", guidance)
        self.assertNotIn("推断敏感信息", guidance)
        self.assertNotIn("只有理解确实", guidance)

        review = {"session_key": "group:1", "internal_review": {
            "review_id": "r1", "candidates": []}}
        prompt = prompt_context.render_internal_review_prompt(review)
        self.assertIn("无证据的纯文字", prompt)
        self.assertIn("不必等理解发生新变化", prompt)
        self.assertIn("分数可留空", prompt)
        self.assertIn("同一个人，按场合说话", prompt)
        self.assertNotIn("工具与数据边界", prompt)
        self.assertNotIn("只允许下列既定操作", prompt)
        self.assertNotIn("不升级亲密", prompt)
        self.assertNotIn("推断医疗/性/浪漫", prompt)
        self.assertNotIn("证据充分也可写初步认识", prompt)

        social = tool_catalog.BY_NAME["memory.social"]
        text = social["description"] + json.dumps(social, ensure_ascii=False)
        self.assertIn("吐槽", text)
        self.assertIn("无证据", text)
        self.assertIn("分数留空", text)
        self.assertIn("evidence_memory_ids", text)


class InternalReviewCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix="v6_review_")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        (self.base / "state").mkdir()
        self.store = store(str(self.base))
        self.now = time.time()
        for index in range(6):
            self.chat("group:1", "101", f"@哈基米 说好了一起完成实验{index}，这次解决了",
                      f"a{index}", self.now - 1500 * (index + 1), targets=["999"], self_id="999")
        for index in range(4):
            self.chat("group:1", "102", f"@哈基米 谢谢你的整理，第{index}次讨论弄明白了",
                      f"c{index}", self.now - 1700 * (index + 1), targets=["999"], self_id="999")
        self.chat("group:2", "202", "@哈基米 谢谢你帮我整理完成，辛苦了", "b1",
                  self.now - 900, targets=["999"], self_id="999")

    def chat(self, session, user, text, message_id, at, targets=(), reply_to="", self_id=""):
        self.store.note({
            "session_key": session, "kind": "chat", "user_id": str(user), "text": text,
            "message_id": str(message_id), "received_at": float(at),
            "direct_targets": list(targets), "reply_to": str(reply_to),
            "self_id": str(self_id),
        })
        with self.store.db() as db:
            return int(db.execute(
                "SELECT id FROM records WHERE kind='chat' AND origin=? ORDER BY id DESC LIMIT 1",
                (str(message_id),)).fetchone()[0])

    def claim_review(self):
        result = personal_review.tick(self.base, self.now, publish=True)
        self.assertEqual(result["status"], "scheduled")
        coordinator = turn_coordinator.TurnCoordinator(self.base)
        claim = coordinator.claim_primary()
        self.assertIsNotNone(claim)
        from hakimi.prompt.prompt_context import activate_context
        activate_context(coordinator.snapshot()["payload"], self.base, claim=claim, now=self.now)
        return coordinator, claim, coordinator.snapshot()["payload"]["internal_review"]

    def call_social(self, args, claim):
        base = SimpleNamespace(
            session=args["session"], turn=claim["token"], call_name="memory.social",
            note=args.get("note"), target_id=args.get("target_id"),
            subject_id=args.get("subject_id", "hakimi"),
            relationship_type=args.get("relationship_type", "关系印象"),
            affinity=args.get("affinity"), closeness=args.get("closeness"),
            confidence=args.get("confidence"), impact=args.get("impact"),
            reason=args.get("reason"), evidence_memory_ids=args.get("evidence_memory_ids"),
        )
        with patch.multiple(qq_cli, BASE_DIR=self.base, STATE_DIR=self.base / "state"):
            with redirect_stdout(io.StringIO()):
                qq_cli.cmd_call(base)

    def test_candidate_may_write_pure_text_without_evidence(self):
        coordinator, claim, review = self.claim_review()
        candidate = review["candidates"][0]
        note = " 她今天讲实验时很认真。\n我还是有点想逗她。  "
        self.call_social({
            "session": review["group"], "target_id": candidate["person_id"],
            "note": note, "affinity": None, "closeness": None,
            "evidence_memory_ids": None,
        }, claim)
        with self.store.db() as db:
            row = db.execute(
                "SELECT impression,evidence_record_ids FROM social_assessments "
                "WHERE target_id=? ORDER BY id DESC LIMIT 1",
                (candidate["person_id"],)).fetchone()
        self.assertEqual(row["impression"], note)
        self.assertEqual(row["evidence_record_ids"], "[]")

    def test_evidence_must_belong_to_the_selected_candidate(self):
        coordinator, claim, review = self.claim_review()
        self.assertGreaterEqual(len(review["candidates"]), 2)
        first, second = review["candidates"][0], review["candidates"][1]
        foreign = second["evidence_memory_ids"][0]
        with self.assertRaises(qq_cli.ToolError):
            self.call_social({
                "session": review["group"], "target_id": first["person_id"],
                "note": "借用别人的证据", "affinity": None, "closeness": None,
                "evidence_memory_ids": foreign,
            }, claim)
        own = first["evidence_memory_ids"][0]
        self.call_social({
            "session": review["group"], "target_id": first["person_id"],
            "note": "用自己的证据写一句", "affinity": None, "closeness": None,
            "evidence_memory_ids": own,
        }, claim)
        coordinator.complete(claim["token"])

    def test_internal_review_deadline_is_80_seconds_not_one(self):
        coordinator, claim, _review = self.claim_review()
        self.assertGreaterEqual(turn_coordinator.INTERNAL_REVIEW_TTL_SECONDS, 80)
        self.assertGreaterEqual(claim["deadline"] - self.now, 79)
        self.assertLessEqual(claim["deadline"] - self.now, 90)
        coordinator.complete(claim["token"])

    def test_social_append_and_auto_evidence_scoring_and_closeness_exposure(self):
        coordinator, claim, review = self.claim_review()
        candidate = review["candidates"][0]
        person_id = candidate["person_id"]
        # 1) Full write with scores but without explicit evidence_memory_ids -> auto-binds candidate evidence
        full_note = "平时爱聊实验细节，说话爽快直接，是个靠谱的研究搭子"
        base_call = SimpleNamespace(
            session=review["group"], turn=claim["token"], call_name="memory.social",
            note=full_note, target_id=person_id, subject_id="hakimi",
            relationship_type="实验搭子", affinity=62, closeness=68, confidence=75,
            impact="ordinary", reason="多次一起讨论实验", evidence_memory_ids=None, mode=None,
        )
        with patch.multiple(qq_cli, BASE_DIR=self.base, STATE_DIR=self.base / "state"):
            with redirect_stdout(io.StringIO()):
                first_receipt = qq_cli.cmd_call(base_call)
        self.assertTrue(first_receipt["saved"])
        self.assertEqual(first_receipt["affinity"], 62)
        self.assertEqual(first_receipt["closeness"], 68)
        self.assertEqual(first_receipt["confidence"], 75)
        self.assertTrue(first_receipt["evidence_memory_ids"])

        # 2) Append a casual 1-line extra impression via memory.social_append (and tweak closeness)
        extra_note = "今天还顺手帮我核对了数据表格，挺细心的。"
        append_call = SimpleNamespace(
            session=review["group"], turn=claim["token"], call_name="memory.social_append",
            note=extra_note, target_id=person_id, subject_id="hakimi",
            relationship_type=None, affinity=None, closeness=72, confidence=78,
            impact="ordinary", reason="顺手帮忙核对数据", evidence_memory_ids=None,
        )
        with patch.multiple(qq_cli, BASE_DIR=self.base, STATE_DIR=self.base / "state"):
            with redirect_stdout(io.StringIO()):
                second_receipt = qq_cli.cmd_call(append_call)
        self.assertTrue(second_receipt["saved"])
        self.assertEqual(second_receipt["mode"], "append")
        self.assertEqual(second_receipt["relationship_type"], "实验搭子")
        self.assertEqual(second_receipt["affinity"], 62)
        self.assertEqual(second_receipt["closeness"], 72)
        self.assertEqual(second_receipt["confidence"], 78)
        with self.store.db() as db:
            rows = db.execute(
                "SELECT id,state,impression,relationship_type,affinity,closeness,confidence "
                "FROM social_assessments WHERE target_id=? ORDER BY id ASC",
                (person_id,),
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["state"], "superseded")
        self.assertEqual(rows[1]["state"], "active")
        self.assertEqual(rows[1]["impression"], full_note + "。" + extra_note)
        self.assertEqual(rows[1]["relationship_type"], "实验搭子")

        # 3) Verify previous_scores exposes closeness in social_opportunity & select_review
        self.chat("group:1", person_id, "@哈基米 新一轮实验结果出来了", "fresh-after-append",
                   time.time() + 10, targets=["999"], self_id="999")
        cue = memory_lifecycle.social_opportunity(
            "group:1", [], {"user_id": person_id, "text": "@哈基米 新一轮实验结果出来了",
                            "message_id": "fresh-after-append"},
            self.base, time.time() + 11,
        )
        self.assertIsNotNone(cue)
        self.assertEqual(cue["previous_scores"]["closeness"], 72)
        self.assertEqual(cue["previous_scores"]["affinity"], 62)
        coordinator.complete(claim["token"])


class IdentityFixture:
    def __init__(self, base):
        self.base = Path(base)
        self.runtime = self.base / "official_test_runtime"
        (self.runtime / "state").mkdir(parents=True, exist_ok=True)
        self.old_store = store(str(self.base))
        self.new_store = store(str(self.runtime))
        self._old_notes()
        self._runtime_notes()
        self._sources()

    def _old_notes(self):
        def chat(session, user, nickname, text, mid, at):
            self.old_store.note({
                "session_key": session, "kind": "chat", "user_id": str(user),
                "nickname": nickname, "text": text, "message_id": mid,
                "received_at": float(at),
            })
            with self.old_store.db() as db:
                return int(db.execute(
                    "SELECT id FROM records WHERE kind='chat' AND origin=? ORDER BY id DESC LIMIT 1",
                    (mid,)).fetchone()[0])

        self.old_chat_id = chat("group:111", "777", "旧阿七", "旧群的声学实验做完了", "old-1", 1000)
        chat("group:111", "888", "旧阿八", "旧群聊天里的阿八", "old-2", 1001)
        self.old_group2_chat_id = chat("group:222", "999", "旧群二成员", "旧群二的安静讨论", "old-3", 1002)
        self.old_same_number_id = chat("group:9001", "7001", "旧同号成员", "旧群里数字也是9001", "old-4", 1003)
        self.old_private_id = chat("private:777", "777", "旧阿七", "这是私聊秘密", "old-p", 1004)
        self.person_note = " 旧笔记：阿七对声学很认真。\n第二行保留空格  "
        self.person_note_id = self.old_store.remember_manual(
            {"kind": "person", "user_id": "777", "note": self.person_note,
             "key": "印象·旧阿七"},
            session="group:111", provider="gemini", channel="gemini_tool", now=1010,
        )["record_id"]
        self.group_note = "旧群偏好：讨论声学时保持安静，也不要刷屏。"
        self.group_note_id = self.old_store.remember_manual(
            {"kind": "preference", "note": self.group_note, "key": "群规"},
            session="group:111", provider="gemini", channel="gemini_tool", now=1011,
        )["record_id"]

    def _runtime_notes(self):
        self.new_store.note({"session_key": "group:9001", "kind": "chat", "user_id": "5001",
                             "nickname": "新阿七", "text": "本群的声学实验进度",
                             "message_id": "new-1", "received_at": 2000})
        self.new_store.note({"session_key": "group:9001", "kind": "chat", "user_id": "5002",
                             "nickname": "新阿八", "text": "本群的日常聊天",
                             "message_id": "new-2", "received_at": 2001})
        self.new_store.note({"session_key": "group:9002", "kind": "chat", "user_id": "5003",
                             "nickname": "新群二成员", "text": "新群二的讨论",
                             "message_id": "new-3", "received_at": 2002})
        self.new_store.note({"session_key": "private:5001", "kind": "chat", "user_id": "5001",
                             "text": "当前私聊", "message_id": "new-p", "received_at": 2003})
        self.new_store.remember_manual(
            {"kind": "person", "user_id": "5001",
             "note": "本群笔记：声学实验要慢慢来。", "key": "本群印象"},
            session="group:9001", provider="gemini", channel="gemini_tool", now=2010,
        )

    def _sources(self):
        (self.base / "config.json").write_text(json.dumps({
            "group_weights": {
                "111": {"name": "旧声学群"}, "222": {"name": "旧群二"},
                "9001": {"name": "旧同号群"},
            },
            "user_bias": {"users": {"9999": {"nickname": "旧手填昵称"}}},
        }, ensure_ascii=False), encoding="utf-8")
        (self.base / "group_profiles.json").write_text(json.dumps({
            "groups": {"111": {"profile": "study_calm"}, "333": {"profile": "quiet_default"}}
        }, ensure_ascii=False), encoding="utf-8")
        (self.base / "official_memory_review.json").write_text(json.dumps({
            "source_group": "111",
            "group_notes": [{"note": "旧摘要：这个群以前约定过安静讨论。"}],
            "pending_members": [{
                "old_qq": "888", "old_nickname": "旧阿八",
                "notes": ["旧摘要：阿八以前常来问作业。"],
            }],
            "group_reviews": {
                "222": {"group_notes": [{"note": "旧摘要：二群是闲聊群。"}],
                        "pending_members": []},
            },
        }, ensure_ascii=False), encoding="utf-8")
        (self.base / "memory.md").write_text(
            "# QQ 群友记忆与长期印象库\n"
            "## 核心群聊备忘\n"
            "- **`111` (旧声学群)**：\n"
            "  - 群友 `777` (**旧阿七**)：旧版手记：阿七以前也在这里讨论声学实验。\n"
            "- **`222` (旧群二)**：\n"
            "  - 群友 `999` (**旧群二成员**)：旧版手记：二群的安静讨论。\n"
            "## 私人会话\n"
            "  - 群友 `817740` (**私聊**)：这条旧私聊绝不能读出。\n",
            encoding="utf-8")
        from hakimi.memory.legacy_md_notes import read_notes as read_md_notes
        self.md_note_id = next(
            note["id"] for note in read_md_notes(self.base)
            if note["group_id"] == "111" and note["user_id"] == "777")
        routes = RouteStore(self.runtime / "state/official_routes.sqlite3")
        routes.set("group_routes", {
            "A": {"local_id": "9001", "name": "新声学群", "openid": "SECRET_GROUP_OPENID", "active": True},
            "B": {"local_id": "9002", "name": "新群二", "openid": "SECRET_GROUP_OPENID_2", "active": True},
        })
        with routes.db() as db:
            db.execute("INSERT OR REPLACE INTO actors VALUES ('5004','ACTOR_ONLY_OPENID')")

    def link_group(self, old="111", new="9001"):
        rev = identity_links.revision(self.runtime)
        result = identity_links.apply(self.runtime, {
            "action": "link_group", "old_group": old, "new_group": new,
            "expected_revision": rev, "confirm": True,
        })
        return result

    def link_member(self, old="777", new="5001", old_group="111", new_group="9001"):
        rev = identity_links.revision(self.runtime)
        result = identity_links.apply(self.runtime, {
            "action": "link_member", "old_group": old_group, "new_group": new_group,
            "old_user": old, "new_user": new, "expected_revision": rev, "confirm": True,
        })
        return result

    def linked(self):
        self.link_group()
        self.link_member()
        return identity_links.revision(self.runtime)


class IdentityCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix="v6_id_")
        self.addCleanup(self.temp.cleanup)
        self.fx = IdentityFixture(self.temp.name)

    def test_catalog_merges_sources_and_never_auto_matches_equal_numbers(self):
        data = identity_links.catalog(self.fx.runtime)
        old_ids = {item["id"] for item in data["old_groups"]}
        self.assertTrue({"111", "222", "333", "9001"} <= old_ids)
        self.assertEqual(data["old_root"]["id"], identity_links._old_root_id(self.fx.runtime))
        old_members = {item["old_user"]: item for item in
                       identity_links.old_members_catalog(self.fx.runtime, "111")}
        self.assertIn("777", old_members)
        self.assertIn("888", old_members)
        self.assertTrue(old_members["888"]["has_notes"])
        self.assertIn("memory_md", old_members["777"]["sources"])
        md_group = next(item for item in data["old_groups"] if item["id"] == "111")
        self.assertIn("memory_md", md_group["sources"])
        self.assertGreaterEqual(md_group["handwritten_notes"], 1)
        new_groups = {item["id"]: item for item in data["new_groups"]}
        self.assertIn("9001", new_groups)
        self.assertEqual(new_groups["9001"]["name"], "新声学群")
        self.assertEqual(data["counts"]["group_links"], 0)
        self.assertEqual(data["revision"], 0)
        # Same numeric group/user on both sides is not a link and is not inferred.
        self.assertEqual(identity_links.links_payload(self.fx.runtime)["group_links"], [])

    def test_get_catalog_links_and_preview_do_not_create_mapping_tables(self):
        path = self.fx.runtime / "state/memory.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            before = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        identity_links.catalog(self.fx.runtime)
        identity_links.links_payload(self.fx.runtime)
        identity_links.preview(self.fx.runtime, {
            "action": "link_group", "old_group": "111", "new_group": "9001"})
        with closing(sqlite3.connect(path)) as db:
            after = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(before, after)
        self.assertFalse({name for name in after if name.startswith("identity_")})
        self.assertEqual(identity_links.revision(self.fx.runtime), 0)

    def test_new_member_must_be_observed_in_the_selected_group(self):
        members = identity_links.new_members_catalog(self.fx.runtime, "9001")
        self.assertNotIn("5004", {item["new_user"] for item in members})
        self.fx.link_group()
        # Actor-only id (5004) is not proof of membership in 9001.
        actor_only = identity_links.preview(self.fx.runtime, {
            "action": "link_member", "old_group": "111", "new_group": "9001",
            "old_user": "777", "new_user": "5004",
        })
        self.assertFalse(actor_only["valid"], actor_only)
        self.assertIn("new_member_not_observed",
                      {item["code"] for item in actor_only["conflicts"]})
        # A real member of another group (5003 in 9002) is also not proof here.
        other_group = identity_links.preview(self.fx.runtime, {
            "action": "link_member", "old_group": "111", "new_group": "9001",
            "old_user": "777", "new_user": "5003",
        })
        self.assertFalse(other_group["valid"], other_group)
        self.assertIn("new_member_not_observed",
                      {item["code"] for item in other_group["conflicts"]})
        # The actual member of this selected group is accepted.
        selected_group = identity_links.preview(self.fx.runtime, {
            "action": "link_member", "old_group": "111", "new_group": "9001",
            "old_user": "777", "new_user": "5001",
        })
        self.assertTrue(selected_group["valid"], selected_group)
        self.assertTrue(selected_group["new_member_observed_in_group"])


class IdentityApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix="v6_apply_")
        self.addCleanup(self.temp.cleanup)
        self.fx = IdentityFixture(self.temp.name)

    def test_preview_apply_revision_conflict_unlink_and_rebind_restore(self):
        preview = identity_links.preview(self.fx.runtime, {
            "action": "link_group", "old_group": "111", "new_group": "9001"})
        self.assertTrue(preview["valid"])
        self.assertEqual(preview["old_notes"]["count"], 3)
        self.assertTrue(any(item["memory_id"].upper().startswith("LM")
                            for item in preview["old_notes"]["preview"]))
        self.fx.link_group()
        with self.assertRaises(identity_links.IdentityError) as stale:
            identity_links.apply(self.fx.runtime, {
                "action": "link_group", "old_group": "111", "new_group": "9001",
                "expected_revision": 0, "confirm": True,
            })
        self.assertEqual(stale.exception.status, 409)
        duplicate = identity_links.apply(self.fx.runtime, {
            "action": "link_group", "old_group": "111", "new_group": "9001",
            "expected_revision": 1, "confirm": True,
        })
        self.assertFalse(duplicate["changed"])
        self.fx.link_member()
        legacy_id = "L" + str(self.fx.person_note_id)
        record = identity_links.read_legacy(self.fx.runtime, legacy_id, "group:9001")
        self.assertIsNotNone(record)
        self.assertIn("旧群111→新群9001", record["identity_label"])
        self.assertIn("旧QQ777→本地ID 5001", record["identity_label"])
        self.assertEqual(record["origin_label"], "历史原文")
        self.assertEqual(record["hash"], content_hash(self.fx.person_note))
        unlink = identity_links.preview(self.fx.runtime, {
            "action": "unlink_group", "old_group": "111", "new_group": "9001"})
        self.assertEqual(unlink["member_links_to_invalidate"], 1)
        identity_links.apply(self.fx.runtime, {
            "action": "unlink_group", "old_group": "111", "new_group": "9001",
            "expected_revision": 2, "confirm": True,
        })
        self.assertIsNone(identity_links.read_legacy(self.fx.runtime, legacy_id, "group:9001"))
        rebind = identity_links.apply(self.fx.runtime, {
            "action": "link_group", "old_group": "111", "new_group": "9001",
            "expected_revision": 3, "confirm": True,
        })
        self.assertEqual(rebind["restored_members"], 1)
        self.assertIsNotNone(identity_links.read_legacy(self.fx.runtime, legacy_id, "group:9001"))

    def test_group_and_member_conflicts_and_cross_group_guards(self):
        self.fx.link_group()
        self.fx.link_group("222", "9002")
        other = identity_links.preview(self.fx.runtime, {
            "action": "link_group", "old_group": "111", "new_group": "9002"})
        self.assertIn("old_group_conflict", {item["code"] for item in other["conflicts"]})
        other = identity_links.preview(self.fx.runtime, {
            "action": "link_group", "old_group": "222", "new_group": "9001"})
        self.assertIn("new_group_conflict", {item["code"] for item in other["conflicts"]})
        self.fx.link_member("777", "5001", "111", "9001")
        cross_old = identity_links.preview(self.fx.runtime, {
            "action": "link_member", "old_group": "222", "new_group": "9002",
            "old_user": "777", "new_user": "5003"})
        self.assertIn("cross_group_old_conflict", {item["code"] for item in cross_old["conflicts"]})
        cross_new = identity_links.preview(self.fx.runtime, {
            "action": "link_member", "old_group": "222", "new_group": "9002",
            "old_user": "999", "new_user": "5001"})
        self.assertIn("cross_group_new_conflict", {item["code"] for item in cross_new["conflicts"]})

    def test_manual_old_qq_without_notes_can_be_saved_and_is_labelled(self):
        self.fx.link_group()
        preview = identity_links.preview(self.fx.runtime, {
            "action": "link_member", "old_group": "111", "new_group": "9001",
            "old_user": "9999", "new_user": "5002"})
        self.assertTrue(preview["valid"], preview)
        self.assertFalse(preview["old_member_known"])
        self.assertTrue(preview["old_notes"]["no_old_notes"])
        self.assertEqual(preview["old_nickname"], "旧手填昵称")
        result = self.fx.link_member(old="9999", new="5002")
        self.assertTrue(result["changed"])
        self.assertTrue(result["detail"]["no_old_notes"])

    def test_unknown_new_member_is_rejected(self):
        self.fx.link_group()
        preview = identity_links.preview(self.fx.runtime, {
            "action": "link_member", "old_group": "111", "new_group": "9001",
            "old_user": "777", "new_user": "5999"})
        self.assertIn("new_member_not_observed", {item["code"] for item in preview["conflicts"]})

    def test_member_unlink_hides_person_note_and_rebind_restores_it(self):
        self.fx.linked()
        legacy_id = "L" + str(self.fx.person_note_id)
        self.assertIsNotNone(identity_links.read_legacy(self.fx.runtime, legacy_id, "group:9001"))
        identity_links.apply(self.fx.runtime, {
            "action": "unlink_member", "old_group": "111", "new_group": "9001",
            "old_user": "777", "new_user": "5001", "expected_revision": 2, "confirm": True,
        })
        self.assertIsNone(identity_links.read_legacy(self.fx.runtime, legacy_id, "group:9001"))
        self.fx.link_member()
        self.assertIsNotNone(identity_links.read_legacy(self.fx.runtime, legacy_id, "group:9001"))

    def test_review_summary_ids_are_fixed_and_resolve_through_mapping(self):
        self.fx.linked()
        group_id = "R111-g0"
        note = identity_links.read_legacy(self.fx.runtime, group_id, "group:9001")
        self.assertIsNotNone(note)
        self.assertEqual(note["origin_label"], "旧摘要")
        self.assertIn("旧摘要：这个群以前约定过安静讨论。", note["text"])
        member_note = identity_links.read_legacy(self.fx.runtime, "R111-m888-0", "group:9001")
        self.assertIsNone(member_note)  # member 888 not mapped yet
        self.fx.link_member("888", "5002")
        member_note = identity_links.read_legacy(self.fx.runtime, "R111-m888-0", "group:9001")
        self.assertIsNotNone(member_note)
        self.assertEqual(member_note["origin_label"], "旧摘要")
        self.assertIn("旧QQ888→本地ID 5002", member_note["identity_label"])


class IdentityRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix="v6_retr_")
        self.addCleanup(self.temp.cleanup)
        self.fx = IdentityFixture(self.temp.name)
        self.fx.linked()

    def test_retrieve_adds_focus_legacy_notes_within_budget_and_hides_after_unlink(self):
        messages = [{"session_key": "group:9001", "user_id": "5001",
                     "text": "本群的声学实验进度"}]
        target = {"user_id": "5001", "text": "声学实验"}
        result = self.fx.new_store.retrieve(
            "group:9001", messages, target, query="声学实验",
            limit=6, budget=1200, explain=True)
        legacy = [line for line in result if line.startswith("[L") or line.startswith("[R")]
        self.assertTrue(legacy)
        self.assertLessEqual(len(legacy), 2)
        self.assertIn("历史原文", legacy[0])
        self.assertIn("旧群111→新群9001", legacy[0])
        self.assertIn("人物已匹配", legacy[0])
        full = identity_links.read_legacy(
            self.fx.runtime, "L" + str(self.fx.person_note_id), "group:9001", limit=6000)
        self.assertIn("旧QQ777→本地ID 5001", full["identity_label"])
        self.assertTrue(any(not line.startswith("[L") and not line.startswith("[R")
                            for line in result))
        identity_links.apply(self.fx.runtime, {
            "action": "unlink_group", "old_group": "111", "new_group": "9001",
            "expected_revision": identity_links.revision(self.fx.runtime), "confirm": True,
        })
        after = self.fx.new_store.retrieve(
            "group:9001", messages, target, query="声学实验",
            limit=6, budget=1200, explain=True)
        self.assertFalse(any(line.startswith("[L") or line.startswith("[R") for line in after))

    def test_explicit_query_reserves_legacy_slot_even_with_popular_current_rows(self):
        for index in range(10):
            self.fx.new_store.note({
                "session_key": "group:9001", "kind": "chat", "user_id": "5002",
                "nickname": "新阿八", "text": f"当前热门声学实验记录{index}",
                "message_id": f"popular-{index}", "received_at": 5000 + index,
            })
        # The explicit memory.search query differs from the current target text.
        messages = [{"session_key": "group:9001", "user_id": "5001", "text": "今天天气不错"}]
        target = {"user_id": "5001", "text": "今天天气不错"}
        result = self.fx.new_store.retrieve(
            "group:9001", messages, target, query="声学实验 旧阿七",
            limit=3, budget=700, explain=True)
        legacy = [line for line in result if line.startswith(("[L", "[R", "[LM"))]
        self.assertTrue(legacy, result)
        self.assertLessEqual(len(result), 3)
        self.assertTrue(any(not line.startswith(("[L", "[R", "[LM")) for line in result))
        # Without the explicit query the old note is not dragged in by the
        # unrelated current target text.
        plain = self.fx.new_store.retrieve(
            "group:9001", messages, target, query="今天天气不错",
            limit=3, budget=700, explain=True)
        self.assertFalse(any(line.startswith(("[L", "[R", "[LM")) for line in plain))

    def test_private_cross_groups_reads_mapped_public_legacy_only(self):
        legacy_id = "L" + str(self.fx.person_note_id)
        private = "private:5001"
        self.assertIsNone(self.fx.new_store.read(legacy_id, private, {}))
        self.assertIsNotNone(self.fx.new_store.read(
            legacy_id, private, {}, cross_groups=True))
        self.assertIsNone(self.fx.new_store.read(
            "L" + str(self.fx.old_private_id), private, {}, cross_groups=True))
        # Default private scope never auto-loads old-group notes...
        self.assertEqual(identity_links.retrieve_legacy_notes(
            self.fx.runtime, private, "5001", [], query="声学实验"), [])
        # ...but an explicit all-groups scope may read mapped public old groups.
        cross = identity_links.retrieve_legacy_notes(
            self.fx.runtime, private, "5001", [], query="声学实验", cross_groups=True)
        self.assertTrue(cross)
        self.assertTrue(cross[0].startswith("[L") or cross[0].startswith("[R")
                        or cross[0].startswith("[LM"))

    def test_read_legacy_obeys_group_member_and_private_boundaries(self):
        legacy_id = "L" + str(self.fx.person_note_id)
        self.assertIsNotNone(self.fx.new_store.read(legacy_id, "group:9001", {}))
        self.assertIsNone(self.fx.new_store.read(legacy_id, "group:9002", {}))
        self.assertIsNone(self.fx.new_store.read(legacy_id, "private:5001", {}))
        self.assertIsNone(self.fx.new_store.read(
            "L" + str(self.fx.old_private_id), "group:9001", {}))
        self.assertIsNone(self.fx.new_store.read(
            "L" + str(self.fx.old_same_number_id), "group:9001", {}))
        self.fx.link_group("222", "9002")
        self.fx.link_member("999", "5003", "222", "9002")
        cross_id = "L" + str(self.fx.old_group2_chat_id)
        self.assertIsNone(self.fx.new_store.read(cross_id, "group:9001", {}))
        self.assertIsNotNone(self.fx.new_store.read(cross_id, "group:9001", {},
                                                    cross_groups=True))

    def test_original_text_and_historical_scores_are_not_rewritten(self):
        with self.fx.old_store.db() as db:
            before_record = dict(db.execute(
                "SELECT text,content_hash FROM records WHERE id=?",
                (self.fx.person_note_id,)).fetchone())
            before_original = dict(db.execute(
                "SELECT text,content_hash FROM memory_originals WHERE record_id=? ORDER BY id DESC LIMIT 1",
                (self.fx.person_note_id,)).fetchone())
        from hakimi.social.social_behavior import behavior_for
        before_behavior = behavior_for(self.fx.runtime, "5001", "group:9001", now=3000)
        self.fx.new_store.retrieve(
            "group:9001", [{"session_key": "group:9001", "user_id": "5001",
                            "text": "声学实验"}],
            {"user_id": "5001", "text": "声学实验"}, query="声学实验",
            limit=6, budget=1200)
        after_behavior = behavior_for(self.fx.runtime, "5001", "group:9001", now=3000)
        self.assertEqual(before_behavior, after_behavior)
        with self.fx.old_store.db() as db:
            after_record = dict(db.execute(
                "SELECT text,content_hash FROM records WHERE id=?",
                (self.fx.person_note_id,)).fetchone())
            after_original = dict(db.execute(
                "SELECT text,content_hash FROM memory_originals WHERE record_id=? ORDER BY id DESC LIMIT 1",
                (self.fx.person_note_id,)).fetchone())
        self.assertEqual(before_record, after_record)
        self.assertEqual(before_original, after_original)
        self.assertEqual(after_original["text"], self.fx.person_note)
        self.assertEqual(after_original["content_hash"], content_hash(self.fx.person_note))

    def test_person_payload_and_memory_payload_use_legacy_reader(self):
        payload = memory_console.person_payload(
            self.fx.runtime, {"id": "5001", "group": "group:9001"})
        history = payload["identity_history"]
        self.assertTrue(history["available"])
        self.assertEqual(history["links"][0]["old_group"], "111")
        self.assertEqual(history["links"][0]["new_group"], "9001")
        self.assertTrue(any(note["memory_id"].upper().startswith("LM")
                            for note in history["links"][0]["notes"]))
        note_id = history["links"][0]["notes"][0]["memory_id"]
        memory = memory_console.memory_payload(
            self.fx.runtime, {"id": note_id, "group": "9001", "limit": 6000})
        self.assertEqual(memory["id"], note_id)
        self.assertEqual(memory["origin_label"], "历史原文")
        self.assertIn("旧群111→新群9001", memory["identity_label"])
        identity_links.apply(self.fx.runtime, {
            "action": "unlink_group", "old_group": "111", "new_group": "9001",
            "expected_revision": identity_links.revision(self.fx.runtime), "confirm": True,
        })
        with self.assertRaises(memory_console.ApiError) as missing:
            memory_console.memory_payload(self.fx.runtime, {"id": note_id, "group": "9001"})
        self.assertEqual(missing.exception.status, 404)
        after = memory_console.person_payload(
            self.fx.runtime, {"id": "5001", "group": "group:9001"})
        self.assertFalse(after["identity_history"]["available"])


class OperatorLegacyReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix="v6_admin_")
        self.addCleanup(self.temp.cleanup)
        self.fx = IdentityFixture(self.temp.name)

    def test_operator_reads_before_mapping_model_does_not_and_unlink_hides_model(self):
        legacy_id = "L" + str(self.fx.person_note_id)
        admin = memory_console.identity_memory_payload(
            self.fx.runtime, {"id": legacy_id, "limit": 6000})
        self.assertEqual(admin["text"], self.fx.person_note)
        self.assertEqual(admin["origin_label"], "历史原文")
        self.assertIn("尚未确认匹配", admin["identity_label"])
        self.assertIsNone(self.fx.new_store.read(legacy_id, "group:9001", {}))

        md_admin = memory_console.identity_memory_payload(
            self.fx.runtime, {"id": self.fx.md_note_id, "limit": 6000})
        self.assertIn("旧版手记", md_admin["origin_label"])
        self.assertTrue(md_admin["hash"])
        self.assertIsNone(self.fx.new_store.read(self.fx.md_note_id, "group:9001", {}))

        review_admin = identity_links.read_for_operator(
            self.fx.runtime, "R111-g0", limit=6000)
        self.assertEqual(review_admin["origin_label"], "旧摘要")
        self.assertIn("旧摘要：这个群以前约定过安静讨论。", review_admin["text"])
        review_member = identity_links.read_for_operator(
            self.fx.runtime, "R111-m888-0", limit=6000)
        self.assertIn("阿八以前常来问作业", review_member["text"])
        self.assertIsNone(self.fx.new_store.read("R111-m888-0", "group:9001", {}))

        # Fixed ids only: no path/SQL input, and old private records stay hidden.
        self.assertIsNone(identity_links.read_for_operator(
            self.fx.runtime, "../../memory.md"))
        self.assertIsNone(identity_links.read_for_operator(
            self.fx.runtime, "L" + str(self.fx.old_private_id)))

        self.fx.link_group()
        self.assertIsNone(self.fx.new_store.read(legacy_id, "group:9001", {}))
        self.fx.link_member()
        self.assertIsNotNone(self.fx.new_store.read(legacy_id, "group:9001", {}))
        md_model = self.fx.new_store.read(self.fx.md_note_id, "group:9001", {})
        self.assertIsNotNone(md_model)
        self.assertEqual(md_model["hash"], md_admin["hash"])
        self.assertIn("旧版手记", md_model["origin_label"])

        identity_links.apply(self.fx.runtime, {
            "action": "unlink_group", "old_group": "111", "new_group": "9001",
            "expected_revision": identity_links.revision(self.fx.runtime), "confirm": True,
        })
        self.assertIsNone(self.fx.new_store.read(legacy_id, "group:9001", {}))
        again = memory_console.identity_memory_payload(
            self.fx.runtime, {"id": legacy_id, "limit": 6000})
        self.assertEqual(again["text"], self.fx.person_note)

    def test_operator_pagination_is_bounded_and_exact(self):
        note_id = "L" + str(self.fx.person_note_id)
        first = identity_links.read_for_operator(self.fx.runtime, note_id, offset=0, limit=20)
        second = identity_links.read_for_operator(
            self.fx.runtime, note_id, offset=first["next_offset"], limit=6000)
        self.assertEqual(first["text"] + second["text"], self.fx.person_note)
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])


class OldSchemaCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix="v6_oldschema_")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.runtime = self.base / "official_test_runtime"
        (self.base / "state").mkdir(parents=True)
        (self.runtime / "state").mkdir(parents=True)
        self.new_store = store(str(self.runtime))
        self.new_store.note({"session_key": "group:9001", "kind": "chat", "user_id": "5001",
                             "nickname": "新阿七", "text": "本群声学实验", "message_id": "new-1",
                             "received_at": 2000})
        # The real legacy database has only these 13 columns: no
        # memory_originals, state, nickname, shared or content_hash.
        with closing(sqlite3.connect(self.base / "state/memory.sqlite3")) as db:
            db.execute(
                "CREATE TABLE records (id INTEGER PRIMARY KEY,source_key TEXT,kind TEXT,"
                "session TEXT,user_id TEXT,key TEXT,text TEXT,at REAL,expires REAL,"
                "origin TEXT,source TEXT,summary TEXT,vector BLOB)")
            cursor = db.execute(
                "INSERT INTO records(source_key,kind,session,user_id,key,text,at,expires,origin,source,summary) "
                "VALUES ('legacy-person','person','group:111','777','印象',?,1010,0,'m1','gemini','旧的摘要')",
                (self.person_text,))
            self.person_record_id = int(cursor.lastrowid)
            db.execute(
                "INSERT INTO records(source_key,kind,session,user_id,key,text,at,expires,origin,source,summary) "
                "VALUES ('legacy-group','preference','group:111','','群规',?,1011,0,'m2','gemini','旧群规摘要')",
                ("旧库兼容：本群讨论声学时保持安静。",))
            db.execute(
                "INSERT INTO records(source_key,kind,session,user_id,key,text,at,expires,origin,source,summary) "
                "VALUES ('legacy-private','person','private:777','777','私聊',?,1012,0,'m3','gemini','')",
                ("旧私聊秘密",))
            db.commit()

    person_text = " 旧库兼容：阿七的声学笔记\n第二行保留空格  "

    def test_legacy_database_without_new_columns_is_read_only_compatible(self):
        groups = {item["id"]: item for item in identity_links.old_groups_catalog(self.runtime)}
        self.assertIn("111", groups)
        self.assertGreaterEqual(groups["111"]["records"], 2)
        members = {item["old_user"]: item for item in
                   identity_links.old_members_catalog(self.runtime, "111")}
        self.assertIn("777", members)
        self.assertGreaterEqual(members["777"]["notes"], 1)
        preview = identity_links.preview(self.runtime, {
            "action": "link_group", "old_group": "111", "new_group": "9001"})
        self.assertTrue(preview["valid"], preview)
        self.assertGreaterEqual(preview["old_notes"]["count"], 1)

        legacy_id = "L" + str(self.person_record_id)
        admin = identity_links.read_for_operator(self.runtime, legacy_id, limit=6000)
        self.assertEqual(admin["text"], self.person_text)
        self.assertEqual(admin["hash"], content_hash(self.person_text))
        identity_links.apply(self.runtime, {
            "action": "link_group", "old_group": "111", "new_group": "9001",
            "expected_revision": 0, "confirm": True,
        })
        identity_links.apply(self.runtime, {
            "action": "link_member", "old_group": "111", "new_group": "9001",
            "old_user": "777", "new_user": "5001", "expected_revision": 1, "confirm": True,
        })
        model = self.new_store.read(legacy_id, "group:9001", {})
        self.assertEqual(model["text"], self.person_text)
        self.assertEqual(model["hash"], content_hash(self.person_text))
        self.assertEqual(model["origin_label"], "历史原文")


class IdentityHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix="v6_http_")
        self.addCleanup(self.temp.cleanup)
        self.fx = IdentityFixture(self.temp.name)
        self.server = memory_console.create_server(
            str(self.fx.runtime), port=0,
            token_file=str(self.temp.name + "/console.token"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.token = (Path(self.temp.name) / "console.token").read_text(encoding="ascii").strip()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def auth(self):
        return {"Authorization": "Bearer " + self.token}

    def post_headers(self, **extra):
        headers = {
            "Authorization": "Bearer " + self.token,
            "Origin": self.origin,
            "Content-Type": "application/json",
        }
        headers.update(extra)
        return headers

    def payload(self, **values):
        body = {"action": "link_group", "old_group": "111", "new_group": "9001",
                "expected_revision": 0, "confirm": True}
        body.update(values)
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    def test_endpoints_authentication_origin_limits_and_roundtrip(self):
        status, _body = self.request("GET", "/api/identity/catalog")
        self.assertEqual(status, 401)
        status, body = self.request("GET", "/api/identity/catalog", headers=self.auth())
        self.assertEqual(status, 200)
        self.assertNotIn(b"openid", body.lower())
        self.assertNotIn(b"SECRET_GROUP_OPENID", body)
        self.assertEqual(json.loads(body)["data"]["revision"], 0)
        status, body = self.request(
            "GET", "/api/identity/members?side=old&group=111", headers=self.auth())
        self.assertEqual(status, 200)
        old_members = {item["old_user"] for item in json.loads(body)["data"]["members"]}
        self.assertIn("777", old_members)
        self.assertIn("888", old_members)
        status, body = self.request(
            "GET", "/api/identity/members?side=new&group=9001", headers=self.auth())
        self.assertEqual(status, 200)
        new_members = {item["new_user"] for item in json.loads(body)["data"]["members"]}
        self.assertIn("5001", new_members)
        self.assertNotIn("5004", new_members)

        status, _body = self.request(
            "POST", "/api/identity/apply", self.payload(), self.post_headers(Origin=""))
        self.assertEqual(status, 403)
        status, _body = self.request(
            "POST", "/api/identity/apply", self.payload(),
            self.post_headers(Origin="http://evil.example"))
        self.assertEqual(status, 403)
        status, _body = self.request(
            "POST", "/api/identity/apply", self.payload(),
            self.post_headers(**{"Content-Type": "text/plain"}))
        self.assertEqual(status, 415)
        status, _body = self.request(
            "POST", "/api/identity/apply", b'{"action":"link_group"}',
            self.post_headers())
        self.assertEqual(status, 400)
        status, _body = self.request(
            "POST", "/api/identity/apply", self.payload(evil="x"),
            self.post_headers())
        self.assertEqual(status, 400)
        status, _body = self.request(
            "POST", "/api/identity/apply", b"{" + b"x" * 33000,
            self.post_headers())
        self.assertEqual(status, 413)
        status, _body = self.request(
            "POST", "/api/identity/apply", self.payload(confirm=False),
            self.post_headers())
        self.assertEqual(status, 400)
        status, body = self.request(
            "POST", "/api/identity/preview",
            json.dumps({"action": "link_group", "old_group": "111",
                        "new_group": "9001", "expected_revision": 0}).encode(),
            self.post_headers())
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["data"]["valid"])
        status, body = self.request(
            "POST", "/api/identity/apply", self.payload(), self.post_headers())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"]["revision"], 1)
        status, body = self.request(
            "POST", "/api/identity/apply", self.payload(), self.post_headers())
        self.assertEqual(status, 409)
        status, body = self.request("GET", "/api/identity/links", headers=self.auth())
        self.assertEqual(status, 200)
        data = json.loads(body)["data"]
        self.assertEqual(len(data["group_links"]), 1)
        self.assertEqual(data["audit"][0]["operator"], "local_operator")
        self.assertEqual(data["audit"][0]["operator_source"], "memory_console")
        self.assertNotIn(b"openid", body.lower())
        self.assertNotIn(b"SECRET_GROUP_OPENID", body)
        status, _body = self.request(
            "POST", "/api/status", b"{}", self.post_headers())
        self.assertEqual(status, 405)

    def test_memory_route_delegates_and_disappears_on_unlink(self):
        legacy_id = "L" + str(self.fx.person_note_id)
        # The operator can verify the old original before any mapping exists.
        status, body = self.request(
            "GET", "/api/identity/memory?id=" + legacy_id + "&group=9001",
            headers=self.auth())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"]["text"], self.fx.person_note)
        status, _body = self.request(
            "GET", "/api/memory?id=" + legacy_id + "&group=9001", headers=self.auth())
        self.assertEqual(status, 404)
        status, body = self.request(
            "POST", "/api/identity/apply", self.payload(), self.post_headers())
        self.assertEqual(status, 200)
        member = {
            "action": "link_member", "old_group": "111", "new_group": "9001",
            "old_user": "777", "new_user": "5001", "expected_revision": 1, "confirm": True,
        }
        status, body = self.request(
            "POST", "/api/identity/apply", json.dumps(member).encode(),
            self.post_headers())
        self.assertEqual(status, 200)
        status, body = self.request(
            "GET", "/api/identity/memory?id=" + legacy_id + "&group=9001",
            headers=self.auth())
        self.assertEqual(status, 200)
        data = json.loads(body)["data"]
        self.assertEqual(data["origin_label"], "历史原文")
        self.assertIn("旧群111→新群9001", data["identity_label"])
        unlink = {
            "action": "unlink_group", "old_group": "111", "new_group": "9001",
            "expected_revision": 2, "confirm": True,
        }
        status, body = self.request(
            "POST", "/api/identity/apply", json.dumps(unlink).encode(),
            self.post_headers())
        self.assertEqual(status, 200)
        # Admin preview remains available after unbinding; the model reader does not.
        status, body = self.request(
            "GET", "/api/identity/memory?id=" + legacy_id + "&group=9001",
            headers=self.auth())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"]["text"], self.fx.person_note)
        status, _body = self.request(
            "GET", "/api/memory?id=" + legacy_id + "&group=9001", headers=self.auth())
        self.assertEqual(status, 404)


class IdentityAssetTests(unittest.TestCase):
    def test_identity_assets_entry_and_layout_engine_are_wired(self):
        static = repo_file("memory_console_static")
        for name in ("identity.html", "identity.js", "identity.css"):
            self.assertTrue((static / name).is_file(), name)
        html = (static / "identity.html").read_text(encoding="utf-8")
        js = (static / "identity.js").read_text(encoding="utf-8")
        index = (static / "index.html").read_text(encoding="utf-8")
        self.assertIn("旧群与成员匹配", html)
        self.assertIn("identity.js", html)
        self.assertIn("identity.css", html)
        self.assertIn("版本 0", html)
        for term in ("local_operator", "官方 runtime", "group_weights", "group_profiles",
                     "旧 root", "actors 只用于", "revision 0"):
            self.assertNotIn(term, html)
        for marker in ("/api/identity/catalog", "/api/identity/members",
                       "/api/identity/preview", "/api/identity/apply"):
            self.assertIn(marker, js)
        self.assertIn("查看旧原文", js)
        self.assertIn("你手动确认", js)
        self.assertIn("id=\"identityEntry\"", index)
        self.assertIn("旧群与成员匹配", index)
        self.assertIn("/layout.js", index)
        self.assertTrue((static / "layout.js").is_file())
        self.assertIn("/layout.js", memory_console.STATIC_FILES)
        self.assertIn("/identity.html", memory_console.STATIC_FILES)
        self.assertIn("/identity.js", memory_console.STATIC_FILES)
        self.assertIn("/identity.css", memory_console.STATIC_FILES)
        from hakimi.transport import official_test
        self.assertIn("identity_links.py", official_test.CORE)
        self.assertIn("legacy_md_notes.py", official_test.CORE)


if __name__ == "__main__":
    unittest.main()
