"""v4 social impressions, seed import, live console security and data tests.

All tests are offline and use temporary SQLite databases.  They never call a
model, read credentials, or touch a real bot.
"""
import contextlib
from datetime import datetime, timezone
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

here = Path(__file__).resolve().parent
ROOT = here.parent
sys.path.insert(0, str(ROOT))

from hakimi.social.memory_social import (  # noqa: E402
    SocialError,
    familiar_score,
    import_seed,
    interaction_stats,
    latest_assessments,
    pair_familiarity,
    record_social,
    validate_seed,
)
from hakimi.memory.memory_store import MemoryStore, content_hash  # noqa: E402
from hakimi.memory.smart_memory import remember_memory  # noqa: E402
from hakimi.console.memory_console import create_server, load_or_create_token  # noqa: E402
from _repo import repo_file


def day(offset=0):
    return time.time() - offset * 86400


def utc_day(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d")


class SocialBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=here, prefix="v4_social_")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        (self.base / "state").mkdir(parents=True, exist_ok=True)
        self.store = MemoryStore(self.base)
        self.now = time.time()

    def observe(self, text, user="101", session="group:1", mid="1", at=None, **extra):
        row = {"session_key": session, "user_id": user, "message_id": mid,
               "received_at": self.now if at is None else at, "text": text, **extra}
        self.store.observe(row)
        with self.store.db() as db:
            return db.execute("SELECT id FROM records WHERE origin=? AND session=? ORDER BY id DESC LIMIT 1",
                              (str(mid), session)).fetchone()[0]

    def make_evidence(self, user="101", session="group:1", count=3, at=None, **extra):
        ids = []
        for index in range(count):
            ids.append(self.observe(
                f"@哈基米 公开对话 {index}", user=user, session=session,
                mid=f"{session}:{user}:{index}", at=(at or self.now) - index * 3600, **extra))
        return [f"M{value}" for value in ids]


