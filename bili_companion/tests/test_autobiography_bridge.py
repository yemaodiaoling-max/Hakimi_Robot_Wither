"""Tests for Bilibili autobiography condensation, live presence, desktop-pet interest, and pruning."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import tempfile
import unittest

from bili_companion.domain import Evidence, Scope
from bili_companion.integrations.autobiography import (
    HIGH_TTL_SECONDS,
    LOW_TTL_SECONDS,
    NORMAL_TTL_SECONDS,
    build_autobiography_episodes,
    prune_bili_databases,
    write_autobiography_snapshot,
    write_presence_snapshot,
)
from bili_companion.integrations.qq import QQSource
from bili_companion.integrations.qq_interest import QQInterestSampler, imported_records
from bili_companion.integrations.service import LocalTransport, tick
from bili_companion.memory import MemoryStore
from bili_companion.runtime.storage import Database
from bili_companion.sync import SyncStore, SyncWorker


class AutobiographyBridgeTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[1] / "state" / "test_tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.root = Path(self.temp.name)
        self.now = 1_800_000_000.0
        self.native_path = self.root / "native_watch.sqlite3"
        self.watcher_path = self.root / "watch" / "watcher.sqlite3"
        self.qq_db_path = self.root / "qq.sqlite3"
        self.memory = MemoryStore(Database(self.native_path))
        self.watcher_db = Database(self.watcher_path)
        with self.watcher_db.connect(write=True) as db:
            db.executescript(
                """
                CREATE TABLE watcher_state (id INTEGER PRIMARY KEY, value TEXT);
                CREATE TABLE video_history (
                    bvid TEXT PRIMARY KEY, selected_at REAL, updated_at REAL,
                    selection_reason TEXT, observations INTEGER DEFAULT 0,
                    interest TEXT DEFAULT 'neutral', decision TEXT DEFAULT '',
                    summary TEXT DEFAULT '', coverage TEXT DEFAULT '[]',
                    evidence_ids TEXT DEFAULT '[]', status TEXT DEFAULT 'selected'
                );
                CREATE TABLE comment_receipts (
                    account TEXT, bvid TEXT, oid INTEGER, rpid INTEGER,
                    text TEXT, at REAL, visibility TEXT, checked_at REAL
                );
                CREATE TABLE danmaku_attempts (
                    account TEXT, room TEXT, session TEXT, dedup TEXT,
                    text TEXT, at REAL, state TEXT
                );
                """
            )
            db.execute(
                "INSERT INTO watcher_state VALUES (1, ?)",
                (json.dumps({
                    "state": "WATCHING_VIDEO",
                    "room_id": "8729095131",
                    "live_session": None,
                    "video": {"bvid": "BV1High12345", "title": "必胜客自助评测", "position": 75.0, "duration": 240.0, "observations": 3},
                }, ensure_ascii=False),),
            )

    def tearDown(self):
        self.temp.cleanup()

    def test_video_and_live_autobiography_tiers_and_noise_filter(self):
        # 1. High-interest video with comment -> 60d TTL
        # 2. Normal video -> 14d TTL
        # 3. Low-interest 2-obs video -> 5d TTL
        # 4. Low-interest 1-obs skipped video -> excluded
        with self.watcher_db.connect(write=True) as db:
            rows = [
                ("BV1High12345", self.now - 300, self.now - 100, "想看158元自助餐到底亏不亏", 3, "high", "continue",
                 "《必胜客自助评测》博主展示必胜客自助菜单，狂炫奶油蘑菇意面[0,1,2]。",
                 json.dumps([{"start": 0, "end": 60, "modality": "video"}, {"start": 0, "end": 60, "modality": "audio"}]),
                 "[]", "finished"),
                ("BV1Norm12345", self.now - 400, self.now - 200, "恢复既有视频；状态不代表已观察", 2, "neutral", "continue",
                 "《费米子传播子入门》主讲人推导格林函数在动量空间的极点[0,3]。",
                 json.dumps([{"start": 10, "end": 40, "modality": "video"}]),
                 "[]", "sampled"),
                ("BV1Low212345", self.now - 500, self.now - 300, "随手点进来看看", 2, "low", "skip",
                 "画面介绍一款普通鼠标垫材质[1]。",
                 json.dumps([{"start": 0, "end": 20, "modality": "video"}]),
                 "[]", "skipped"),
                ("BV1Skip12345", self.now - 600, self.now - 400, "无聊广告", 1, "low", "skip",
                 "纯广告切片[0]。",
                 json.dumps([{"start": 0, "end": 10, "modality": "video"}]),
                 "[]", "skipped"),
            ]
            db.executemany("INSERT INTO video_history VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            db.execute(
                "INSERT INTO comment_receipts VALUES (?,?,?,?,?,?,?,?)",
                ("hakimi-bilibili", "BV1High12345", 1, 2, "意面看着确实香但158还是肉疼", self.now - 90, "visible", self.now - 80),
            )
            db.execute(
                "INSERT INTO danmaku_attempts VALUES (?,?,?,?,?,?,?)",
                ("hakimi-bilibili", "8729095131", "8729095131:live1", "d1", "这波操作有点东西", self.now - 50, "sent"),
            )
            db.execute(
                "INSERT INTO danmaku_attempts VALUES (?,?,?,?,?,?,?)",
                ("hakimi-bilibili", "8729095131", "8729095131:live1", "d2", "城边治安这么差吗", self.now - 40, "sent"),
            )

        live_scope = Scope("hakimi-bilibili", "live_room", "8729095131")
        self.memory.append(Evidence("le1", live_scope, "8729095131:live1", "原神画面在枫丹水道里游来游去", self.now - 60, 0, 30, "video", "observation"))
        self.memory.append(Evidence("le2", live_scope, "8729095131:live1", "背景播放轻快弦乐并伴有角色台词", self.now - 30, 30, 60, "audio", "observation"))

        episodes = build_autobiography_episodes(self.native_path, self.watcher_path, now=self.now)
        by_key = {ep["key"]: ep for ep in episodes}

        self.assertNotIn("B站观看·BV1Skip12345", by_key)
        self.assertIn("B站观看·BV1High12345", by_key)
        self.assertIn("B站观看·BV1Norm12345", by_key)
        self.assertIn("B站观看·BV1Low212345", by_key)

        high_ep = by_key["B站观看·BV1High12345"]
        self.assertEqual(high_ep["importance"], "high")
        self.assertAlmostEqual(high_ep["expires_at"] - high_ep["occurred_at"], HIGH_TTL_SECONDS)
        self.assertIn("《必胜客自助评测》", high_ep["text"])
        self.assertIn("我留评：「意面看着确实香但158还是肉疼」", high_ep["text"])
        self.assertNotIn("[0,1,2]", high_ep["text"])
        self.assertIn("必胜客自助评测", high_ep["topics"])

        norm_ep = by_key["B站观看·BV1Norm12345"]
        self.assertAlmostEqual(norm_ep["expires_at"] - norm_ep["occurred_at"], NORMAL_TTL_SECONDS)
        self.assertNotIn("恢复既有视频", norm_ep["text"])

        low_ep = by_key["B站观看·BV1Low212345"]
        self.assertAlmostEqual(low_ep["expires_at"] - low_ep["occurred_at"], LOW_TTL_SECONDS)

        live_eps = [ep for ep in episodes if ep["kind"] == "live"]
        self.assertEqual(len(live_eps), 1)
        self.assertEqual(live_eps[0]["importance"], "high")
        self.assertIn("发了弹幕", live_eps[0]["text"])

        snap_path = self.root / "bili_autobiography.json"
        written = write_autobiography_snapshot(episodes, snap_path, now=self.now)
        self.assertEqual(written, len(episodes))
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
        self.assertEqual(snap["version"], 1)
        self.assertEqual(snap["source"], "bilibili")
        self.assertEqual(len(snap["checksum"]), 64)

        pres_path = self.root / "presence.json"
        pres = write_presence_snapshot(self.watcher_path, pres_path, now=self.now)
        self.assertEqual(pres["watching_now"]["bvid"], "BV1High12345")
        self.assertEqual(pres["watching_now"]["title"], "必胜客自助评测")
        self.assertLessEqual(pres_path.stat().st_size, 1800)

    def test_desktop_pet_interest_sanitized_and_no_self_loop(self):
        with closing(sqlite3.connect(self.qq_db_path)) as db, db:
            db.execute(
                """CREATE TABLE records(id INTEGER PRIMARY KEY,kind TEXT,session TEXT,
                   user_id TEXT,text TEXT,at REAL,source TEXT,shared INTEGER,state TEXT,
                   channel TEXT,model TEXT,expires REAL,key TEXT DEFAULT '')"""
            )
            # Safe public lore
            db.execute(
                "INSERT INTO records VALUES (1,'lore','group:1','','原神枫丹水下机制考据',?,'https://wiki.biligame.com/ys/a',1,'active','gemini','gemini',0,'考据')",
                (self.now - 100,),
            )
            # Desktop pet episode with private chat clause mixed in -> must strip private chat clause
            db.execute(
                "INSERT INTO records VALUES (2,'episode','private:10001','10001',?,?,?,0,'active','desktop_pet','hydrogen',0,'桌宠阶段脉络')",
                (
                    "[桌宠近期陪伴脉络] 我在主人电脑桌面上陪他经历：主人在推路径积分传播子公式。随后找群友317a甲私聊聊社科。最后打开B站看游戏视频。",
                    self.now - 50,
                    "桌宠10轮阶段整理摘要",
                ),
            )
            # Bilibili watched episode -> must NEVER be re-exported back to Bilibili
            db.execute(
                "INSERT INTO records VALUES (3,'episode','self:bilibili','SELF','[B站看片] 自己的记录',?,'https://www.bilibili.com/video/BV1x',1,'active','bilibili_watch','bili',0,'B站观看·BV1x')",
                (self.now - 20,),
            )

        qq_store = SyncStore(Database(self.root / "qq_side.sqlite3"), endpoint="qq", local_id="q", peer_id="b", clock=lambda: self.now)
        bili_store = SyncStore(Database(self.root / "bili_side.sqlite3"), endpoint="bilibili", local_id="b", peer_id="q", clock=lambda: self.now)

        src = QQSource(self.qq_db_path, qq_store, owner_private_session="private:10001")
        items = list(src.rows())
        ids = {i["source_id"] for i in items}
        self.assertEqual(ids, {"qq:M1", "qq:desktop-pet:M2"})
        pet_item = next(i for i in items if i["source_id"] == "qq:desktop-pet:M2")
        self.assertIn("路径积分传播子", pet_item["text"])
        self.assertNotIn("群友317a甲", pet_item["text"])
        self.assertNotIn("私聊", pet_item["text"])

        sampler = QQInterestSampler(self.qq_db_path, qq_store, rng=random.Random(7), owner_private_session="private:10001")
        worker = SyncWorker(qq_store, sampler, LocalTransport(bili_store, qq_store), interval_seconds=7200)
        res = worker.run_once(force=True)
        self.assertEqual(res["state"], "synced")
        imported = imported_records(bili_store)
        self.assertTrue(any(r["source_id"] == "qq:desktop-pet:M2" for r in imported))

    def test_prune_bili_databases_keeps_protected_and_deletes_stale(self):
        old_ts = self.now - 50 * 86400
        fresh_ts = self.now - 1 * 86400
        active_scope = Scope("hakimi-bilibili", "video", "BV1High12345")
        stale_scope = Scope("hakimi-bilibili", "video", "BV1VeryOld999")
        self.memory.append(Evidence("ev_active", active_scope, "s1", "当前活跃视频证据", old_ts, 0, 10, "video", "observation"))
        self.memory.append(Evidence("ev_stale", stale_scope, "s2", "五十天前的旧视频证据", old_ts, 0, 10, "video", "observation"))
        self.memory.append(Evidence("ev_fresh", stale_scope, "s3", "昨天的新证据", fresh_ts, 10, 20, "video", "observation"))
        self.memory.revise(stale_scope, "五十天前的旧笔记", ["ev_stale", "ev_fresh"], note_id="video-summary:BV1VeryOld999")
        # Add orphan note whose evidence doesn't exist after ev_stale is pruned -> create a second fully expired scope
        dead_scope = Scope("hakimi-bilibili", "video", "BV1DeadOnly99")
        self.memory.append(Evidence("ev_dead", dead_scope, "s4", "待删除旧证据", old_ts, 0, 10, "video", "observation"))
        self.memory.revise(dead_scope, "待删除旧笔记", ["ev_dead"], note_id="video-summary:BV1DeadOnly99")

        bridge_db = self.root / "bridge" / "bilibili_bridge.sqlite3"
        bili_store = SyncStore(Database(bridge_db), endpoint="bilibili", local_id="b", peer_id="q", clock=lambda: self.now)
        with bili_store.database.connect(write=True) as db:
            db.execute("INSERT INTO sync_outbox VALUES (?, 'pkt_old', 'payload', ?, 'acked', 1)", (bili_store.channel, self.now - 10))
            db.execute("INSERT INTO sync_outbox VALUES (?, 'pkt_live', 'payload', ?, 'pending', 0)", (bili_store.channel, self.now + 100))

        stats = prune_bili_databases(
            self.native_path,
            self.watcher_path,
            bridge_db,
            now=self.now,
            max_age_days=45.0,
            max_evidence=4000,
        )
        self.assertEqual(stats["deleted_aged_evidence"], 2)
        self.assertEqual(stats["deleted_notes"], 1)
        self.assertEqual(stats["deleted_outbox"], 1)

        with self.memory.database.connect() as db:
            ev_ids = {r[0] for r in db.execute("SELECT id FROM evidence").fetchall()}
            self.assertEqual(ev_ids, {"ev_active", "ev_fresh"})
            note_ids = {r[0] for r in db.execute("SELECT id FROM notes").fetchall()}
            self.assertEqual(note_ids, {"video-summary:BV1VeryOld999"})

        with bili_store.database.connect() as db:
            out_ids = {r[0] for r in db.execute("SELECT packet_id FROM sync_outbox").fetchall()}
            self.assertEqual(out_ids, {"pkt_live"})

    def test_tick_with_autobiography_and_presence_without_legacy_snapshot(self):
        with self.watcher_db.connect(write=True) as db:
            db.execute(
                "INSERT INTO video_history VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("BV1High12345", self.now - 100, self.now - 20, "想看评测", 2, "high", "continue",
                 "《必胜客自助评测》试吃意面。", json.dumps([{"start": 0, "end": 30, "modality": "video"}]), "[]", "finished"),
            )
        cfg = {
            "state_dir": str(self.root / "bridge"),
            "qq_identity": "hakimi-qq",
            "bilibili_identity": "hakimi-bilibili",
            "qq_database": str(self.qq_db_path),
            "bilibili_database": str(self.native_path),
            "watcher_database": str(self.watcher_path),
            "bilibili_account_id": "hakimi-bilibili",
            "autobiography_snapshot": str(self.root / "bili_autobiography.json"),
            "presence_snapshot": str(self.root / "hakimi_presence.json"),
        }
        with closing(sqlite3.connect(self.qq_db_path)) as db, db:
            db.execute(
                """CREATE TABLE records(id INTEGER PRIMARY KEY,kind TEXT,session TEXT,
                   user_id TEXT,text TEXT,at REAL,source TEXT,shared INTEGER,state TEXT,
                   channel TEXT,model TEXT,expires REAL)"""
            )
        res = tick(cfg, force=True, clock=lambda: self.now)
        self.assertEqual(res["bilibili"]["state"], "skipped_autobiography")
        self.assertEqual(res["autobiography_episodes"], 1)
        self.assertTrue(res["presence_updated"])
        self.assertTrue((self.root / "bili_autobiography.json").is_file())
        self.assertTrue((self.root / "hakimi_presence.json").is_file())


if __name__ == "__main__":
    unittest.main()
