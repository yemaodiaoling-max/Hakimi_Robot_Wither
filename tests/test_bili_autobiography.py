"""Tests for Bilibili autobiography ingestion, content-triggered recall, QQ gating, and physical pruning."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from hakimi.features import diary_daily as diary
from hakimi.memory import bili_autobiography as bili_auto
from hakimi.memory.memory_store import MemoryStore
from hakimi.prompt.prompt_context import build_context
from hakimi.prompt.prompt_language import context as positive_context


def _write_autobiography(base: Path, episodes: list[dict], *, generated_at: float) -> Path:
    body = json.dumps(episodes, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    snap = {
        "version": 1,
        "source": "bilibili",
        "generated_at": generated_at,
        "checksum": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "episodes": episodes,
    }
    target = base / "state" / "bili_autobiography.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    return target


class BiliAutobiographyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        (self.base / "state").mkdir(parents=True, exist_ok=True)
        self.now = datetime(2026, 11, 23, 20, 0, tzinfo=diary.ZONE).timestamp()

    def test_regular_live_room_uses_official_id_and_gates_unrelated_chat(self):
        path = self.base / "regular_live_room.json"
        def check(text, uid, **fields):
            return bili_auto.regular_live_room_context(dict(text=text, user_id=uid, **fields), path)
        self.assertIsNone(check("平时看谁直播", "account-A"))
        path.write_text(json.dumps(dict(room_id="1234", host_name="测试主播", qq_official_id="account-A",
                                        observation_note="Test note")), encoding="utf-8")
        owner = check("我今天开播了", "account-A")
        self.assertTrue(owner["current_sender_is_room_owner"])
        self.assertNotIn("account-A", json.dumps(owner))
        self.assertIsNone(check("我今天开播了", "account-B", nickname="测试主播"))
        self.assertIsNone(check("明天晚上吃什么", "account-A"))
        self.assertIsNone(check("12345号房间", "account-B"))
        self.assertIsNone(check("别人的直播间", "account-B"))
        self.assertIsNone(check("我每天去别人的直播间", "account-B"))
        for text in ("1234号房间", "每天去的那个直播间是谁的", "测试主播开播了"):
            context = check(text, "account-B")
            self.assertEqual(context["room_owner"], "测试主播")
            self.assertNotIn("current_sender_is_room_owner", context)
        data = {"session_key": "group:888", "recent_context_messages": [
            {"message_id": "a", "user_id": "account-A", "text": "我今天开播了", "received_at": self.now - 1}]}
        with patch("hakimi.paths.persona_overrides", return_value=self.base):
            prompt = positive_context(build_context(data, base=self.base, now=self.now))
        self.assertEqual(prompt["regular_live_room"], owner)

    def test_ingest_in_place_revision_and_content_recall(self):
        ep1 = {
            "kind": "video",
            "key": "B站观看·BV1PizzaTest",
            "text": "[B站看片] 《必胜客自助评测》(BV1PizzaTest，已看0~60s音画)：想看158元值不值；博主疯狂勾选奶油蘑菇意面和披萨；我留评：「意面看着好香」",
            "source": "https://www.bilibili.com/video/BV1PizzaTest",
            "evidence": "bilibili:video:BV1PizzaTest (0~60s音画)",
            "topics": ["B站", "视频", "必胜客自助评测", "奶油蘑菇意面"],
            "occurred_at": self.now - 120,
            "expires_at": self.now + 60 * 86400,
            "importance": "high",
            "revision": 1,
        }
        _write_autobiography(self.base, [ep1], generated_at=self.now - 10)
        store = MemoryStore(self.base)
        with patch("hakimi.memory.bili_autobiography.time.time", return_value=self.now):
            res1 = bili_auto.sync_bili_autobiography(store, now=self.now)
        self.assertTrue(res1["synced"])
        self.assertEqual(res1["inserted"], 1)

        with store.db() as db:
            row1 = db.execute("SELECT id, expires FROM records WHERE key='B站观看·BV1PizzaTest'").fetchone()
            self.assertIsNotNone(row1)
            rid1 = row1["id"]

        # Update the same BV with a richer text -> updates in-place keeping the same record ID
        ep1_v2 = dict(
            ep1,
            text="[B站看片] 《必胜客自助评测》(BV1PizzaTest，已看0~120s音画)：想看158元值不值；博主吃奶油蘑菇意面还点了一大盘烤鸡翅；我留评：「烤鸡翅也划算」",
            occurred_at=self.now - 60,
            revision=2,
        )
        _write_autobiography(self.base, [ep1_v2], generated_at=self.now)
        res2 = bili_auto.sync_bili_autobiography(store, now=self.now)
        self.assertTrue(res2["synced"])
        self.assertEqual(res2["updated"], 1)
        with store.db() as db:
            row2 = db.execute("SELECT id, text FROM records WHERE key='B站观看·BV1PizzaTest'").fetchone()
            self.assertEqual(row2["id"], rid1)
            self.assertIn("烤鸡翅", row2["text"])

        # Content-triggered natural recall across group session
        hits = store.retrieve(
            "group:999",
            [{"user_id": "12345", "text": "必胜客自助里面的奶油蘑菇意面和烤鸡翅好吃吗"}],
            {"user_id": "12345", "text": "必胜客自助里面的奶油蘑菇意面和烤鸡翅好吃吗"},
            now=self.now,
        )
        self.assertTrue(any("B站亲历" in h and "烤鸡翅" in h for h in hits))
        self.assertTrue(all("来源：self:bilibili" not in h for h in hits))
        self.assertTrue(any("https://www.bilibili.com/video/BV1PizzaTest" in h for h in hits))

        # Unrelated group chat does not spuriously match the pizza video
        unrelated = store.retrieve(
            "group:999",
            [{"user_id": "12345", "text": "今晚高数线代作业第三题怎么证明"}],
            {"user_id": "12345", "text": "今晚高数线代作业第三题怎么证明"},
            now=self.now,
        )
        self.assertFalse(any("B站亲历" in h for h in unrelated))

    def test_qq_prompt_gating_and_token_budget(self):
        ep = {
            "kind": "video",
            "key": "B站观看·BV1Docs1234",
            "text": "[B站看片] 《何以当归2》(BV1Docs1234，已看0~90s音画)：食贫道讲台湾现状与年轻人生活",
            "source": "https://www.bilibili.com/video/BV1Docs1234",
            "evidence": "bilibili:video:BV1Docs1234 (0~90s音画)",
            "topics": ["B站", "视频", "何以当归2", "食贫道"],
            "occurred_at": self.now - 100,
            "expires_at": self.now + 14 * 86400,
            "importance": "normal",
            "revision": 1,
        }
        _write_autobiography(self.base, [ep], generated_at=self.now)
        pres_path = self.base / "state" / "hakimi_presence.json"
        pres_path.write_text(
            json.dumps({
                "version": 1,
                "updated_at": self.now - 30,
                "watching_now": {"kind": "video", "bvid": "BV1Docs1234", "title": "何以当归2纪录片长标题测试一下截断"},
                "recent_watched": [
                    {"bvid": "BV1Other111", "title": "必胜客自助餐一五八元实测"},
                    {"bvid": "BV1Other222", "title": "原神水下遗迹剧情"},
                ],
                "recent_danmaku": [],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        # Write a decoy legacy snapshot that must be ignored
        (self.base / "state" / "bili_bridge_context.json").write_text(
            json.dumps({"version": 1, "source": "bilibili", "generated_at": self.now, "checksum": "bad", "records": []}),
            encoding="utf-8",
        )

        # Avoid picking up VPS /opt/hakimi/bili_companion/state/presence.json during unit test
        with patch.object(bili_auto, "DEFAULT_PRESENCE_PATHS", ()), \
             patch("hakimi.memory.bili_autobiography.time.time", return_value=self.now):
            # 1. Ordinary unrelated chat: 0 presence, 0 experience hint
            p_plain = {
                "session_key": "group:888",
                "recent_context_messages": [
                    {"message_id": "m1", "user_id": "111", "nickname": "群友A", "text": "今天微积分作业好多", "received_at": self.now - 5}
                ],
            }
            ctx_plain = build_context(p_plain, base=self.base, now=self.now)
            self.assertNotIn("bili_presence", ctx_plain)
            self.assertNotIn("bili_experience_hint", ctx_plain)

            # 2. Group chat mentioning video content -> recalls B站亲历 in scoped_memory_data + hint, no presence
            p_content = {
                "session_key": "group:888",
                "recent_context_messages": [
                    {"message_id": "m2", "user_id": "111", "nickname": "群友A", "text": "食贫道的何以当归2讲了台湾生活吗", "received_at": self.now - 4}
                ],
            }
            ctx_content = build_context(p_content, base=self.base, now=self.now)
            self.assertTrue(any("B站亲历" in line for line in ctx_content["scoped_memory_data"]))
            self.assertIn("bili_experience_hint", ctx_content)
            self.assertNotIn("bili_presence", ctx_content)

            # 3. Group chat asking what Hakimi is watching -> injects <=135 char presence that survives positive_context
            p_ask = {
                "session_key": "group:888",
                "recent_context_messages": [
                    {"message_id": "m3", "user_id": "111", "nickname": "群友A", "text": "哈基米你最近在看啥视频呢", "received_at": self.now - 3}
                ],
            }
            ctx_ask = build_context(p_ask, base=self.base, now=self.now)
            self.assertIn("bili_presence", ctx_ask)
            wire = json.dumps(ctx_ask["bili_presence"], ensure_ascii=False, separators=(",", ":"))
            self.assertLessEqual(len(wire), 135)
            cleaned = positive_context(ctx_ask)
            self.assertIn("bili_presence", cleaned)

            # 4. Owner turn -> always gets gated bili_presence
            p_owner = {
                "session_key": "group:888",
                "recent_context_messages": [
                    {"message_id": "m4", "user_id": "8112866014149232024", "nickname": "主人", "text": "我困了", "received_at": self.now - 2}
                ],
            }
            ctx_owner = build_context(p_owner, base=self.base, now=self.now)
            self.assertIn("bili_presence", ctx_owner)

    def test_physical_pruning_by_ttl_and_cap(self):
        # Configure cap=50 in config.json and create 55 records (3 expired + 52 live)
        (self.base / "config.json").write_text(json.dumps({"memory_policy": {"bilibili_watch_limit": 50}}))
        episodes = []
        for i in range(3):
            episodes.append({
                "kind": "video",
                "key": f"B站观看·BV1Exp{i:06d}",
                "text": f"[B站看片] 《过期视频{i}》(BV1Exp{i:06d})：已经过期",
                "source": f"https://www.bilibili.com/video/BV1Exp{i:06d}",
                "evidence": "bilibili:video:exp",
                "topics": ["B站", f"过期{i}"],
                "occurred_at": self.now - 20 * 86400,
                "expires_at": self.now + 10,  # Will expire after self.now += 20
                "importance": "low",
                "revision": 1,
            })
        for i in range(52):
            high = i < 5
            episodes.append({
                "kind": "video",
                "key": f"B站观看·BV1Act{i:06d}",
                "text": f"[B站看片] 《有效视频{i}》(BV1Act{i:06d})：内容测试{i}",
                "source": f"https://www.bilibili.com/video/BV1Act{i:06d}",
                "evidence": "bilibili:video:act",
                "topics": ["B站", f"话题{i}"],
                "occurred_at": self.now - (1000 - i),
                "expires_at": self.now + (60 * 86400 if high else 14 * 86400),
                "importance": "high" if high else "normal",
                "revision": 1,
            })
        _write_autobiography(self.base, episodes, generated_at=self.now)
        store = MemoryStore(self.base)
        bili_auto.sync_bili_autobiography(store, now=self.now)
        with store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records WHERE channel='bilibili_watch'").fetchone()[0], 55)

        # Advance time past the 3 expired records and run prune
        future = self.now + 20
        store.prune(now=future)
        with store.db() as db:
            rem_rows = db.execute("SELECT id, key FROM records WHERE channel='bilibili_watch'").fetchall()
            self.assertEqual(len(rem_rows), 50)
            rem_keys = {r["key"] for r in rem_rows}
            # All 3 expired are gone; all 5 high-importance survive; 2 oldest normal were capped out
            for i in range(3):
                self.assertNotIn(f"B站观看·BV1Exp{i:06d}", rem_keys)
            for i in range(5):
                self.assertIn(f"B站观看·BV1Act{i:06d}", rem_keys)
            # No dangling FTS or edges for deleted records
            live_ids = {r["id"] for r in rem_rows}
            edge_rids = {r[0] for r in db.execute("SELECT DISTINCT record_id FROM edges").fetchall()}
            self.assertTrue(edge_rids <= live_ids)

    def test_diary_material_and_desktop_pet_integration(self):
        day = "2026-11-23"
        ep = {
            "kind": "video",
            "key": "B站观看·BV1Diary123",
            "text": "[B站看片] 《费米子传播子》(BV1Diary123，已看0~60s音画)：讲路径积分推费米子传播子",
            "source": "https://www.bilibili.com/video/BV1Diary123",
            "evidence": "bilibili:video:BV1Diary123",
            "topics": ["B站", "费米子传播子"],
            "occurred_at": self.now - 120,
            "expires_at": self.now + 14 * 86400,
            "importance": "normal",
            "revision": 1,
        }
        runtime = self.base / "official_test_runtime"
        (runtime / "state").mkdir(parents=True, exist_ok=True)
        _write_autobiography(runtime, [ep], generated_at=self.now)
        (runtime / "state" / "hakimi_presence.json").write_text(
            json.dumps({
                "version": 1,
                "updated_at": self.now - 10,
                "watching_now": {"kind": "live", "room_id": "8729095131"},
                "recent_watched": [],
                "recent_danmaku": [],
            }),
            encoding="utf-8",
        )
        store = MemoryStore(runtime)
        bili_auto.sync_bili_autobiography(store, now=self.now)

        with patch.object(diary, "ROOT", self.base), \
             patch.object(diary.time, "time", return_value=self.now):
            blocks = diary.diary_material(day)
        self.assertTrue(any("B站亲历" in b["scope"] for b in blocks))
        bili_block = next(b for b in blocks if "B站亲历" in b["scope"])
        self.assertIn("费米子传播子", bili_block["messages"][0]["text"])

        import desktop_pet_agent as pet_mod
        with patch.object(pet_mod.paths, "runtime", return_value=runtime), \
             patch.object(pet_mod, "get_memory_store", return_value=store), \
             patch.object(bili_auto, "DEFAULT_PRESENCE_PATHS", ()), \
             patch("time.time", return_value=self.now):
            pack = pet_mod._retrieve_initial_context("费米子传播子")
            self.assertEqual(pack["bili_presence"]["watching_now"]["room_id"], "8729095131")
            tool_res = pet_mod._execute_server_tool("bili_status", {"limit": 3})
            self.assertTrue(tool_res["ok"])
            self.assertEqual(tool_res["presence"]["watching_now"]["room_id"], "8729095131")
            self.assertEqual(len(tool_res["recent_episodes"]), 1)


if __name__ == "__main__":
    unittest.main()