class SocialAssessmentTests(SocialBase):
    def test_exact_note_hash_retry_and_unknown_scores(self):
        evidence = self.make_evidence()
        note = "  她今天愿意接话。\n\n但这只是一次观察 ✨  "
        result = record_social(
            self.base, "group:1", note, "101",
            relationship_type="聊过几次的群友", affinity=70, closeness=None,
            confidence=80, evidence_memory_ids=evidence,
            origin_message_id="turn-1", now=self.now,
        )
        self.assertTrue(result["saved"])
        self.assertTrue(result["created"])
        self.assertEqual(result["direction"], "hakimi->101")
        self.assertEqual(result["provenance"], "gemini_manual")
        self.assertEqual(result["hash"], hashlib.sha256(note.encode("utf-8")).hexdigest())
        read = self.store.read(result["memory_id"], "group:1", {"101"})
        self.assertEqual(read["text"], note)
        self.assertEqual(read["text_kind"], "manual_original")
        self.assertEqual(read["kind"], "person")
        with self.store.db() as db:
            row = db.execute("SELECT * FROM social_assessments WHERE id=?", (result["assessment_id"],)).fetchone()
            self.assertEqual(row["subject_id"], "hakimi")
            self.assertEqual(row["target_id"], "101")
            self.assertEqual(row["affinity"], 70)
            self.assertIsNone(row["closeness"])
            self.assertEqual(row["confidence"], 80)
            self.assertEqual(row["provenance"], "gemini_manual")
            self.assertEqual(row["original_record_id"], result["record_id"])

        replay = record_social(
            self.base, "group:1", note, "101",
            relationship_type="聊过几次的群友", affinity=70, closeness=None,
            confidence=80, evidence_memory_ids=evidence,
            origin_message_id="turn-1", now=self.now + 10,
        )
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["memory_id"], result["memory_id"])
        self.assertEqual(replay["assessment_id"], result["assessment_id"])
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM memory_originals").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM social_assessments").fetchone()[0], 1)

    def test_out_of_range_scores_are_rejected(self):
        evidence = self.make_evidence()
        for value in (-1, 101, 1.5, True, "5x"):
            with self.assertRaises(SocialError) as caught:
                record_social(self.base, "group:1", "印象", "101", affinity=value,
                              evidence_memory_ids=evidence, origin_message_id="bad")
            self.assertIn(caught.exception.code, {"invalid_score"})
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM social_assessments").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM memory_originals").fetchone()[0], 0)

    def test_revision_keeps_original_and_supersedes_projection(self):
        evidence = self.make_evidence()
        first = record_social(self.base, "group:1", "旧印象：只是普通搭话", "101",
                              relationship_type="群友", affinity=40, evidence_memory_ids=evidence,
                              origin_message_id="turn-old", now=self.now)
        second = record_social(self.base, "group:1", "新印象：连续几天都主动搭话，但仍不下好感结论", "101",
                               relationship_type="群友", affinity=None, evidence_memory_ids=evidence,
                               origin_message_id="turn-new", now=self.now + 5)
        self.assertNotEqual(first["memory_id"], second["memory_id"])
        self.assertEqual(second["version"], 2)
        old_read = self.store.read(first["memory_id"], "group:1", {"101"})
        self.assertEqual(old_read["text"], "旧印象：只是普通搭话")
        self.assertEqual(old_read["status"], "superseded")
        with self.store.db() as db:
            rows = db.execute("SELECT id,state,superseded_by,affinity FROM social_assessments ORDER BY id").fetchall()
            self.assertEqual([(row["state"], row["affinity"]) for row in rows],
                             [("superseded", 40), ("active", None)])
            self.assertEqual(rows[0]["superseded_by"], rows[1]["id"])
            originals = db.execute("SELECT text FROM memory_originals ORDER BY id").fetchall()
            self.assertEqual([row[0] for row in originals],
                             ["旧印象：只是普通搭话", "新印象：连续几天都主动搭话，但仍不下好感结论"])

    def test_evidence_scope_and_known_people(self):
        visible = self.observe("@哈基米 本群第一句", user="101", session="group:1", mid="g1")
        cross = self.observe("@哈基米 另一群公开句", user="101", session="group:2", mid="g2")
        other = self.observe("@哈基米 不是目标", user="303", session="group:1", mid="g3")
        private = self.observe("私聊秘密", user="202", session="private:202", mid="p1")
        with self.assertRaises(SocialError) as caught:
            record_social(self.base, "group:1", "不应采用私聊证据", "101",
                          evidence_memory_ids=[f"M{private}"], origin_message_id="x")
        self.assertEqual(caught.exception.code, "evidence_unreadable")
        with self.assertRaises(SocialError) as caught:
            record_social(self.base, "private:202", "私聊不能引用群聊证据", "202",
                          evidence_memory_ids=[f"M{visible}"], origin_message_id="x")
        self.assertEqual(caught.exception.code, "evidence_unreadable")
        ok = record_social(self.base, "group:1", "允许使用另一群的公开证据", "101",
                           evidence_memory_ids=[f"M{cross}"], origin_message_id="cross")
        self.assertTrue(ok["saved"])
        with self.assertRaises(SocialError) as caught:
            record_social(self.base, "group:1", "证据不是目标本人", "101",
                          evidence_memory_ids=[f"M{other}"], origin_message_id="other")
        self.assertEqual(caught.exception.code, "evidence_not_about_target")
        with self.assertRaises(SocialError) as caught:
            record_social(self.base, "group:1", "虚构人物", "817740",
                          evidence_memory_ids=[f"M{visible}"], origin_message_id="fake")
        self.assertEqual(caught.exception.code, "target_not_visible")

    def test_relation_direction_and_subject_validation(self):
        first = self.observe("[CQ:at,qq=202] 这个说法不对", user="101", session="group:1", mid="a",
                             direct_targets=["202"], self_id="8415435122")
        second = self.observe("[CQ:at,qq=101] 我解释一下", user="202", session="group:1", mid="b",
                              direct_targets=["101"], self_id="8415435122")
        result = record_social(self.base, "group:1", "两人会围绕剧情直接互相@反驳", "202",
                               subject_id="101", relationship_type="剧情争论",
                               evidence_memory_ids=[f"M{first}", f"M{second}"],
                               origin_message_id="rel")
        self.assertEqual(result["direction"], "101->202")
        self.assertEqual(result["provenance"], "gemini_manual")
        with self.assertRaises(SocialError) as caught:
            record_social(self.base, "group:1", "自指", "101", subject_id="101",
                          evidence_memory_ids=[f"M{first}"], origin_message_id="self")
        self.assertEqual(caught.exception.code, "invalid_pair")

    def test_interaction_familiarity_is_cross_day_and_not_co_presence(self):
        for index in range(4):
            self.observe("@哈基米 单向搭话", user="101", mid=f"one-{index}", at=self.now - index * 86400,
                         direct_targets=["8415435122"], self_id="8415435122")
        with self.store.db() as db:
            stats = interaction_stats(db, self_ids={"8415435122"})
        score, basis, detail = pair_familiarity(stats, "101", "hakimi")
        self.assertIsNotNone(score)
        self.assertGreater(score, 0)
        self.assertEqual(basis["mutual_days"], 0)
        self.assertIn("单向", basis["reason"])
        self.assertEqual(detail["direction_events"], {"101": 4})

        # Add the other direction on two of the days: only now are mutual days real.
        for index in range(2):
            self.observe("哈基米回复", user="8415435122", mid=f"back-{index}",
                         at=self.now - index * 86400 - 120, direct_targets=["101"], self_id="8415435122")
        with self.store.db() as db:
            stats = interaction_stats(db, self_ids={"8415435122"})
        score, basis, detail = pair_familiarity(stats, "101", "hakimi")
        self.assertEqual(basis["mutual_days"], 2)
        self.assertIn("双向", basis["reason"])

        # Consecutive co-presence without an @/reply is not a social relation.
        co_base = Path(str(self.base) + "_co")
        (co_base / "state").mkdir(parents=True)
        co_store = MemoryStore(co_base)
        for index in range(3):
            co_store.observe({"session_key": "group:1", "user_id": "404", "message_id": f"c{index}",
                              "received_at": self.now + index, "text": "只是同场说话"})
        with co_store.db() as db:
            self.assertEqual(interaction_stats(db, self_ids={"8415435122"}), {})
        self.assertIsNone(familiar_score(None)[0])

    @unittest.skipUnless((ROOT.parent / "social-evidence.json").exists(), "social-evidence.json 只在原 Windows 工作目录中存在")

    def test_real_seed_is_traceable_and_assisted(self):
        seed = repo_file("social_seed.json")
        evidence = ROOT.parent / "social-evidence.json"
        result = validate_seed(seed, evidence)
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["items"], 12)
        data = json.loads(seed.read_text(encoding="utf-8"))
        self.assertEqual(data["source"], "dsh_assisted")
        for item in data["people"] + data["relations"]:
            self.assertEqual(item["source"], "dsh_assisted")
            self.assertTrue(item["evidence_memory_ids"])
            self.assertTrue(item["evidence_dates"])
            self.assertIsInstance(item["confidence"], int)
            for value in ("affinity", "closeness"):
                self.assertTrue(item[value] is None or 0 <= item[value] <= 100)

    def test_seed_import_is_idempotent_and_never_gemini_original(self):
        target = self.observe("@哈基米 种子目标", user="101", mid="seed-target", at=self.now - 100)
        other = self.observe("[CQ:at,qq=101] 关系另一端", user="202", mid="seed-other",
                             at=self.now - 90, direct_targets=["101"], self_id="8415435122")
        dates = [utc_day(self.now - 100), utc_day(self.now - 90)]
        seed_data = {
            "version": 1, "source": "dsh_assisted", "generated_by": "dsh",
            "generated_at": self.now,
            "people": [{
                "seed_id": "test-person-101", "subject_id": "hakimi", "target_id": "101",
                "session": "group:1", "relationship_type": "测试关系", "affinity": 55,
                "closeness": None, "confidence": 65,
                "impression": "测试用的辅助印象，不是手写原话。",
                "reason": "两条公开消息。", "source": "dsh_assisted",
                "evidence_memory_ids": [f"M{target}", f"M{other}"],
                "evidence_dates": sorted(set(dates)),
            }],
            "relations": [{
                "seed_id": "test-rel-101-202", "subject_id": "202", "target_id": "101",
                "session": "group:1", "relationship_type": "公开互动", "affinity": None,
                "closeness": None, "confidence": 60,
                "impression": "关系辅助印象。", "reason": "公开直接@。",
                "source": "dsh_assisted",
                "evidence_memory_ids": [f"M{target}", f"M{other}"],
                "evidence_dates": sorted(set(dates)),
            }]
        }
        seed_path = self.base / "seed.json"
        seed_path.write_text(json.dumps(seed_data, ensure_ascii=False), encoding="utf-8")
        first = import_seed(self.base, seed_path)
        self.assertEqual(first["imported"], 2)
        self.assertEqual(first["skipped"], [])
        second = import_seed(self.base, seed_path)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["duplicates"], 2)
        with self.store.db() as db:
            originals = db.execute(
                "SELECT r.kind,r.user_id,o.provider,o.channel,o.text FROM records r "
                "JOIN memory_originals o ON o.record_id=r.id WHERE o.provider='dsh'").fetchall()
            assessments = latest_assessments(db)
            self.assertEqual(len(originals), 2)
            self.assertTrue(all(row["kind"] == "reflection" and row["user_id"] == "" for row in originals))
            self.assertEqual(len(assessments), 2)
            self.assertTrue(all(item["provenance"] == "dsh_assisted" for item in assessments))
            self.assertEqual(db.execute("SELECT count(*) FROM social_assessments").fetchone()[0], 2)
            # Deleting evidence must remove it from visible scoring, not invent a new score.
            db.execute("UPDATE records SET state='deleted' WHERE id=?", (target,))
        with self.store.db() as db:
            after = latest_assessments(db)
        self.assertTrue(after)
        self.assertTrue(all(item["affinity"] is None and not item["scores_available"] for item in after))
        # A deleted evidence row makes the next import skip instead of creating a fake rating.
        again = import_seed(self.base, seed_path)
        self.assertEqual(again["imported"], 0)
        self.assertEqual(len(again["skipped"]), 2)


