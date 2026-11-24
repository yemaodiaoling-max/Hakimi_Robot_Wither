"""A focused relationship graph must never borrow the reverse opinion."""
import unittest

from hakimi.social.relation_perspective import outgoing_assessments, project_outgoing


def opinion(direction, score, stamp=1):
    return {"direction": direction, "provenance": "terra_assisted", "created_at": stamp,
            "assessment_id": stamp, "affinity": score, "scores_available": score is not None}


class RelationPerspectiveTests(unittest.TestCase):
    def test_reverse_is_independent(self):
        rows = [opinion("11->22", 80), opinion("22->11", 20, 2)]
        self.assertEqual(outgoing_assessments(rows, "11", "22")["terra"]["affinity"], 80)
        self.assertEqual(outgoing_assessments(rows, "22", "11")["terra"]["affinity"], 20)

    def test_missing_or_new_unknown_does_not_reuse_score(self):
        rows = [opinion("11->22", 80), opinion("11->22", None, 2)]
        self.assertIsNone(outgoing_assessments(rows, "11", "22")["terra"]["affinity"])
        self.assertEqual(outgoing_assessments(rows, "22", "11"), {})

    def test_projection_is_observed_and_reverse_unknown(self):
        edge = {"source": "person:11", "target": "person:22", "scores": {},
                "closeness": {}, "confidence": {}, "measures": {"affinity": {}, "closeness": {}},
                "directions": [opinion("11->22", 80)]}
        project_outgoing(edge, "22")
        self.assertIsNone(edge["scores"]["terra"])
        self.assertEqual(edge["perspective"]["direction"], "22->11")
        self.assertEqual(edge["perspective"]["viewpoint"], "observed")


if __name__ == "__main__":
    unittest.main()
