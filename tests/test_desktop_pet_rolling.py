"""Unit tests for Desktop Pet constant-water-level rolling window and axial memory."""
import copy
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

try:
    import desktop_pet_agent as pet_mod
except ImportError:
    import desktop_pet.server_agent as pet_mod

from hakimi.memory.memory_store import MemoryStore

STEADY_KEEP_TURNS = pet_mod.STEADY_KEEP_TURNS
DEFAULT_MAX_TURNS = pet_mod.DEFAULT_MAX_TURNS
_consolidate_evicted_slice = pet_mod._consolidate_evicted_slice
_retrieve_initial_context = pet_mod._retrieve_initial_context
_is_private_chat_observation = pet_mod._is_private_chat_observation
_sanitize_summary_for_qq_sync = pet_mod._sanitize_summary_for_qq_sync
_sync_desktop_thought_to_memory = pet_mod._sync_desktop_thought_to_memory
_execute_server_tool = pet_mod._execute_server_tool


class DesktopPetRollingTests(unittest.TestCase):
    def test_evicted_slice_consolidation_preserves_steady_window(self):
        # State currently has 8 turns (16 messages)
        history = []
        for i in range(1, 9):
            history.append({"role": "user", "text": f"主人说的事情_{i}", "at": 1000 + i * 10})
            history.append({"role": "model", "text": f"哈基米的回应_{i}", "at": 1000 + i * 10 + 2})

        state = {
            "session_id": "test_session",
            "turns": 8,
            "context_tokens": 5000,
            "rolling_summary": "上一轮摘要",
            "just_scrolled_out": [],
            "history": history,
            "total_rotations": 0,
        }

        # Slicing simulation
        evicted_count = len(state["history"]) - (STEADY_KEEP_TURNS * 2)
        self.assertEqual(evicted_count, 6)  # 3 turns = 6 messages evicted

        evicted_history = state["history"][:evicted_count]
        state["history"] = state["history"][-STEADY_KEEP_TURNS * 2:]

        # Window keeps exactly 10 messages (5 turns)
        self.assertEqual(len(state["history"]), 10)
        self.assertEqual(len(evicted_history), 6)

        # Consolidate evicted slice (mocking vertex model call)
        with patch.object(pet_mod, "_call_vertex_gemini", side_effect=Exception("offline")):
            info = _consolidate_evicted_slice(state, evicted_history)

        self.assertTrue(info["consolidated"])
        self.assertTrue(len(info["summary"]) > 0)
        # just_scrolled_out populated with last 2 evicted messages
        self.assertEqual(len(state["just_scrolled_out"]), 2)
        self.assertIn("[刚滑出]", state["just_scrolled_out"][0])
        self.assertIn("主人说的事情_3", state["just_scrolled_out"][0])

    def test_retrieve_initial_context_enrichment_for_short_queries(self):
        history = [
            {"role": "user", "text": "我想买一台MacBook Pro用来写代码"},
            {"role": "model", "text": "M4芯片挺合适的"},
        ]
        # Short query
        query = "然后呢？"

        mock_store = MagicMock()
        mock_store.retrieve.return_value = [
            "[M201] 人物8112866014149232024·偏好·记今天：偏好使用MacBook写代码",
            "[M202] 约定·记今天：计划下个月换新电脑",
        ]

        with patch.object(pet_mod, "get_memory_store", return_value=mock_store), \
             patch.object(pet_mod.paths, "runtime") as mock_runtime:
            (mock_runtime.return_value / "state" / "chat_history.jsonl").exists.return_value = False
            pack = _retrieve_initial_context(query, history=history, rolling_summary="正在讨论买电脑")

        # Verify query was enriched with context before calling store.retrieve
        call_args = mock_store.retrieve.call_args
        self.assertIsNotNone(call_args)
        called_query = call_args.kwargs.get("query")
        self.assertIn("MacBook Pro", called_query)
        self.assertIn("然后呢？", called_query)

        # Verify vertical and horizontal structure
        self.assertIn("vertical_memory", pack)
        self.assertIn("horizontal_memory", pack)
        vert = pack["vertical_memory"]
        self.assertEqual(len(vert["owner_portraits_and_commitments"]), 2)

    def test_private_chat_screen_not_synced_to_qq_memory(self):
        # Window title heuristics distinguish private IM sessions vs known public groups
        self.assertTrue(_is_private_chat_observation("仍然是彩色群友63cb！等4个会话", "", ""))
        self.assertTrue(_is_private_chat_observation("微信", "看屏幕", "吐槽"))
        self.assertTrue(_is_private_chat_observation("Gemini", "看屏幕", "怕本喵抖落你跟群友317a的私聊呀"))
        self.assertFalse(_is_private_chat_observation("万有引力パワー摸鱼部等3个会话", "看屏幕", "物理推导好难"))
        self.assertFalse(_is_private_chat_observation("主人子协会等4个会话", "看屏幕", "大家在群里讨论原神"))
        self.assertFalse(_is_private_chat_observation("analysis.pdf - Microsoft Edge", "看屏幕", "翻到第6页"))

        # Summary sanitization strips only private chat clauses
        dirty_summary = "主人先看物理推导PDF。随后找群友63cb私聊聊社科。最后在B站看科普视频。"
        cleaned = _sanitize_summary_for_qq_sync(dirty_summary)
        self.assertIn("物理推导PDF", cleaned)
        self.assertIn("科普视频", cleaned)
        self.assertNotIn("群友63cb", cleaned)
        self.assertNotIn("私聊", cleaned)

        # memory_remember blocks private chat notes
        mock_store = MagicMock()
        with patch.object(pet_mod, "get_memory_store", return_value=mock_store):
            res = _execute_server_tool("memory_remember", {
                "note": "主人在和好友私聊讨论哲学",
                "kind": "episode",
                "key": "桌宠实时想法",
                "evidence": "桌宠窗口:仍然是彩色群友63cb！等4个会话",
            })
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("skipped"), "private_chat_privacy_guard")
        mock_store.remember_manual.assert_not_called()

        # _sync_desktop_thought_to_memory skips realtime sync on private chat windows
        with patch.object(pet_mod, "get_memory_store", return_value=mock_store):
            sync_res = _sync_desktop_thought_to_memory(
                window_title="仍然是彩色群友63cb！等4个会话",
                owner_text="[看屏幕: 仍然是彩色群友63cb！等4个会话]",
                reply_text="刚把物理推导扔一边就跟她聊社科啦",
                recent_turns=[{"window": "Gemini", "owner": "看屏幕", "pet": "看Section 2", "private": False}],
                rolling_summary="正在啃物理推导。跟彩色群友63cb私聊。",
                is_rotate=True,
            )
        self.assertEqual(sync_res["synced"]["realtime"]["skipped"], "private_chat_privacy_guard")
        mock_store.remember_manual.assert_called_once()
        saved_note = mock_store.remember_manual.call_args.args[0]["note"]
        self.assertIn("物理推导", saved_note)
        self.assertNotIn("彩色群友63cb", saved_note)
        self.assertNotIn("私聊", saved_note)

    def test_group_retrieval_filters_desktop_pet_private_leaks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "state").mkdir(parents=True, exist_ok=True)
            store = MemoryStore(base)
            now = 1794780000.0
            owner_session = "private:8112866014149232024"
            owner_id = "8112866014149232024"

            # Insert a leaked desktop_pet record and a clean desktop_pet record
            store.remember_manual(
                {
                    "note": "[桌宠实时心里话] 怕本喵去大群抖落你跟群友63cb的私聊呀",
                    "kind": "episode",
                    "user_id": owner_id,
                    "key": "桌宠实时想法",
                    "evidence": "桌宠窗口:仍然是彩色群友63cb！等4个会话",
                    "topics": ["桌宠", "主人", "私聊"],
                },
                session=owner_session,
                provider="gemini",
                channel="desktop_pet",
                now=now - 120,
            )
            store.remember_manual(
                {
                    "note": "[桌宠阶段脉络] 主人正在认真啃BerryFlow物理推导第13页共振奇点",
                    "kind": "episode",
                    "user_id": owner_id,
                    "key": "桌宠阶段脉络",
                    "evidence": "桌宠窗口:analysis.pdf",
                    "topics": ["桌宠", "主人", "物理推导"],
                },
                session=owner_session,
                provider="gemini",
                channel="desktop_pet",
                now=now - 60,
            )

            group_hits = store.retrieve(
                "group:8355220284",
                [{"user_id": owner_id, "text": "刚才在看什么私聊和物理推导"}],
                {"user_id": owner_id, "text": "刚才在看什么私聊和物理推导"},
                now=now,
                budget=1200,
                limit=6,
            )
            joined = "\n".join(group_hits)
            self.assertNotIn("私聊", joined)
            self.assertNotIn("群友63cb", joined)
            self.assertIn("BerryFlow物理推导", joined)



    @unittest.skip('uses private chat fixtures that are not published')



    def test_desktop_pet_anti_ai_persona_bubble_trim_and_variation_hint(self):
        import json
        from unittest.mock import patch
        import desktop_pet_agent as pet

        self.assertIn("2–4 字短词破冰 | 短句", pet.DESKTOP_PERSONA)
        self.assertIn("行，那我群友4182", pet.DESKTOP_PERSONA)
        self.assertNotIn("每条 10~45 字左右", pet.DESKTOP_PERSONA)

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({
                "session_id": "s1",
                "turns": 1,
                "context_tokens": 100,
                "history": [
                    {"role": "user", "text": "带点吃的", "at": 1.0, "window": "QQ", "private": False},
                    {"role": "model", "text": "行，那我带点吃的过去，你别先睡着了", "at": 2.0, "window": "QQ", "private": False},
                ],
                "rolling_summary": "",
                "active_topics": [],
                "just_scrolled_out": [],
                "total_rotations": 0,
            }, ensure_ascii=False), encoding="utf-8")
            captured_prompts = []

            def fake_call(contents, tools=None, model=None, timeout=55.0):
                captured_prompts.append(contents[-1]["parts"][0]["text"])
                return {
                    "candidates": [{
                        "content": {
                            "parts": [{"text": "行，那我带点吃的过去，你别先睡着了"}]
                        }
                    }]
                }

            with (
                patch.object(pet, "_state_file", return_value=state_path),
                patch.object(pet, "_retrieve_initial_context", return_value={}),
                patch.object(pet, "_call_vertex_gemini", side_effect=fake_call),
                patch.object(pet, "_sync_desktop_thought_to_memory", return_value=None),
            ):
                res = pet.handle_chat({"text": "别光说", "mode": "chat", "window_title": "桌面"})

            self.assertTrue(res["ok"])
            self.assertEqual(res["bubbles"], ["你别先睡着了"])
            self.assertEqual(res["reply"], "你别先睡着了")
            self.assertIn("variation_hint", captured_prompts[0])
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["history"][-1]["text"], "你别先睡着了")

    def test_desktop_clock_snapshot_and_timezone_fallback(self):
        orig_tz = pet_mod._SESSION_CLIENT_TZ
        orig_lbl = pet_mod._SESSION_CLIENT_TZ_LABEL
        try:
            pet_mod._SESSION_CLIENT_TZ = None
            pet_mod._SESSION_CLIENT_TZ_LABEL = ""

            # 1. Explicit non-local timezones keep standard clock_snapshot behavior
            ts = 1795738800.0  # 2026-11-27 00:20:00 UTC == 2026-11-26 19:20:00 EST (-05:00)
            utc_snap = pet_mod._desktop_clock_snapshot(timezone_name="UTC", now=ts)
            self.assertEqual(utc_snap["utc_offset"], "+00:00")
            self.assertEqual(utc_snap["date"], "2026-11-27")
            sh_snap = pet_mod._desktop_clock_snapshot(timezone_name="Asia/Shanghai", now=ts)
            self.assertEqual(sh_snap["utc_offset"], "+08:00")
            self.assertEqual(sh_snap["date"], "2026-11-27")
            self.assertEqual(sh_snap["time"], "08:20:00")

            # 2. Without client_time or TZ on a UTC Linux host, falls back to America/New_York
            with patch.dict("os.environ", {}, clear=False):
                import os
                os.environ.pop("TZ", None)
                ny_snap = pet_mod._desktop_clock_snapshot(req={}, now=ts)
            self.assertEqual(ny_snap["source"], "server_tz")
            self.assertEqual(ny_snap["utc_offset"], "-05:00")
            self.assertEqual(ny_snap["date"], "2026-11-26")
            self.assertEqual(ny_snap["time"], "19:20:00")

            # 3. Stale client_time (>300s drift) is ignored
            stale_req = {
                "client_time": {
                    "date": "2026-11-27",
                    "time": "09:20:00",
                    "weekday": "星期五",
                    "utc_offset": "+09:00",
                    "unix_seconds": int(ts) - 600,
                }
            }
            stale_res = pet_mod._desktop_clock_snapshot(req=stale_req, now=ts)
            self.assertEqual(stale_res["source"], "server_tz")
            self.assertIsNone(pet_mod._SESSION_CLIENT_TZ)

            # 4. Fresh client_time is honored and cached for subsequent local time_now lookups
            fresh_req = {
                "client_time": {
                    "datetime": "2026-11-27T09:20:00+09:00",
                    "date": "2026-11-27",
                    "time": "09:20:00",
                    "weekday": "星期五",
                    "timezone_label": "本机时区 JST UTC+09:00",
                    "utc_offset": "+09:00",
                    "unix_seconds": int(ts),
                }
            }
            fresh_res = pet_mod._desktop_clock_snapshot(req=fresh_req, now=ts)
            self.assertEqual(fresh_res["source"], "client_clock")
            self.assertEqual(fresh_res["utc_offset"], "+09:00")
            self.assertEqual(fresh_res["time"], "09:20:00")

            # Subsequent server fallback / time_now("local") reuses cached +09:00 offset
            cached_res = pet_mod._desktop_clock_snapshot(req=None, now=ts + 65)
            self.assertEqual(cached_res["source"], "server_tz")
            self.assertEqual(cached_res["utc_offset"], "+09:00")
            self.assertEqual(cached_res["time"], "09:21:05")
            self.assertEqual(cached_res["timezone_label"], "本机时区 JST UTC+09:00")
        finally:
            pet_mod._SESSION_CLIENT_TZ = orig_tz
            pet_mod._SESSION_CLIENT_TZ_LABEL = orig_lbl


if __name__ == "__main__":
    unittest.main()
