"""Cache invalidation must never cache policy decisions or completed authority."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from hakimi.core.runtime_io import read_derived_cached, invalidate
from hakimi.chat import quote_policy
from hakimi.tools import sticker_policy


class CachedPolicyReadTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent.resolve()
        self.temp = tempfile.TemporaryDirectory(prefix="cached_reads_", dir=root)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.assertTrue(self.base.is_relative_to(root))
        (self.base/"state").mkdir()

    def test_atomic_replace_append_explicit_invalidation_and_removal_are_immediate(self):
        path = self.base/"data.jsonl"
        path.write_text("old", encoding="utf-8")
        loader = Mock(side_effect=lambda p:p.read_text(encoding="utf-8"))
        read = lambda:read_derived_cached(path, loader, "test")
        self.assertEqual((read(), read()), ("old", "old"))
        self.assertEqual(loader.call_count, 1)
        replacement = self.base/"replacement.tmp"
        replacement.write_text("new", encoding="utf-8")
        replacement.replace(path)
        self.assertEqual(read(), "new")
        with path.open("a", encoding="utf-8") as stream:stream.write("er")
        self.assertEqual(read(), "newer")
        invalidate(path)
        self.assertEqual(read(), "newer")
        self.assertEqual(loader.call_count, 4)
        path.unlink()
        with self.assertRaises(FileNotFoundError):read()

    def test_quote_cache_still_expires_without_a_file_change(self):
        now = 200000
        path = self.base/"state/chat_history.jsonl"
        rows = [{"session_key":"group:123", "message_id":i, "user_id":str(111+i%2),
                 "received_at":now-age} for i,age in enumerate((8,5,1), 1)]
        path.write_text("".join(json.dumps(row)+"\n" for row in rows), encoding="utf-8")
        self.assertTrue(quote_policy.allow_quote("group:123", "1", self.base, now))
        self.assertFalse(quote_policy.allow_quote("group:123", "1", self.base, now+181))

    def test_future_reply_validation_is_recomputed_for_each_clock_snapshot(self):
        path = self.base/"state/outbox.jsonl"
        row = {"reply_id":"one", "session_key":"group:123", "created_at":200010, "segments":["文字"]}
        path.write_text(json.dumps(row)+"\n", encoding="utf-8")
        with self.assertRaises(ValueError):sticker_policy._history(path, 200000)
        records, _ = sticker_policy._history(path, 200010)
        self.assertEqual(records[0]["at"], 200010)
        with self.assertRaises(ValueError):sticker_policy._history(path, 200000)

    def test_an_incomplete_outbox_never_reuses_a_previous_valid_snapshot(self):
        path = self.base/"state/outbox.jsonl"
        row = {"reply_id":"one", "session_key":"group:123", "created_at":200000, "segments":["文字"]}
        line = json.dumps(row)
        path.write_text(line+"\n", encoding="utf-8")
        self.assertEqual(len(sticker_policy._history(path, 200000)[0]), 1)
        with path.open("a", encoding="utf-8") as stream:stream.write('{"partial":')
        with self.assertRaises(ValueError):sticker_policy._history(path, 200000)
        path.write_text(line+"\n", encoding="utf-8")
        self.assertEqual(len(sticker_policy._history(path, 200000)[0]), 1)


if __name__ == "__main__":
    unittest.main()
