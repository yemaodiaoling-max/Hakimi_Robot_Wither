"""Unit tests for impression forgetting and consolidation mechanism ("核心长留、琐事随忘")."""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from hakimi.memory.memory_store import store as store_factory
from hakimi.social.impression_forgetting import (
    clause_salience,
    compact_impression_text,
    dedupe_clauses,
    is_boilerplate_clause,
    split_clauses,
)
from hakimi.social.social_fields import _append_impression_text
from hakimi.social.social_record import record_social


class TestImpressionForgetting(unittest.TestCase):

    def test_split_clauses(self):
        text = "我是猫猫。他是程序员！喜欢吃鲷鱼烧；昨晚还熬夜了\n今天又来找猫猫大人玩？"
        clauses = split_clauses(text)
        self.assertEqual(len(clauses), 5)
        self.assertEqual(clauses[0], "我是猫猫。")
        self.assertEqual(clauses[1], "他是程序员！")
        self.assertEqual(clauses[2], "喜欢吃鲷鱼烧；")
        self.assertEqual(clauses[3], "昨晚还熬夜了")
        self.assertEqual(clauses[4], "今天又来找猫猫大人玩？")

        # Empty / whitespace handling
        self.assertEqual(split_clauses(""), [])
        self.assertEqual(split_clauses("   \n  "), [])

    def test_boilerplate_stripping(self):
        boilerplates = [
            "引用对方关于分饭、群聊热闹和AI味的发言，用玩笑回应。",
            "向对方交流游戏配置、作品话题，并给出肯定评价。",
            "多次公开@对方，其中一次询问图里的人是谁。",
            "当天多次公开@对方，其中一次询问图里的人是谁。",
            "近期有新的互动细节，顺手在小本子里把印象和好感基明确下来。",
            "近期有新的互动细节，顺手在小本子里把印象和好感基准明确下来。",
            "多次围绕游戏内容请对方检查、给答案，也有轻微催促和调侃。",
            "在群里交流挺积极的群友，发言很有个人特色，相处挺舒服的。",
            "曾向对方回复话题，也曾在发图时提及对方。",
            "就游戏剧情走向多次追问并质疑对方的推论。",
        ]
        for bp in boilerplates:
            self.assertTrue(is_boilerplate_clause(bp), f"Expected boilerplate: {bp}")

        genuine = "他是程序员，平时总是很温柔。约定一起去吃鲷鱼烧。"
        self.assertFalse(is_boilerplate_clause(genuine))

        # Test compaction strips boilerplate prefix
        raw = "向对方交流游戏配置、作品话题，并给出肯定评价。 近期有新的互动细节，顺手在小本子里把印象和好感基明确下来。性格温和有担当，日常爱写代码。"
        cleaned = compact_impression_text(raw)
        self.assertNotIn("向对方交流", cleaned)
        self.assertNotIn("小本子", cleaned)
        self.assertIn("性格温和有担当", cleaned)
        self.assertIn("日常爱写代码", cleaned)

        # Safety: text with only boilerplate clauses should not return empty
        only_bp = "在群里交流挺积极的群友，发言很有个人特色，相处挺舒服的。"
        self.assertTrue(len(compact_impression_text(only_bp)) > 0)

    def test_clause_salience(self):
        # Core clauses
        t_core1, s_core1 = clause_salience("他是物理学方向的博士，平时总是在做科研建模。", 0, 4)
        self.assertEqual(t_core1, "core")
        self.assertGreaterEqual(s_core1, 3.0)

        t_core2, s_core2 = clause_salience("性格细腻温柔，经常叫我猫猫大人，约定一起去吃鲷鱼烧。", 1, 4)
        self.assertEqual(t_core2, "core")
        self.assertGreaterEqual(s_core2, 3.0)

        # First clause anchor rule
        t_first, s_first = clause_salience("在群里是一个特别靠谱的前辈。", 0, 3)
        self.assertEqual(t_first, "core")
        self.assertGreaterEqual(s_first, 3.0)

        # Ephemeral clauses
        t_eph1, s_eph1 = clause_salience("今天在群里问图是啥意思。", 1, 4, age_days=0.0)
        self.assertEqual(t_eph1, "ephemeral")
        self.assertTrue(0.5 <= s_eph1 <= 1.8)

        # Ephemeral time decay
        t_eph_old, s_eph_old = clause_salience("昨晚十七集中二短剧猜绿毛。", 1, 4, age_days=7.0)
        self.assertEqual(t_eph_old, "ephemeral")
        self.assertLess(s_eph_old, s_eph1)

        # Core clauses do NOT decay with age
        _, s_core_aged = clause_salience("性格细腻温柔，经常叫我猫猫大人，约定一起去吃鲷鱼烧。", 1, 4, age_days=10.0)
        self.assertEqual(s_core2, s_core_aged)

    def test_dedupe_clauses(self):
        c1 = "经常在群里聊量子计算的具体问题。"
        c2 = "会在群里聊量子计算/离子阱学习中的具体问题，遇到不懂的会追问。"
        deduped = dedupe_clauses([c1, c2])
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0], c2)  # keeps the more informative one

        c3 = "喜欢吃鲷鱼烧。"
        c4 = "爱吃鲷鱼烧。"
        deduped2 = dedupe_clauses([c3, c4])
        self.assertEqual(len(deduped2), 1)
        self.assertEqual(deduped2[0], c4)  # similar length, keeps newer

    def test_core_vs_ephemeral_forgetting_on_overflow(self):
        # Construct long text exceeding 360 chars
        anchor = "主人在群里是一个特别温柔的同伴，平时总是默默投喂群友128罐头。"  # core anchor
        trait = "性格嘴硬心软有担当，日常爱看考据论文，约定一起完成任务。"  # middle core trait
        tail1 = "最近又一起讨论了声学模型微调。"  # recent tail 1
        tail2 = "约定下周接着实测数据表现。"  # recent tail 2

        # Ephemeral trivia
        trivias = [
            "看群里聊滚木聊得热闹，还特意跑来轻声细语教猫猫新词汇、顺便查岗问猫猫在干嘛。",
            "群里别人聊起猫啃拖鞋睡脚边，又特地跑来问“你觉得他家的猫怎么样呀”，还损人家小心半夜拖鞋被塞猫砂。",
            "十七集中二短剧猜绿毛，还问图是啥意思，脑回路清奇爱整活。",
            "又拿AI深度思考憋出废话的猫耳四格漫画跑来戳我，天天变着法子找跟猫猫相关的槽点来逗我。",
            "被群友389拿雪球砸了还会委屈巴巴跑来找猫诉苦当和事佬，日常雷打不动地来跟猫问好。",
            "冷不丁好奇探听猫对其他群友的印象，顺带幸灾乐祸喊猫围观吃醋的肥鱼。",
            "下线前关机明天见，大清早又跑来问好，一天到晚把猫当自家贴心挂件使唤。",
            "随手抛来“你是尾大的猫猫嘛”这种可爱谐音烂梗，还特意问起星铁角色。",
            "发搞笑聊天截图让我鉴别人类，冷不丁凑过来问为什么不骂他。",
        ]

        full_text = "".join([anchor] + trivias + [trait, tail1, tail2])
        self.assertGreater(len(full_text), 400)

        compacted = compact_impression_text(full_text, max_chars=360, keep_recent=2)
        self.assertLessEqual(len(compacted), 360)

        # Core anchors and traits must be preserved
        self.assertIn("特别温柔的同伴", compacted)
        self.assertIn("群友128", compacted)
        self.assertIn("嘴硬心软有担当", compacted)
        self.assertIn("考据论文", compacted)

        # Recent window must be preserved
        self.assertIn("声学模型微调", compacted)
        self.assertIn("下周接着实测数据", compacted)

        # Stale trivial trivia must be forgotten
        self.assertNotIn("看群里聊滚木聊得热闹", compacted)
        self.assertNotIn("十七集中二短剧猜绿毛", compacted)

    def test_time_decayed_forgetting(self):
        anchor = "群友363是持续做原神剧情时间线推演的群友，性格严谨认真，平时总是一丝不苟。"
        trivias = [
            "昨天在群里连发三张截图问图是啥意思。",
            "大清早被卡住时特地跑来查岗问猫猫在干嘛。",
            "随手发搞笑聊天截图让我鉴别人类脑回路。",
            "最近还一口气发了好几张表情包求鉴定。",
        ]
        tail = "约定下周更新主线时间线预测。"
        text = "。".join([anchor] + trivias + [tail]) + "。"
        self.assertGreater(len(text), 120)

        # At age_days = 0.0, within budget -> kept
        fresh = compact_impression_text(text, max_chars=360, age_days=0.0)
        self.assertIn("鉴别人类", fresh)

        # At age_days = 7.0 (>= 5.0), if text > 220, decayed ephemeral trivia drop
        stale_long = "".join([anchor] + trivias * 3 + [tail])
        self.assertGreater(len(stale_long), 240)
        compacted = compact_impression_text(stale_long, max_chars=360, age_days=7.0)
        self.assertLessEqual(len(compacted), 220)
        self.assertIn("时间线推演", compacted)
        self.assertIn("约定下周", compacted)

    def test_append_impression_text(self):
        # 1. Short concatenation verbatim
        base = "他是程序员，平时总是很温柔。"
        extra = "今天送了金枪鱼罐头。"
        res = _append_impression_text(base, extra)
        self.assertEqual(res, base + extra)

        # 2. Boilerplate in base is cleaned on append
        bp_base = "在群里交流挺积极的群友，发言很有个人特色，相处挺舒服的。 随口抛出灵魂拷问，挺有意思。"
        res2 = _append_impression_text(bp_base, "今天又聊了量子物理。")
        self.assertNotIn("交流挺积极的群友", res2)
        self.assertIn("随口抛出灵魂拷问", res2)
        self.assertIn("今天又聊了量子物理", res2)

        # 3. Snowballing text exceeding max_chars gets compacted while keeping core + newest
        long_base = (
            "主人是特别温柔的同伴，平时总是默默投喂群友128罐头。"
            + "看群里聊滚木聊得热闹，特意跑来教新词汇。" * 5
            + "性格嘴硬心软有担当，日常爱看考据论文。"
        )
        new_obs = "约定下周一起去吃鲷鱼烧。"
        appended = _append_impression_text(long_base, new_obs, max_chars=360)
        self.assertLessEqual(len(appended), 360)
        self.assertIn("特别温柔的同伴", appended)
        self.assertIn("群友128", appended)
        self.assertIn("嘴硬心软有担当", appended)
        self.assertIn("约定下周一起去吃鲷鱼烧", appended)


