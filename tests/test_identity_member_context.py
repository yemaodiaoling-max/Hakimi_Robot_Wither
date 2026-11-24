"""Tests for the read-only identity member context payload."""

from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from hakimi.social import identity_member_context
from hakimi.social.identity_member_context import ContextError, payload


SCHEMA = """
CREATE TABLE records (
 id INTEGER PRIMARY KEY,
 session TEXT NOT NULL,
 user_id TEXT NOT NULL,
 kind TEXT NOT NULL,
 text TEXT NOT NULL,
 summary TEXT NOT NULL DEFAULT '',
 at REAL NOT NULL,
 state TEXT NOT NULL DEFAULT 'active'
)
"""


class MemberContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / "state").mkdir()
        self.db_path = self.base / "state" / "memory.sqlite3"
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(SCHEMA)
            connection.commit()

    def add(self, session, user_id, text, at, *, kind="chat", summary="",
            state="active"):
        with closing(sqlite3.connect(self.db_path)) as connection:
            cursor = connection.execute(
                "INSERT INTO records(session,user_id,kind,text,summary,at,state) "
                "VALUES(?,?,?,?,?,?,?)",
                (session, user_id, kind, text, summary, at, state),
            )
            connection.commit()
            return cursor.lastrowid

    def test_latest_three_stay_in_the_selected_group_and_exclude_deleted(self):
        self.add("group:100", "200", "一", 100)
        self.add("group:100", "200", "二", 200)
        self.add("group:100", "200", "三", 300)
        self.add("group:100", "200", "四", 400)
        self.add("group:100", "200", "x" * 300, 500)
        self.add("group:100", "200", "删掉的", 600, state="deleted")
        self.add("group:999", "200", "别的群", 900)
        self.add("private:200", "200", "私聊", 900)
        self.add("group:100", "999", "别人", 900)
        self.add("group:100", "200", "笔记", 900, kind="note")

        data = payload(str(self.base), {"group": "100", "user": "200"})

        self.assertEqual(data["group"], "100")
        self.assertEqual(data["user"], "200")
        self.assertEqual(data["hint"], "仅供你核对身份，不会据此自动匹配")
        self.assertEqual([item["memory_id"] for item in data["messages"]],
                         ["M5", "M4", "M3"])
        self.assertEqual([item["excerpt"] for item in data["messages"]],
                         ["x" * 240, "四", "三"])
        self.assertTrue(all(item["label"] == "群内原话"
                            for item in data["messages"]))
        self.assertEqual(data["messages"][0]["at"], 500)
        joined = "".join(item["excerpt"] for item in data["messages"])
        for leaked in ("删掉的", "别的群", "私聊", "别人", "笔记"):
            self.assertNotIn(leaked, joined)

    def test_summary_is_used_and_fully_empty_rows_are_skipped(self):
        self.add("group:100", "200", "更早", 100)
        self.add("group:100", "200", "", 200, summary="旧摘要内容")
        self.add("group:100", "200", "   ", 300, summary="   ")
        self.add("group:100", "200", "原话", 400)

        data = payload(str(self.base), {"group": "group:100", "user": "200"})

        self.assertEqual([item["memory_id"] for item in data["messages"]],
                         ["M4", "M2"])
        self.assertEqual(data["messages"][0]["excerpt"], "原话")
        self.assertEqual(data["messages"][0]["label"], "群内原话")
        self.assertEqual(data["messages"][1]["excerpt"], "旧摘要内容")
        self.assertEqual(data["messages"][1]["label"], "旧摘要")

    def test_fully_empty_rows_are_skipped_without_hiding_the_member(self):
        self.add("group:100", "200", "", 100)
        self.add("group:100", "200", "   ", 200, summary="   ")
        data = payload(str(self.base), {"group": "100", "user": "200"})
        self.assertEqual(data["messages"], [])
        self.assertEqual(data["hint"], "仅供你核对身份，不会据此自动匹配")

    def test_unknown_member_is_not_observed(self):
        with self.assertRaises(ContextError) as caught:
            payload(str(self.base), {"group": "100", "user": "200"})
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "member_not_observed")
        self.assertEqual(str(caught.exception), "该账号尚未在所选群出现")

        self.add("group:100", "200", "删掉的", 100, state="deleted")
        with self.assertRaises(ContextError) as caught:
            payload(str(self.base), {"group": "100", "user": "200"})
        self.assertEqual(caught.exception.status, 404)

    def test_invalid_values_and_extra_fields_are_rejected(self):
        cases = [
            None,
            [],
            {},
            {"group": "100"},
            {"user": "200"},
            {"group": "100", "user": "200", "extra": "1"},
            {"group": "100", "user": "200", "private": "1"},
            {"group": 100, "user": "200"},
            {"group": "100", "user": 200},
            {"group": "0", "user": "200"},
            {"group": "0100", "user": "200"},
            {"group": "private:100", "user": "200"},
            {"group": "group:", "user": "200"},
            {"group": "group:0100", "user": "200"},
            {"group": "group:100:2", "user": "200"},
            {"group": "1" * 21, "user": "200"},
            {"group": "100", "user": "0"},
            {"group": "100", "user": "0200"},
            {"group": "100", "user": "group:200"},
            {"group": "100", "user": "2" * 21},
        ]
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(ContextError) as caught:
                    payload(str(self.base), values)
                self.assertEqual(caught.exception.status, 400)
                self.assertNotIn(str(self.db_path), str(caught.exception))

    def test_prefixed_group_is_accepted(self):
        self.add("group:321", "654", "本群原话", 100)
        data = payload(str(self.base), {"group": "group:321", "user": "654"})
        self.assertEqual(data["group"], "321")
        self.assertEqual(data["user"], "654")
        self.assertEqual(data["messages"][0]["excerpt"], "本群原话")

    def test_missing_database_is_a_generic_503_and_is_not_created(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "state").mkdir()
            with self.assertRaises(ContextError) as caught:
                payload(str(base), {"group": "100", "user": "200"})
            error = caught.exception
            self.assertEqual(error.status, 503)
            self.assertEqual(error.code, "database_not_ready")
            self.assertNotIn(str(base), str(error))
            self.assertNotIn("SELECT", str(error))
            self.assertFalse((base / "state" / "memory.sqlite3").exists())

    def test_source_bytes_are_unchanged_and_connection_is_closed(self):
        self.add("group:100", "200", "原话", 100)
        before = self.db_path.read_bytes()

        real_connect = sqlite3.connect
        opened = []

        def traced_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        with patch.object(identity_member_context.sqlite3, "connect",
                          side_effect=traced_connect):
            data = payload(str(self.base), {"group": "100", "user": "200"})

        self.assertEqual(data["messages"][0]["excerpt"], "原话")
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
