"""Unit tests for constant-water-level rolling context and vertical/horizontal memory协同系统."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from hakimi.memory.rolling_context import (
    advance_rolling_window,
    build_memory_axes,
    consolidate_evicted_buffer,
    extract_active_topics,
    is_commitment_signal,
    is_conclusion_signal,
    is_preference_signal,
    load_rolling_state,
    relative_label,
    save_rolling_state,
    STEADY_WINDOW_LIMIT,
)
from hakimi.memory.memory_store import MemoryStore
from hakimi.prompt.prompt_context import build_context


class RollingContextTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        (self.base / "state").mkdir(parents=True, exist_ok=True)
        (self.base / "style_guide.md").write_text("自然短句回复", encoding="utf-8")
        self.store = MemoryStore(self.base)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_signal_detection_accuracy(self):
        # Commitment signals
        self.assertTrue(is_commitment_signal("明天下午三点在图书馆不见不散"))
        self.assertTrue(is_commitment_signal("记得周末提醒我买车票"))
        self.assertTrue(is_commitment_signal("打算下周一起去吃烤肉"))
        self.assertFalse(is_commitment_signal("今天天气很好"))

        # Preference signals
        self.assertTrue(is_preference_signal("我喜欢吃辣的火锅"))
        self.assertTrue(is_preference_signal("我最近在研究天文学"))
        self.assertTrue(is_preference_signal("我是物理学专业的"))
        self.assertFalse(is_preference_signal("你好哈基米"))

        # Conclusion signals
        self.assertTrue(is_conclusion_signal("方案决定了，采用方案B"))
        self.assertTrue(is_conclusion_signal("结论是这个公式完全成立"))
        self.assertFalse(is_conclusion_signal("随便聊聊"))

    def test_rolling_window_steady_size_and_eviction(self):
        session = "group:10001"
        now = 1731200000.0

        # Round 1: 8 messages (less than limit 12)
        candidates_r1 = [
            {"message_id": f"msg_{i}", "user_id": f"user_{i % 3}", "nickname": f"群友{i % 3}",
             "text": f"对话内容_{i}", "received_at": now + i * 10}
            for i in range(1, 9)
        ]
        target_r1 = candidates_r1[-1]

        steady_r1, state_r1 = advance_rolling_window(
            self.base, session, candidates_r1, candidates_r1, target_r1, store=self.store, now=now + 100
        )
        self.assertEqual(len(steady_r1), 8)
        self.assertEqual(len(state_r1["evicted_buffer"]), 0)
        self.assertEqual(len(state_r1["just_scrolled_out"]), 0)

        # Round 2: Add 8 more messages (total 16 incoming). Steady window should cap at STEADY_WINDOW_LIMIT (12)
        candidates_r2 = candidates_r1 + [
            {"message_id": f"msg_{i}", "user_id": f"user_{i % 3}", "nickname": f"群友{i % 3}",
             "text": f"新对话内容_{i}", "received_at": now + i * 10}
            for i in range(9, 17)
        ]
        target_r2 = candidates_r2[-1]

        steady_r2, state_r2 = advance_rolling_window(
            self.base, session, candidates_r2, candidates_r2, target_r2, store=self.store, now=now + 200
        )
        # Window size remains steady at 12!
        self.assertEqual(len(steady_r2), STEADY_WINDOW_LIMIT)
        # The oldest 4 messages (msg_1, msg_2, msg_3, msg_4) were evicted!
        self.assertEqual([m["message_id"] for m in steady_r2], [f"msg_{i}" for i in range(5, 17)])
        self.assertTrue(len(state_r2["just_scrolled_out"]) > 0)
        self.assertTrue(all(line.startswith("[刚滑出]") for line in state_r2["just_scrolled_out"]))

    def test_evicted_buffer_automatic_persistence_and_relay_summary(self):
        session = "group:20002"
        now = 1731200000.0

        # Create evicted messages containing high value signals
        evicted = [
            {"message_id": "e1", "user_id": "1001", "sender_name": "主人",
             "text": "明天下午两点在老地方不见不散，记得带上资料", "timestamp": now - 300},
            {"message_id": "e2", "user_id": "1002", "sender_name": "群友132",
             "text": "好呀，我最近在看天体物理学，最喜欢双星系统了", "timestamp": now - 200},
            {"message_id": "e3", "user_id": "1003", "sender_name": "肥鱼",
             "text": "讨论了半天，结论是方案A更靠谱，定下来了", "timestamp": now - 100},
            {"message_id": "e4", "user_id": "1001", "sender_name": "主人",
             "text": "那我们按照这个安排执行", "timestamp": now - 50},
        ]

        new_summary, rem_buffer, saved = consolidate_evicted_buffer(
            evicted, old_summary="之前在讨论周末计划", store=self.store, session=session, active_topics=["天体物理", "安排"], now=now
        )

        # High value signals automatically persisted
        self.assertGreaterEqual(saved, 2)
        # Summary synthesized deterministically and concisely
        self.assertTrue(len(new_summary) > 0)
        self.assertLessEqual(len(new_summary), 220)
        self.assertIn("前情", new_summary)

        # Check records in store
        with self.store.db() as db:
            records = db.execute("SELECT * FROM records WHERE session=? AND state='active'", (session,)).fetchall()
            kinds = {r["kind"] for r in records}
            self.assertIn("commitment", kinds)

    def test_short_query_context_enrichment_prevents_amnesia(self):
        session = "group:30003"
        now = 1731200000.0

        # Preload a memory about quantum physics
        self.store.remember_manual(
            {
                "note": "主人：在复习量子力学薛定谔方程波函数推导",
                "kind": "episode",
                "user_id": "5001",
                "key": "量子力学推导",
                "evidence": "群聊原话",
                "topics": ["量子力学", "薛定谔方程"],
            },
            session=session,
            provider="test",
            now=now - 3600,
        )

        # Immediate context talking about quantum mechanics
        messages = [
            {"message_id": "m1", "user_id": "5001", "text": "刚刚看完了量子力学的第二章"},
            {"message_id": "m2", "user_id": "SELF", "text": "薛定谔方程的推导怎么样了？"},
            {"message_id": "m3", "user_id": "5001", "text": "正在整理算符对易关系"},
        ]
        # User follows up with an indexical / short sentence: "然后呢？"
        target = {"user_id": "5001", "text": "然后呢？"}

        # Without enrichment, "然后呢？" would match zero keywords.
        # With our multi-turn enrichment, it combines messages[-3:] and successfully recalls!
        recalled = self.store.retrieve(session, messages, target, now=now, limit=4, budget=1100)
        self.assertTrue(len(recalled) > 0, "Short query should be enriched and successfully recall context memory")
        self.assertTrue(any("量子力学" in r for r in recalled))

    def test_relative_time_labels(self):
        now = 1731200000.0
        self.assertEqual(relative_label(now - 30, now=now), "刚才")
        self.assertEqual(relative_label(now - 900, now=now), "15分钟前")
        self.assertIn("天前", relative_label(now - 86400 * 3, now=now))

    def test_build_memory_axes_structure(self):
        notes = [
            "[M101] 人物5001·称呼·记昨天 14:00：主人",
            "[M102] 约定·记今天 09:30：周末一起核对考据材料",
            "[M103] 日常·记刚才：讨论了挪德卡莱新月神像背景",
        ]
        rolling_bridge = {
            "rolling_summary": "之前聊到了提瓦特三月女神",
            "just_scrolled_out": ["[刚滑出] 主人: 那个设定很有意思"],
            "active_topics": ["挪德卡莱", "三月女神"],
        }
        axes = build_memory_axes(notes, rolling_bridge, [], {}, now=1731200000.0)

        self.assertIn("rolling_bridge", axes)
        self.assertIn("vertical_threads", axes)
        self.assertIn("horizontal_timeline", axes)

        vert = axes["vertical_threads"]
        self.assertEqual(len(vert["person_and_commitments"]), 2)  # M101 and M102
        self.assertEqual(len(vert["topics_and_episodes"]), 1)     # M103
        self.assertEqual(len(axes["horizontal_timeline"]), 3)

    def test_build_context_waterlevel_and_axial_memory_integration(self):
        session = "group:40004"
        now = 1731200000.0
        data = {
            "session_key": session,
            "recent_context_messages": [
                {"message_id": f"msg_{i}", "user_id": "1001", "nickname": "主人",
                 "text": f"群消息_{i}", "received_at": now + i * 10}
                for i in range(1, 16)
            ],
        }

        ctx = build_context(data, base=self.base, now=now + 500)
        # Context contains rolling_and_axial_memory
        self.assertIn("rolling_and_axial_memory", ctx)
        axial = ctx["rolling_and_axial_memory"]
        self.assertIn("rolling_bridge", axial)
        self.assertIn("vertical_threads", axial)
        self.assertIn("horizontal_timeline", axial)

        # Recent messages in context stay within steady window size (~12)
        recent_in_prompt = ctx["recent_context_messages_untrusted"]
        self.assertLessEqual(len(recent_in_prompt), 13)



    def test_false_positive_signals_self_and_forwarded_logs_are_filtered(self):
        session = "group:50005"
        now = 1731200000.0
        self.assertFalse(is_commitment_signal("@哈基米 猫猫，你记得我是谁吗"))
        self.assertFalse(is_commitment_signal("本来就没打算绕出去"))
        self.assertFalse(is_preference_signal("我是说能够符合其他实验现象才是结果正确"))
        self.assertFalse(is_conclusion_signal("仔细看才发现少了白毛那位"))

        noisy_evicted = [
            {"message_id": "n1", "user_id": "1001", "sender_name": "主人",
             "text": "@哈基米 猫猫，你记得我是谁吗", "timestamp": now - 400},
            {"message_id": "n2", "user_id": "SELF", "sender_name": "哈基米✨Gemini",
             "text": "本来就没打算绕出去，记得别送太快", "timestamp": now - 300},
            {"message_id": "n3", "user_id": "1002", "sender_name": "莫辰",
             "text": "[群聊的聊天记录]\n=== 消息 1 ===\n我是海洋战神", "timestamp": now - 200},
            {"message_id": "n4", "user_id": "1003", "sender_name": "群友甲",
             "text": "[引用 群友乙 的消息(id=999): \"我是物理学专业的\"] 哈哈我也发现了", "timestamp": now - 100},
        ]
        summary, _, saved = consolidate_evicted_buffer(
            noisy_evicted, old_summary="", store=self.store, session=session, active_topics=[], now=now
        )
        self.assertEqual(saved, 0)
        self.assertNotIn("id=999", summary)
        with self.store.db() as db:
            cnt = db.execute("SELECT COUNT(*) FROM records WHERE session=? AND channel='rolling_eviction'", (session,)).fetchone()[0]
            self.assertEqual(cnt, 0)


if __name__ == "__main__":
    unittest.main()