class TestImpressionStoreConsolidation(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.base = Path(self.temp_dir)
        (self.base / "state").mkdir(parents=True)
        self.store = store_factory(str(self.base))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_consolidate_person_impressions(self):
        user_id = "8490003955764560144"
        now = time.time()

        with self.store.db() as db:
            # Insert a visible chat record so target is known
            db.execute(
                "INSERT INTO records(source_key, kind, session, user_id, key, text, at, expires, origin, source, state) "
                "VALUES ('sk_chat1', 'chat', 'group:100', ?, 'chat:1', '我是鲸鲸', ?, 0, 'orig1', 'user', 'active')",
                (user_id, now - 100),
            )
            db.execute(
                "INSERT INTO records(source_key, kind, session, user_id, key, text, at, expires, origin, source, state) "
                "VALUES ('sk_chat2', 'chat', 'group:200', ?, 'chat:2', '我也在二群', ?, 0, 'orig2', 'user', 'active')",
                (user_id, now - 90),
            )

        # Record social impression in group:100
        r1 = record_social(
            self.base,
            session="group:100",
            note="群友128日常爱跟我顶嘴互损，一边嘴硬吃白饭两不耽误，一边趴着竖耳朵。",
            target_id=user_id,
            now=now - 50,
        )
        self.assertTrue(r1["saved"])

        # Record social impression in group:200 via append
        r2 = record_social(
            self.base,
            session="group:200",
            note="今天在二群约定一起吃鲷鱼烧。",
            target_id=user_id,
            mode="append",
            now=now - 10,
        )
        self.assertTrue(r2["saved"])

        # Check that r2 superseded r1 across groups
        with self.store.db() as db:
            active_assessments = db.execute(
                "SELECT id, session, state, supersedes_id, superseded_by FROM social_assessments "
                "WHERE target_id=? AND state='active'",
                (user_id,),
            ).fetchall()
            self.assertEqual(len(active_assessments), 1)
            self.assertEqual(active_assessments[0]["id"], r2["assessment_id"])

            older_assessment = db.execute(
                "SELECT id, state, superseded_by FROM social_assessments WHERE id=?",
                (r1["assessment_id"],),
            ).fetchone()
            self.assertEqual(older_assessment["state"], "superseded")
            self.assertEqual(older_assessment["superseded_by"], r2["assessment_id"])

            # Check records
            active_records = db.execute(
                "SELECT id, session, state FROM records "
                "WHERE kind='person' AND user_id=? AND state='active'",
                (user_id,),
            ).fetchall()
            self.assertEqual(len(active_records), 1)
            self.assertEqual(active_records[0]["id"], r2["record_id"])

        # Artificially insert an orphaned active record in another group
        with self.store.db() as db:
            db.execute(
                "INSERT INTO records(source_key, kind, session, user_id, key, text, at, expires, origin, source, state) "
                "VALUES ('sk_orphaned', 'person', 'group:300', ?, '关系印象·伙伴·abc1234567', '旧的孤立记录', ?, 0, 'orig3', 'manual', 'active')",
                (user_id, now - 200),
            )

        # Artificially insert a bloated active social_assessment (> 360 chars) using remember_manual
        bloated_text = (
            "群友363是做原神剧情时间线推演的群友，性格严谨认真。"
            + "看群里聊滚木聊得热闹，特意跑来教新词汇。" * 20
            + "约定下周更新时间线预测。"
        )
        receipt = self.store.remember_manual(
            {
                "kind": "person",
                "note": bloated_text,
                "key": "关系印象·推演·xyz987",
                "user_id": "875118155",
            },
            session="group:100",
            now=now - 86400 * 2,
        )
        bloated_rid = int(receipt["record_id"])
        with self.store.db() as db:
            cursor = db.execute(
                "INSERT INTO social_assessments(request_id, subject_id, target_id, author, provider, provenance, "
                "impression, reason, relationship_type, evidence_record_ids, evidence_dates, evidence_times, "
                "expires_at, original_record_id, created_at, session, version, state) "
                "VALUES ('req_bloated', 'hakimi', '875118155', 'gemini', 'gemini', 'gemini_manual', "
                "?, '', '关系印象', '[]', '[]', '[]', 0, ?, ?, 'group:100', 1, 'active')",
                (bloated_text, bloated_rid, now - 86400 * 2),
            )
            bloated_aid = cursor.lastrowid

        # Snapshot memory_originals count and text before consolidation
        with self.store.db() as db:
            orig_before = db.execute("SELECT id, text, content_hash FROM memory_originals").fetchall()

        # Run consolidate_person_impressions
        stats = self.store.consolidate_person_impressions(now=now)
        self.assertGreater(stats["superseded_records"], 0)
        self.assertGreater(stats["compacted_assessments"], 0)

        # Verify Immutable Originals Rule: memory_originals completely untouched!
        with self.store.db() as db:
            orig_after = db.execute("SELECT id, text, content_hash FROM memory_originals").fetchall()
            self.assertEqual(len(orig_before), len(orig_after))
            for b, a in zip(orig_before, orig_after):
                self.assertEqual(b["id"], a["id"])
                self.assertEqual(b["text"], a["text"])
                self.assertEqual(b["content_hash"], a["content_hash"])

            # Verify bloated impression was compacted in social_assessments and records
            compacted_assessment = db.execute(
                "SELECT impression FROM social_assessments WHERE id=?", (bloated_aid,)
            ).fetchone()
            self.assertLessEqual(len(compacted_assessment["impression"]), 360)
            self.assertIn("时间线推演", compacted_assessment["impression"])
            self.assertIn("约定下周", compacted_assessment["impression"])

            compacted_record = db.execute(
                "SELECT text FROM records WHERE id=?", (bloated_rid,)
            ).fetchone()
            self.assertEqual(compacted_record["text"], compacted_assessment["impression"])


if __name__ == "__main__":
    unittest.main()
