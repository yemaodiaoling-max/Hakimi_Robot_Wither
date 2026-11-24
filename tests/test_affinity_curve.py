"""Tests for the standalone affinity curve policy."""

from __future__ import annotations

import json
import math
import unittest

from hakimi.social.affinity_curve import NORM_GUIDANCE, adjust_affinity

_MAJOR_REASON = "Verified substantive evidence across repeated observations."


def major(previous, proposed, confidence=75, reason=_MAJOR_REASON, evidence=1):
    return adjust_affinity(
        previous,
        proposed,
        impact="major",
        confidence=confidence,
        reason=reason,
        evidence_count=evidence,
    )


class InitialScoreTests(unittest.TestCase):
    def test_unknown_without_proposal_stays_unknown(self):
        result = adjust_affinity(None, None)
        self.assertIsNone(result["previous"])
        self.assertIsNone(result["proposed"])
        self.assertIsNone(result["applied"])
        self.assertEqual(result["delta"], 0)

    def test_first_ordinary_score_is_bounded_around_fifty(self):
        high = adjust_affinity(None, 90)
        self.assertEqual(high["applied"], 65)
        self.assertEqual(high["delta"], 15)

        low = adjust_affinity(None, 10)
        self.assertEqual(low["applied"], 35)
        self.assertEqual(low["delta"], -15)

        unchanged = adjust_affinity(None, 50)
        self.assertEqual(unchanged["applied"], 50)
        self.assertEqual(unchanged["delta"], 0)

        self.assertEqual(adjust_affinity(None, 35)["applied"], 35)
        self.assertEqual(adjust_affinity(None, 65)["applied"], 65)

    def test_first_qualified_major_is_bounded_at_twenty_and_eighty(self):
        self.assertEqual(major(None, 0)["applied"], 20)
        self.assertEqual(major(None, 100)["applied"], 80)
        self.assertEqual(major(None, 50)["applied"], 50)

    def test_unknown_major_without_qualification_uses_ordinary_bounds(self):
        result = major(None, 100, confidence=74)
        self.assertEqual(result["impact"], "ordinary")
        self.assertEqual(result["applied"], 65)

    def test_missing_proposal_preserves_existing_score(self):
        result = adjust_affinity(77, None)
        self.assertEqual(result["applied"], 77)
        self.assertEqual(result["delta"], 0)
        self.assertEqual(result["proposed"], None)


class BoundaryAndDirectionTests(unittest.TestCase):
    def test_ordinary_negative_is_zero_at_or_below_twenty(self):
        for previous in (0, 10, 20):
            result = adjust_affinity(previous, 0)
            self.assertEqual(result["applied"], previous)
            self.assertEqual(result["delta"], 0)

    def test_ordinary_positive_is_zero_at_or_above_eighty(self):
        for previous in (80, 90, 100):
            result = adjust_affinity(previous, 100)
            self.assertEqual(result["applied"], previous)
            self.assertEqual(result["delta"], 0)

    def test_qualified_major_can_move_inside_guarded_edges(self):
        down = major(20, 0)
        self.assertEqual(down["impact"], "major")
        self.assertEqual(down["delta"], -5)

        up = major(80, 100)
        self.assertEqual(up["impact"], "major")
        self.assertEqual(up["delta"], 5)

    def test_major_at_extremes_is_cautious(self):
        self.assertEqual(major(5, 0)["delta"], -2)
        self.assertEqual(major(95, 100)["delta"], 2)

    def test_positive_movement_is_non_increasing_as_score_rises(self):
        gains = [
            adjust_affinity(old, 100)["delta"] for old in range(0, 101, 10)
        ]
        self.assertTrue(
            all(a >= b for a, b in zip(gains, gains[1:])), gains
        )

    def test_negative_movement_strengthens_as_score_rises(self):
        gains = [adjust_affinity(old, 0)["delta"] for old in range(0, 101, 10)]
        self.assertTrue(
            all(a >= b for a, b in zip(gains, gains[1:])), gains
        )

    def test_ordinary_curve_is_mirrored_around_fifty(self):
        for old in (25, 30, 40, 50, 60, 70, 75):
            down = adjust_affinity(old, 0)["delta"]
            up = adjust_affinity(100 - old, 100)["delta"]
            self.assertEqual(down, -up, (old, down, up))

    def test_major_curve_is_mirrored_around_fifty(self):
        for old in (5, 10, 25, 30, 40, 50, 60, 70, 75, 90, 95):
            down = major(old, 0)["delta"]
            up = major(100 - old, 100)["delta"]
            self.assertEqual(down, -up, (old, down, up))


