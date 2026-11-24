"""Regression cases from the independent v4 review.

These stay separate from the original acceptance file so the reviewed failure
modes remain visible: session-bound versions, old CQ parsing, graph endpoints,
closeness/source selection, latest originals, seed evidence and v3 read paths.
"""
import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

here = Path(__file__).resolve().parent
ROOT = here.parent
sys.path.insert(0, str(ROOT))

from hakimi.console import memory_console as console  # noqa: E402
from hakimi.memory.memory_graph import compact_graph  # noqa: E402
from hakimi.social.memory_social import (  # noqa: E402
    interaction_stats,
    latest_assessments,
    record_social,
    validate_seed,
)
from hakimi.memory.memory_store import MemoryStore  # noqa: E402
from _repo import repo_file


class ReviewBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=here, prefix="v4_review_")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.store = MemoryStore(self.base)
        self.now = time.time()
        self.seq = 0

    def chat(self, uid="101", session="group:1", text="公开讨论", at=None, **extra):
        self.seq += 1
        self.store.observe({
            "user_id": uid, "session_key": session,
            "message_id": f"review-{self.seq}", "received_at": at or (self.now + self.seq),
            "text": text, **extra,
        })
        with self.store.db() as db:
            return "M" + str(db.execute(
                "SELECT id FROM records WHERE kind='chat' ORDER BY id DESC LIMIT 1").fetchone()[0])

    def assess(self, mid, target="101", session="group:1", note="关系印象", **kw):
        provider = kw.pop("provider", "gemini")
        provenance = kw.pop("provenance", "gemini_manual")
        return record_social(
            self.base, session, note, target, relationship_type=kw.pop("relationship_type", "学习讨论"),
            evidence_memory_ids=[mid], provider=provider, provenance=provenance,
            origin_message_id=kw.pop("origin", f"origin-{time.time_ns()}"), **kw)


class SocialRegressionTests(ReviewBase):
    def test_same_body_reason_and_confidence_revisions_version(self):
        mid = self.chat()
        first = self.assess(mid, confidence=70, reason="最初依据")
        second = self.assess(mid, confidence=40, reason="材料不足")
        self.assertNotEqual(first["assessment_id"], second["assessment_id"])
        self.assertEqual(second["version"], 2)

    def test_private_session_never_supersedes_group_and_default_excludes_private(self):
        group = self.chat()
        private = self.chat(session="private:101")
        public = self.assess(group, note="群聊印象")
        self.assess(private, session="private:101", note="私聊印象")
        with self.store.db() as db:
            self.assertEqual([item["id"] for item in latest_assessments(db)],
                             [public["assessment_id"]])
            self.assertEqual(len(latest_assessments(db, session="private:101")), 1)

    def test_pruned_evidence_downgrades_numbers(self):
        mid = self.chat()
        self.assess(mid, affinity=55)
        with self.store.db() as db:
            db.execute("UPDATE records SET text='' WHERE id=?", (int(mid[1:]),))
        with self.store.db() as db:
            item = latest_assessments(db, session="group:1")[0]
        self.assertIsNone(item["affinity"])
        self.assertFalse(item["scores_available"])
        self.assertTrue(item["evidence_expired"])

    def test_nested_quote_and_forward_cq_are_not_current_mentions(self):
        self.chat(text='[引用 某人 的消息(id=1): "[CQ:at,qq=102] 旧引用"] [CQ:at,qq=103] 当前发言')
        with self.store.db() as db:
            stats = interaction_stats(db, session="group:1")
        self.assertIn(("101", "103"), stats)
        self.assertNotIn(("101", "102"), stats)

    def test_one_way_events_count_events_not_days(self):
        self.chat(direct_targets=["102"])
        self.chat(direct_targets=["102"], at=self.now + 10)
        with self.store.db() as db:
            stats = interaction_stats(db, session="group:1")
        item = stats[("101", "102")]
        self.assertEqual(item["events"], 2)
        self.assertEqual(item["direction_events"], {"101": 2})
        self.assertEqual(item["mutual_days"], set())