class ConsoleTestBase(unittest.TestCase):
    """One live loopback server shared by the console tests."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=here, prefix="v4_console_")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.base = Path(cls.tmp.name).resolve()
        (cls.base / "state").mkdir(parents=True, exist_ok=True)
        cls.store = MemoryStore(cls.base)
        cls.now = time.time()
        cls.token = "T" * 48
        cls.token_file = cls.base / "state/console.token"
        cls.token_file.write_text(cls.token, encoding="utf-8")
        cls._seed_data()
        cls.server = create_server(cls.base, port=0, token_file=cls.token_file)
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def _seed_data(cls):
        for index in range(65):
            user = str(100 + index)
            nickname = "成员" + str(index)
            if index == 0:
                nickname = '<img src=x onerror="window.__pwn=1">'
            for message in range(2):
                cls.store.observe({
                    "session_key": "group:1", "user_id": user, "message_id": f"g1-{index}-{message}",
                    "received_at": cls.now - index * 30 - message * 86400,
                    "text": ("@哈基米 对话 " if message == 0 else "普通聊天 ") + f"{index}-{message}",
                    "nickname": nickname,
                    "direct_targets": ["8415435122"] if message == 0 else [],
                    "self_id": "8415435122",
                })
        cls.store.observe({"session_key": "group:1", "user_id": "101", "message_id": "g1-extra-1",
                           "received_at": cls.now - 10, "text": "@哈基米 更新后再说话",
                           "nickname": "成员1", "direct_targets": ["8415435122"], "self_id": "8415435122"})
        cls.store.observe({"session_key": "group:1", "user_id": "101", "message_id": "g1-extra-2",
                           "received_at": cls.now - 5, "text": "@哈基米 又一句",
                           "nickname": "成员1", "direct_targets": ["8415435122"], "self_id": "8415435122"})
        for extra in range(2):
            cls.store.observe({
                "session_key": "group:1", "user_id": "100", "message_id": f"g1-100-extra-{extra}",
                "received_at": cls.now - (extra + 2) * 86400, "text": "@哈基米 更早的直接搭话",
                "nickname": '<img src=x onerror="window.__pwn=1">',
                "direct_targets": ["8415435122"], "self_id": "8415435122",
            })
        cls.store.observe({"session_key": "group:2", "user_id": "202", "message_id": "g2-1",
                           "received_at": cls.now - 80, "text": "第二群普通聊天", "nickname": "小绿"})
        cls.store.observe({"session_key": "private:202", "user_id": "202", "message_id": "p-1",
                           "received_at": cls.now - 70, "text": "私聊秘密内容", "nickname": "小绿"})
        with cls.store.db() as db:
            first_m = db.execute("SELECT id FROM records WHERE origin='g1-0-1' LIMIT 1").fetchone()[0]
            extra_m = db.execute("SELECT id FROM records WHERE origin='g1-extra-1' LIMIT 1").fetchone()[0]
            private_m = db.execute("SELECT id FROM records WHERE origin='p-1' LIMIT 1").fetchone()[0]
        cls.first_m = "M" + str(first_m)
        cls.extra_m = "M" + str(extra_m)
        cls.private_m = "M" + str(private_m)
        # Gemini-authored person note, including safe literal HTML that must never be interpreted.
        cls.manual = cls.store.remember_manual(
            {"kind": "person", "note": "手写原文 <script>alert('x')</script> 保留空格\n第二行",
             "user_id": "100", "key": "称呼", "evidence": "公开原话"},
            session="group:1", provider="gemini", channel="gemini_tool", now=cls.now - 20,
        )
        # DSH-assisted assessment and note (provider separated from Gemini originals).
        cls.assisted = record_social(
            cls.base, "group:1", "辅助整理：成员100会接话，但样本少，不能推断好感。", "100",
            relationship_type="公开搭话的群友", affinity=55, closeness=None, confidence=60,
            evidence_memory_ids=[cls.first_m], provider="dsh", provenance="dsh_assisted",
            reason="只有公开对话证据。", origin_message_id="seed:console", seed_id="console-person-100",
            now=cls.now - 15,
        )
        # A visible relation between two members and one between Hakimi and a person.
        with cls.store.db() as db:
            m1 = db.execute("SELECT id FROM records WHERE origin='g1-1-0' LIMIT 1").fetchone()[0]
            m2 = db.execute("SELECT id FROM records WHERE origin='g1-2-0' LIMIT 1").fetchone()[0]
        record_social(
            cls.base, "group:1", "两人都围绕同一话题公开@过对方。", "102",
            subject_id="101", relationship_type="同话题互动", confidence=55,
            evidence_memory_ids=[f"M{m1}", f"M{m2}"], origin_message_id="rel-console",
        )
        # Long manual note for pagination.
        cls.long_note = "".join(chr(0x4e00 + (index % 400)) for index in range(2300))
        cls.long = cls.store.remember_manual(
            {"kind": "commitment", "note": cls.long_note, "key": "长文"},
            session="group:1", provider="gemini", channel="gemini_tool", now=cls.now - 10,
        )

    def request(self, path, token=True, headers=None, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        values = {}
        if token:
            values["Authorization"] = "Bearer " + self.token
        if headers:
            values.update(headers)
        try:
            connection.request(method, path, headers=values)
            response = connection.getresponse()
            body = response.read()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            return response.status, body, response_headers
        finally:
            connection.close()

    def get_json(self, path, token=True, headers=None):
        status, body, response_headers = self.request(path, token=token, headers=headers)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = None
        return status, payload, response_headers


class ConsoleSecurityTests(ConsoleTestBase):
    def test_static_is_data_free_and_api_requires_bearer(self):
        status, body, headers = self.request("/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"/app.js", body)
        self.assertNotIn("成员1".encode(), body)
        self.assertNotIn(b"M1", body)
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertNotIn("access-control-allow-origin", headers)
        status, body, headers = self.request("/layout.js", token=False)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/javascript; charset=utf-8")
        status, body, _ = self.request("/health", token=False)
        self.assertEqual(status, 200)
        payload = json.loads(body.decode())
        self.assertEqual(payload["service"], "memory-console")
        self.assertNotIn("counts", payload)
        self.assertNotIn("memory.sqlite3", body.decode())
        self.assertEqual(self.request("/api/status", token=False)[0], 401)
        self.assertEqual(self.request("/api/status", token=True,
                                      headers={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.request("/api/status")[0], 200)
        self.assertGreaterEqual(len(self.token.encode()), 32)
        if os.name != "nt":
            self.assertEqual(oct(self.token_file.stat().st_mode & 0o777), oct(0o600))

    def test_host_origin_methods_and_paths(self):
        self.assertEqual(self.request("/api/status", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("/api/status", headers={"Origin": "http://evil.example"})[0], 403)
        origin = f"http://127.0.0.1:{self.port}"
        self.assertEqual(self.request("/api/status", headers={"Origin": origin})[0], 200)
        status, body, headers = self.request("/api/status", token=False,
                                             headers={"Origin": origin})
        self.assertEqual(status, 401)
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertEqual(self.request("/api/status", method="POST")[0], 405)
        self.assertEqual(self.request("/api/status", method="OPTIONS")[0], 405)
        self.assertEqual(self.request("/../state/memory.sqlite3")[0], 404)
        self.assertEqual(self.request("/api/not-real")[0], 404)
        status, payload, _ = self.get_json("/api/memory?id=not-a-number")
        self.assertEqual(status, 400)
        self.assertIn("error", payload)
        self.assertNotIn("Traceback", json.dumps(payload))
        self.assertNotIn("memory.sqlite3", json.dumps(payload))

    def test_frontend_is_self_contained_and_uses_text_content(self):
        index = (repo_file("memory_console_static/index.html")).read_text(encoding="utf-8")
        app = (repo_file("memory_console_static/app.js")).read_text(encoding="utf-8")
        css = (repo_file("memory_console_static/styles.css")).read_text(encoding="utf-8")
        self.assertNotIn("http://", index)
        self.assertNotIn("https://", index)
        self.assertNotIn("innerHTML", app)
        self.assertNotIn("document.write", app)
        self.assertIn("textContent", app)
        self.assertIn("sessionStorage", app)
        self.assertIn("history.replaceState", app)
        self.assertIn("#token=", app)
        self.assertIn("textContent", index + css + app)
        self.assertNotIn("cdn.", index + app + css)

    def test_generated_token_is_long_and_private(self):
        missing = self.base / "state/new-token"
        token = load_or_create_token(missing)
        self.assertGreaterEqual(len(token.encode()), 32)
        self.assertEqual(missing.read_text(encoding="utf-8"), token)
        if os.name != "nt":
            self.assertEqual(oct(missing.stat().st_mode & 0o777), oct(0o600))
        short = self.base / "state/short-token"
        short.write_text("too-short", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_or_create_token(short)
        self.assertEqual(short.read_text(encoding="utf-8"), "too-short")


class ConsoleDataTests(ConsoleTestBase):
    def test_people_search_and_pagination_are_full_library(self):
        status, payload, _ = self.get_json("/api/people?page=1&page_size=30")
        self.assertEqual(status, 200)
        data = payload["data"]
        self.assertGreaterEqual(data["total"], 66)
        self.assertEqual(len(data["people"]), 30)
        self.assertGreater(data["pages"], 2)
        self.assertTrue(data["has_more"])
        self.assertIsNotNone(data["next"])
        last_page = data["pages"]
        status, payload, _ = self.get_json(f"/api/people?page={last_page}&page_size=30")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(payload["data"]["people"]), 1)
        # A person beyond a 20/35-node canvas can still be found by full-library search.
        status, payload, _ = self.get_json("/api/people?q=164&page_size=10")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["total"], 1)
        self.assertEqual(payload["data"]["people"][0]["id"], "164")
        query = urlencode({"q": "成员1"})
        status, payload, _ = self.get_json("/api/people?" + query)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(payload["data"]["total"], 1)
        status, payload, _ = self.get_json("/api/people?" + urlencode({"group": "group:2"}))
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["total"], 1)
        status, payload, _ = self.get_json("/api/people?" + urlencode({"group": "private:202"}))
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["people"][0]["id"], "202")
        status, payload, _ = self.get_json("/api/people?" + urlencode({"group": "bad:1"}))
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_group")

    def test_live_data_version_and_bounded_graph(self):
        status, payload, _ = self.get_json("/api/status")
        self.assertEqual(status, 200)
        before = payload["data"]["version"]
        self.store.observe({"session_key": "group:1", "user_id": "999", "message_id": "live-999",
                            "received_at": time.time(), "text": "@哈基米 实时新增", "nickname": "实时新人",
                            "direct_targets": ["8415435122"], "self_id": "8415435122"})
        status, payload, _ = self.get_json("/api/status")
        after = payload["data"]["version"]
        self.assertNotEqual(before, after)
        status, payload, _ = self.get_json("/api/people?q=999")
        self.assertEqual(payload["data"]["total"], 1)
        status, payload, _ = self.get_json("/api/graph?limit=20")
        graph = payload["data"]
        self.assertLessEqual(len(graph["nodes"]), 20)
        self.assertGreaterEqual(graph["counts"]["total_people_in_scope"], 66)
        self.assertGreater(graph["counts"]["omitted_nodes"], 0)
        self.assertTrue(graph["counts"]["truncated"])
        status, payload, _ = self.get_json("/api/graph?limit=500")
        self.assertEqual(status, 200)
        self.assertLessEqual(len(payload["data"]["nodes"]), 160)
        self.assertGreaterEqual(payload["data"]["limit"], 20)
        self.assertLessEqual(payload["data"]["limit"], 160)
        status, payload, _ = self.get_json("/api/graph?limit=20&focus=100")
        graph = payload["data"]
        self.assertEqual(graph["focus"], "100")
        self.assertTrue(any(node["official_id"] == "100" for node in graph["nodes"]))
        self.assertTrue(any(item["id"] == "assistant:hakimi" for item in graph["nodes"]))
        for edge in graph["edges"]:
            self.assertIn("scores", edge)
            self.assertIn("manual", edge["scores"])
            self.assertIn("assisted", edge["scores"])
            self.assertIn("interaction", edge["scores"])
        status, payload, _ = self.get_json("/api/graph?limit=20&metric=assisted&threshold=50")
        for edge in payload["data"]["edges"]:
            self.assertIsNotNone(edge["metric_score"])
            self.assertGreaterEqual(edge["metric_score"], 50)

    def test_person_relation_memory_and_html_is_data_not_markup(self):
        status, payload, _ = self.get_json("/api/person?id=100")
        self.assertEqual(status, 200)
        person = payload["data"]
        self.assertEqual(person["person"]["id"], "100")
        self.assertIn("<img", person["person"]["nickname"])
        manual = person["impressions"]["manual"]
        self.assertTrue(any("<script>" in item["text"] for item in manual))
        self.assertTrue(any(item["provider"] == "gemini" for item in manual))
        assisted = person["impressions"]["assisted"]
        self.assertEqual(len(assisted), 1)
        self.assertEqual(assisted[0]["provenance"], "dsh_assisted")
        self.assertTrue(assisted[0]["scores_available"])
        self.assertEqual(assisted[0]["evidence"][0]["memory_id"], self.first_m)
        self.assertIn("new_messages", assisted[0]["staleness"])
        self.assertGreaterEqual(assisted[0]["staleness"]["new_messages"], 1)
        self.assertTrue(assisted[0]["staleness"]["needs_update"])
        self.assertIsNotNone(person["interaction"]["with_hakimi"]["score"])
        self.assertTrue(person["people"])

        status, payload, _ = self.get_json("/api/relation?a=hakimi&b=100")
        self.assertEqual(status, 200)
        relation = payload["data"]
        self.assertEqual(relation["assessments"][0]["direction"], "hakimi->100")
        self.assertEqual(relation["assessments"][0]["subject_id"], "hakimi")
        self.assertIn("互动熟悉度", relation["policy"])

        status, payload, _ = self.get_json("/api/memories?q=script")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(payload["data"]["total"], 1)

        long_memory = self.long["id"]
        status, payload, _ = self.get_json("/api/memory?id=" + long_memory + "&limit=500")
        self.assertEqual(status, 200)
        first = payload["data"]
        self.assertTrue(first["has_more"])
        parts = [first["text"]]
        next_offset = first["next_offset"]
        while next_offset is not None:
            status, payload, _ = self.get_json(f"/api/memory?id={long_memory}&offset={next_offset}&limit=500")
            section = payload["data"]
            parts.append(section["text"])
            next_offset = section["next_offset"]
        self.assertEqual("".join(parts), self.long_note)
        self.assertEqual(first["hash"], hashlib.sha256(self.long_note.encode("utf-8")).hexdigest())

        # The static page contains no live nickname or note text; rendering happens via textContent.
        status, body, _ = self.request("/", token=False)
        self.assertNotIn(b"<img", body)
        self.assertNotIn("<script>alert".encode(), body)

    def test_private_scope_and_memory_versions(self):
        status, payload, _ = self.get_json("/api/person?id=202")
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["person"]["has_private"])
        status, payload, _ = self.get_json("/api/person?id=202&group=group%3A1")
        self.assertEqual(status, 404)
        status, payload, _ = self.get_json("/api/person?id=202&group=private%3A202")
        self.assertEqual(status, 200)
        status, payload, _ = self.get_json("/api/memories?person=202&group=private%3A202")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(payload["data"]["total"], 1)
        status, payload, _ = self.get_json("/api/memory?id=" + self.private_m)
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["text"], "私聊秘密内容")
        status, payload, _ = self.get_json("/api/memory?id=" + self.manual["id"])
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["text_kind"], "manual_original")
        self.assertEqual(payload["data"]["provider"], "gemini")
        self.assertIn("手写原文", payload["data"]["text"])


class ToolIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=here, prefix="v4_tool_")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        (self.base / "state/img_cache").mkdir(parents=True)
        import shutil
        shutil.copy2(repo_file("style_guide.md"), self.base / "style_guide.md")
        shutil.copy2(repo_file("group_profiles.json"), self.base / "group_profiles.json")
        (self.base / "config.json").write_text("{}", encoding="utf-8")
        self.now = time.time()
        self.store = MemoryStore(self.base)
        with self.store.db() as db:
            evidence = self.store.observe({
                "session_key": "group:100", "user_id": "22", "message_id": "21",
                "received_at": self.now - 5, "text": "@哈基米 聊了很久",
                "direct_targets": ["8415435122"], "self_id": "8415435122",
            })
            row = db.execute("SELECT id FROM records WHERE origin='21' LIMIT 1").fetchone()
            self.evidence_id = "M" + str(row[0])
        payload = {"session_key": "group:100", "created_at": self.now,
                   "recent_context_messages": [{"session_key": "group:100", "message_id": "21",
                                                "user_id": "22", "text": "聊了很久", "nickname": "成员"}]}
        from hakimi.bridge.turn_coordinator import TurnCoordinator
        self.coordinator = TurnCoordinator(self.base)
        self.coordinator.publish(payload)
        self.claim = self.coordinator.claim_primary()
        from hakimi.prompt.prompt_context import activate_context
        activate_context(self.claim["payload"], self.base, claim=self.claim)
        from hakimi.tools import qq_cli
        self.qq_cli = qq_cli
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, value in {"BASE_DIR": self.base, "STATE_DIR": self.base / "state",
                            "SESSION_MEMORY_FILE": self.base / "state/session_memory.jsonl",
                            "OUTBOX_FILE": self.base / "state/outbox.jsonl"}.items():
            stack.enter_context(patch.object(qq_cli, name, value))

    def call(self, name, **data):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.qq_cli.cmd_call(SimpleNamespace(call_name=name, session="group:100",
                                                 turn=self.claim["token"], **data))
        return json.loads(output.getvalue())["result"]

    def test_social_tool_receipt_retry_and_old_tool(self):
        from hakimi.prompt.tool_catalog import BY_NAME, brief
        self.assertIn("memory.social", BY_NAME)
        self.assertIn("memory.remember", BY_NAME)
        self.assertIn("memory.social", brief())
        note = "  印象原文\n第二行 ✨  "
        first = self.call("memory.social", note=note, target_id="22",
                          relationship_type="公开搭话的群友", affinity=70, closeness=None,
                          confidence=80, evidence_memory_ids=self.evidence_id)
        self.assertTrue(first["saved"])
        self.assertEqual(first["provenance"], "gemini_manual")
        self.assertEqual(first["hash"], hashlib.sha256(note.encode("utf-8")).hexdigest())
        second = self.call("memory.social", note=note, target_id="22",
                           relationship_type="公开搭话的群友", affinity=70, closeness=None,
                           confidence=80, evidence_memory_ids=self.evidence_id)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["memory_id"], first["memory_id"])
        with self.store.db() as db:
            original = db.execute("SELECT text,provider FROM memory_originals WHERE record_id=?",
                                  (first["record_id"],)).fetchone()
            self.assertEqual(original[0], note)
            self.assertEqual(original[1], "gemini")
            self.assertEqual(db.execute("SELECT count(*) FROM social_assessments").fetchone()[0], 1)
        old = self.call("memory.remember", kind="commitment", key="旧接口", note="旧接口仍然可用")
        self.assertTrue(old["saved"])
        self.assertTrue(old["created"])
        with self.assertRaises(Exception):
            self.call("memory.social", note="越界分数", target_id="22", affinity=101,
                      evidence_memory_ids=self.evidence_id)

    def test_dsh_context_preserves_author_in_social_memory(self):
        with patch.object(self.qq_cli, "current_context", return_value={"provider": "dsh"}):
            receipt = self.call("memory.social", note="辅助整理的关系印象", target_id="22",
                                evidence_memory_ids=self.evidence_id)
        self.assertEqual(receipt["provenance"], "dsh_assisted")
        with self.store.db() as db:
            row = db.execute("SELECT provider FROM memory_originals WHERE record_id=?",
                             (receipt["record_id"],)).fetchone()
            self.assertEqual(row[0], "dsh")

    def test_prompt_guidance_mentions_social_without_claiming_automation(self):
        from hakimi.prompt.prompt_context import render_prompt
        prompt = render_prompt({"session_key": "group:100", "recent_context_messages": [
            {"session_key": "group:100", "message_id": "1", "user_id": "22", "text": "记住这个"}]}, self.base)
        self.assertIn("memory.social", prompt)
        self.assertIn("不每轮打分", prompt)
        self.assertIn("只有收到成功回执", prompt)
        self.assertIn("不是对方内心", prompt)


if __name__ == "__main__":
    unittest.main()