class MagnitudeTests(unittest.TestCase):
    def test_center_magnitude_caps(self):
        self.assertEqual(adjust_affinity(50, 100, "minor")["delta"], 1)
        self.assertEqual(adjust_affinity(50, 100, "ordinary")["delta"], 3)
        self.assertEqual(major(50, 100)["delta"], 8)
        self.assertEqual(adjust_affinity(50, 0, "minor")["delta"], -1)
        self.assertEqual(adjust_affinity(50, 0, "ordinary")["delta"], -3)
        self.assertEqual(major(50, 0)["delta"], -8)

    def test_small_proposals_are_not_overshot(self):
        self.assertEqual(adjust_affinity(50, 51)["applied"], 51)
        self.assertEqual(adjust_affinity(50, 52)["applied"], 52)
        self.assertEqual(adjust_affinity(50, 49)["applied"], 49)
        self.assertEqual(adjust_affinity(50, 48)["applied"], 48)
        self.assertEqual(major(50, 51)["applied"], 51)

    def test_never_overshoots_a_distant_proposal(self):
        up = adjust_affinity(99, 100)
        self.assertLessEqual(up["applied"], 100)
        self.assertEqual(up["applied"], 99)

        down = adjust_affinity(99, 0)
        self.assertGreaterEqual(down["applied"], 0)
        self.assertEqual(down["applied"], 96)

    def test_half_steps_round_conservatively_toward_the_previous_score(self):
        # Both factors are exactly 0.5 at these mirrored positions; 3 * 0.5 = 1.5.
        self.assertEqual(adjust_affinity(35, 0)["delta"], -1)
        self.assertEqual(adjust_affinity(65, 100)["delta"], 1)

    def test_same_score_is_a_noop_even_for_a_qualified_major(self):
        result = major(70, 70)
        self.assertEqual(result["applied"], 70)
        self.assertEqual(result["delta"], 0)
        self.assertEqual(result["impact"], "major")
        self.assertIn("no change", result["policy_reason"].lower())


class MajorGuardTests(unittest.TestCase):
    def test_fully_qualified_major_uses_major_curve(self):
        result = major(50, 0)
        self.assertEqual(result["impact"], "major")
        self.assertEqual(result["delta"], -8)
        self.assertNotIn("unqualified", result["policy_reason"])

    def test_boundaries_accept_exactly_ten_characters(self):
        result = major(50, 0, confidence=75, reason="Harassment", evidence=1)
        self.assertEqual(len("Harassment"), 10)
        self.assertEqual(result["impact"], "major")
        self.assertEqual(result["delta"], -8)

    def test_confidence_below_75_downgrades_to_ordinary(self):
        result = major(50, 0, confidence=74.9)
        self.assertEqual(result["impact"], "ordinary")
        self.assertEqual(result["delta"], -3)
        self.assertIn("confidence", result["policy_reason"])

    def test_short_or_punctuation_reason_downgrades_to_ordinary(self):
        for reason in ("too short", "----------", "          "):
            result = major(50, 0, reason=reason)
            self.assertEqual(result["impact"], "ordinary", reason)
            self.assertEqual(result["delta"], -3, reason)
            self.assertIn("reason", result["policy_reason"])

    def test_missing_evidence_downgrades_to_ordinary(self):
        result = major(50, 0, evidence=0)
        self.assertEqual(result["impact"], "ordinary")
        self.assertEqual(result["delta"], -3)
        self.assertIn("evidence", result["policy_reason"])

    def test_all_missing_qualifiers_are_reported(self):
        result = adjust_affinity(
            50, 0, impact="major", confidence=None, reason="", evidence_count=0
        )
        self.assertEqual(result["impact"], "ordinary")
        self.assertIn("unqualified", result["policy_reason"])
        self.assertIn("confidence", result["policy_reason"])
        self.assertIn("reason", result["policy_reason"])
        self.assertIn("evidence", result["policy_reason"])


class ValidationAndShapeTests(unittest.TestCase):
    def test_invalid_scores_are_rejected(self):
        invalid = (True, False, 50.0, "50", -1, 101, math.nan, math.inf)
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    adjust_affinity(value, 50)
                with self.assertRaises(ValueError):
                    adjust_affinity(50, value)

    def test_invalid_impact_is_rejected(self):
        for value in ("severe", "", None, 3):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    adjust_affinity(50, 60, impact=value)

    def test_invalid_confidence_is_rejected(self):
        for value in (True, "high", math.nan, math.inf, -1, 101):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    adjust_affinity(50, 60, confidence=value)

    def test_invalid_evidence_count_is_rejected(self):
        for value in (True, -1, 1.5, "1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    adjust_affinity(50, 60, evidence_count=value)

    def test_invalid_reason_type_is_rejected(self):
        with self.assertRaises(ValueError):
            adjust_affinity(50, 60, reason=None)  # type: ignore[arg-type]

    def test_result_is_json_safe_and_has_exact_keys(self):
        samples = [
            adjust_affinity(None, None),
            adjust_affinity(None, 90),
            adjust_affinity(50, 100),
            adjust_affinity(50, 0),
            major(50, 0),
            major(50, 0, confidence=0),
        ]
        expected_keys = {
            "previous",
            "proposed",
            "applied",
            "delta",
            "impact",
            "policy_reason",
        }
        for result in samples:
            self.assertEqual(set(result), expected_keys)
            json.dumps(result, allow_nan=False)
            self.assertIsInstance(result["delta"], int)
            if result["applied"] is not None:
                self.assertIsInstance(result["applied"], int)
                self.assertGreaterEqual(result["applied"], 0)
                self.assertLessEqual(result["applied"], 100)
            self.assertIsInstance(result["policy_reason"], str)

    def test_exported_norm_guidance_covers_the_policy_scope(self):
        guidance = NORM_GUIDANCE.lower()
        for phrase in (
            "disagreement",
            "consensual jokes",
            "targeted harassment",
            "privacy harm",
            "substantial help",
            "flattery",
            "subject=hakimi",
            "terra/dsh",
        ):
            self.assertIn(phrase, guidance)


if __name__ == "__main__":
    unittest.main()