class ConsoleRegressionTests(ReviewBase):
    def test_closeness_can_be_highlighted_for_each_source(self):
        mid = self.chat()
        record_social(
            self.base, "group:1", "辅助亲近", "101", relationship_type="群友214",
            affinity=30, closeness=70, confidence=60, evidence_memory_ids=[mid],
            provider="dsh", provenance="dsh_assisted", origin_message_id="assisted-closeness")
        record_social(
            self.base, "group:1", "亲笔亲近", "101", relationship_type="群友214",
            affinity=20, closeness=25, confidence=80, evidence_memory_ids=[mid],
            provider="gemini", provenance="gemini_manual", origin_message_id="manual-closeness")
        assisted = console.graph_payload(self.base, {"measure": "closeness", "source": "assisted"})
        manual = console.graph_payload(self.base, {"measure": "closeness", "source": "manual"})
        self.assertEqual(assisted["edges"][0]["metric_score"], 70)
        self.assertEqual(manual["edges"][0]["metric_score"], 25)
        self.assertEqual(manual["measure"], "closeness")
        self.assertEqual(manual["source"], "manual")

    def test_relation_keeps_both_directions_and_newest_label(self):
        first = self.chat(uid="101")
        second = self.chat(uid="102")
        record_social(
            self.base, "group:1", "旧的辅助判断", "102", subject_id="101",
            relationship_type="旧标签", evidence_memory_ids=[first, second],
            provider="dsh", provenance="dsh_assisted", origin_message_id="old-dsh",
            now=self.now - 100)
        record_social(
            self.base, "group:1", "新的亲笔判断", "102", subject_id="101",
            relationship_type="新标签", evidence_memory_ids=[first, second],
            provider="gemini", provenance="gemini_manual", origin_message_id="new-manual",
            now=self.now)
        record_social(
            self.base, "group:1", "反向辅助判断", "101", subject_id="102",
            relationship_type="反向标签", evidence_memory_ids=[first, second],
            provider="dsh", provenance="dsh_assisted", origin_message_id="reverse",
            now=self.now + 1)
        data = console.relation_payload(self.base, {"a": "101", "b": "102"})
        self.assertEqual({item["direction"] for item in data["directions"]},
                         {"101->102", "102->101"})
        graph = console.graph_payload(self.base, {})
        edge = next(item for item in graph["edges"]
                    if {item["source"], item["target"]} == {"person:101", "person:102"})
        self.assertEqual(edge["relationship_type"], "反向标签")
        self.assertEqual({item["direction"] for item in edge["directions"]},
                         {"101->102", "102->101"})

    def test_manual_latest_marker_and_partner_name_batch(self):
        mid = self.chat()
        old = self.store.remember_manual(
            {"kind": "person", "user_id": "101", "key": "研究背景", "note": "旧版科研背景"},
            session="group:1", provider="gemini", now=self.now - 100)
        new = self.store.remember_manual(
            {"kind": "person", "user_id": "101", "key": "研究背景", "note": "新版研究背景"},
            session="group:1", provider="gemini", now=self.now)
        self.chat(uid="101", text="[CQ:at,qq=104] 找伙伴", direct_targets=["104"], nickname="发起人")
        self.chat(uid="104", text="回应", nickname="伙伴名字")
        data = console.person_payload(self.base, {"id": "101"})
        latest = data["impressions"]["manual"][0]
        self.assertEqual(latest["memory_id"], new["id"])
        self.assertTrue(latest["is_latest"])
        old_item = next(item for item in data["impressions"]["manual"] if item["memory_id"] == old["id"])
        self.assertFalse(old_item["is_latest"])
        partner = next(item for item in data["interaction"]["partners"] if item["id"] == "104")
        self.assertEqual(partner["name"], "伙伴名字")

    def test_bot_graph_focus_returns_compact_sourced_social_values(self):
        mid = self.chat()
        self.assess(mid, closeness=70)
        graph = self.store.graph("group:1", ["101"], focus_type="person", focus_id="101", limit=24)
        self.assertTrue(graph["social_relations"])
        compact = compact_graph(graph)
        self.assertTrue(compact["social_relations"])
        relation = compact["social_relations"][0]
        self.assertEqual(relation["direction"], "hakimi->101")
        self.assertEqual(relation["closeness"], 70)
        self.assertEqual(relation["provenance"], "gemini_manual")
        self.assertLessEqual(len(json.dumps(compact, ensure_ascii=False, separators=(",", ":"))), 2400)

    def test_v3_database_without_social_table_still_reads_status_and_original(self):
        mid = self.chat()
        note = self.store.remember_manual(
            {"kind": "person", "user_id": "101", "key": "旧库原文", "note": "v3 原文"},
            session="group:1", provider="gemini", now=self.now)
        with self.store.db() as db:
            db.execute("DROP TABLE social_assessments")
        status = console.status_payload(self.base, self.now)
        self.assertGreaterEqual(status["counts"]["records"], 1)
        detail = console.memory_payload(self.base, {"id": note["id"], "offset": 0, "limit": 500})
        self.assertEqual(detail["text"], "v3 原文")
        server = console.create_server(self.base, port=0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        token = (self.base / "state/memory_console.token").read_text(encoding="utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            connection.request("GET", "/api/status", headers={"Authorization": "Bearer " + token})
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertGreaterEqual(payload["data"]["counts"]["records"], 1)
        finally:
            connection.close()


class SeedAndFrontendReviewTests(unittest.TestCase):
    @unittest.skipUnless((ROOT.parent / "social-evidence.json").exists(), "social-evidence.json 只在原 Windows 工作目录中存在")
    def test_seed_single_direction_claims_match_citations(self):
        seed = json.loads((repo_file("social_seed.json")).read_text(encoding="utf-8"))
        evidence = json.loads((ROOT.parent / "social-evidence.json").read_text(encoding="utf-8"))
        by_id = {}
        for person in evidence["people"]:
            for message in person.get("messages", []):
                by_id[message["id"]] = message
        for pair in evidence["pairs"]:
            for message in pair.get("evidence", []):
                by_id.setdefault(message["id"], message)
        relations = {item["seed_id"]: item for item in seed["relations"]}
        for seed_id in ("relation-majiang-jing-20261027", "relation-tianran-jing-20261027"):
            item = relations[seed_id]
            self.assertIn("单向", item["relationship_type"])
            senders = {by_id[int(mid[1:])]["user_id"] for mid in item["evidence_memory_ids"]}
            self.assertEqual(senders, {item["subject_id"]})
            self.assertNotIn("鲸有接话", item["impression"] + item["reason"])
            self.assertNotIn("视频逗", item["impression"])
            self.assertNotIn("鲸也常接梗", item["impression"])
        self.assertTrue(validate_seed(repo_file("social_seed.json"),
                                      ROOT.parent / "social-evidence.json")["ok"])

    def test_frontend_exposes_both_measure_and_source_and_canvas_pan(self):
        index = (repo_file("memory_console_static/index.html")).read_text(encoding="utf-8")
        app = (repo_file("memory_console_static/app.js")).read_text(encoding="utf-8")
        css = (repo_file("memory_console_static/styles.css")).read_text(encoding="utf-8")
        self.assertIn('id="measure"', index)
        self.assertIn('id="source"', index)
        self.assertIn("closeness", index)
        self.assertIn("metricScore", app)
        self.assertIn("panX", app)
        self.assertIn("startPan", app)
        self.assertIn("displayName", app)
        self.assertIn("is_latest", app)
        self.assertIn("version only advances", app)
        self.assertNotIn("Promise.allSettled", app)
        self.assertIn("font-size: 17px", css)
        self.assertNotIn("sessionStorage", index)
        self.assertNotIn("SQLite", index)


if __name__ == "__main__":
    unittest.main()
