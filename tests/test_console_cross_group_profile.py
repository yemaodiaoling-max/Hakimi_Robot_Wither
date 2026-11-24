"""A group filter chooses who is shown; scores and profiles stay cross-group."""
from pathlib import Path
import sys
import tempfile
import time
import unittest

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here.parent))

from hakimi.console.console_db import initialise_social  # noqa: E402
from hakimi.console.payloads_social import (  # noqa: E402
    graph_payload, people_payload, person_payload, relation_payload)
from hakimi.memory.memory_store import MemoryStore  # noqa: E402
from hakimi.social.memory_social import record_social  # noqa: E402


class CrossGroupProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=here, prefix="xgroup_")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        (self.base / "state").mkdir(parents=True, exist_ok=True)
        self.store = MemoryStore(self.base)
        initialise_social(self.base)
        self.now = time.time()
        # 101 and 102 talk in both groups; the score is written in group:1 only.
        evidence = [self.observe(f"@哈基米 群一 {i}", "101", "group:1", f"a{i}") for i in range(3)]
        self.observe("群二也在", "101", "group:2", "b1")
        self.observe("群二路人", "102", "group:2", "b2")
        self.observe("私聊秘密", "101", "private:101", "p1")
        record_social(self.base, "group:1", "愿意接话的群友", "101",
                      relationship_type="群友", affinity=72, closeness=55, confidence=80,
                      evidence_memory_ids=["M%d" % value for value in evidence],
                      origin_message_id="turn-1", now=self.now)
        # Live notes leave scoring to the event curve; pin a durable score here.
        with self.store.db() as db:
            db.execute("UPDATE social_assessments SET affinity=72,closeness=55,confidence=80")

    def observe(self, text, user, session, mid):
        self.store.observe({"session_key": session, "user_id": user, "message_id": mid,
                            "received_at": self.now, "text": text, "nickname": "昵称" + user})
        with self.store.db() as db:
            return db.execute("SELECT id FROM records WHERE origin=? AND session=?",
                              (mid, session)).fetchone()[0]

    def test_person_scores_follow_the_person_into_another_group(self):
        whole = person_payload(self.base, {"id": "101"})
        filtered = person_payload(self.base, {"id": "101", "group": "group:2"})
        self.assertEqual([item["affinity"] for item in filtered["relations"]], [72])
        self.assertEqual(filtered["relations"], whole["relations"])
        self.assertEqual(filtered["impressions"]["manual"], whole["impressions"]["manual"])
        self.assertEqual(filtered["person"]["records"], whole["person"]["records"])
        self.assertNotIn("private:101", filtered["person"]["sessions"])

    def test_filter_still_decides_visibility(self):
        with self.assertRaises(Exception):
            person_payload(self.base, {"id": "101", "group": "group:3"})
        people = people_payload(self.base, {"group": "group:2"})["people"]
        self.assertEqual({item["id"] for item in people}, {"101", "102"})
        counts = {item["id"]: item["manual_count"] for item in people}
        self.assertEqual(counts["101"], 1)

    def test_relation_and_graph_carry_cross_group_scores(self):
        relation = relation_payload(self.base, {"a": "hakimi", "b": "101", "group": "group:2"})
        self.assertEqual([item["affinity"] for item in relation["directions"]], [72])
        graph = graph_payload(self.base, {"group": "group:2", "metric": "manual_affinity"})
        ids = {node["official_id"] for node in graph["nodes"]}
        self.assertTrue({"101", "102"} <= ids)
        edge = [item for item in graph["edges"]
                if {item["source"], item["target"]} == {"assistant:hakimi", "person:101"}]
        self.assertEqual(edge[0]["scores"]["manual"], 72)

    def test_private_filter_is_not_merged_with_public_scores(self):
        private = person_payload(self.base, {"id": "101", "group": "private:101"})
        self.assertEqual(private["relations"], [])
        self.assertEqual(private["person"]["sessions"], ["private:101"])


if __name__ == "__main__":
    unittest.main()
