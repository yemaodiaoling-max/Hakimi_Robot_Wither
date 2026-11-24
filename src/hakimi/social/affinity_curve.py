"""Pure, host-driven affinity adjustment policy.

This module performs no I/O and stores no notes.  It does not model or claim
feelings.  The host is expected to call it only for an actual Gemini manual
judgment for subject=hakimi, and to preserve the exact note separately.
Terra/DSH-sourced material never auto-controls behavior.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = ["adjust_affinity", "NORM_GUIDANCE"]


NORM_GUIDANCE = (
    "Affinity is a host-maintained score, never a model feeling or a note. "
    "Disagreement, normal criticism, refused requests, and consensual jokes "
    "are not severe negative behavior. Concrete targeted harassment, repeated "
    "malicious boundary violations, threats, or privacy harm can be major "
    "negative only with credible evidence. Exceptional positive behavior means "
    "substantial help, fulfilled important commitments, or sustained trustworthy "
    "action, not flattery, spam, or routine politeness. This policy is called "
    "only for an actual Gemini manual judgment for subject=hakimi; the host "
    "stores the exact note separately. Terra/DSH-sourced material never "
    "auto-controls behavior."
)

_IMPACT_CAPS = {"minor": 1, "ordinary": 3, "major": 8}


def _score(value: Any, name: str) -> int | None:
    """Accept only None or an integer in 0..100; reject bool and non-integers."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be None or an integer in 0..100")
    if not 0 <= value <= 100:
        raise ValueError(f"{name} must be None or an integer in 0..100")
    return value


def _impact(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("impact must be one of: minor, ordinary, major")
    normalized = value.strip().lower()
    if normalized not in _IMPACT_CAPS:
        raise ValueError("impact must be one of: minor, ordinary, major")
    return normalized


def _confidence(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("confidence must be None or a finite number in 0..100")
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError("confidence must be None or a finite number in 0..100")
    return float(value)


def _evidence_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("evidence_count must be a non-negative integer")
    return value


def _text_reason(reason: Any) -> str:
    if not isinstance(reason, str):
        raise ValueError("reason must be a string")
    return reason.strip()


def _substantive(reason: str) -> bool:
    """A guard for real prose, not whitespace or punctuation padding."""
    return len(reason) >= 10 and any(ch.isalnum() for ch in reason)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _smoothstep(value: float) -> float:
    t = _clamp(value, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _round_conservative(value: float) -> int:
    """Nearest integer, with exact halves rounded toward zero."""
    if value <= 0:
        return 0
    return max(0, math.ceil(value - 0.5))


def _result(
    previous: int | None,
    proposed: int | None,
    applied: int | None,
    delta: int,
    impact: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "previous": previous,
        "proposed": proposed,
        "applied": applied,
        "delta": delta,
        "impact": impact,
        "policy_reason": reason,
    }


def adjust_affinity(
    previous: int | None,
    proposed: int | None,
    impact: str = "ordinary",
    confidence: float | None = None,
    reason: str = "",
    evidence_count: int = 0,
) -> dict[str, Any]:
    """Return a JSON-safe affinity decision without storing anything.

    ``previous`` and ``proposed`` are None or integers in 0..100.  For a first
    score, an ordinary (or minor) judgment is bounded to 35..65 around neutral
    50, while a fully qualified major judgment is bounded to 20..80.  For an
    existing score, ordinary/minor movement is damped near the 20 and 80
    boundaries, and a qualified major judgment uses a cautious 0.35..1.0
    multiplier.  Major judgments require confidence >= 75, a substantive
    reason of at least 10 characters, and at least one evidence item; otherwise
    they are treated as ordinary.

    ``delta`` is applied minus previous for an existing score.  For a first
    score it is applied minus the neutral anchor of 50, because no previous
    score exists.
    """
    prev = _score(previous, "previous")
    prop = _score(proposed, "proposed")
    requested_impact = _impact(impact)
    conf = _confidence(confidence)
    evidence = _evidence_count(evidence_count)
    why = _text_reason(reason)

    effective = requested_impact
    prefix = ""

    if requested_impact == "major":
        missing: list[str] = []
        if conf is None or conf < 75:
            missing.append("confidence below 75")
        if not _substantive(why):
            missing.append("reason under 10 substantive characters")
        if evidence < 1:
            missing.append("no evidence")
        if missing:
            effective = "ordinary"
            prefix = (
                "Requested major impact was unqualified ("
                + "; ".join(missing)
                + "); ordinary policy applied. "
            )

    if prop is None:
        return _result(
            prev,
            prop,
            prev,
            0,
            effective,
            prefix + "No explicit proposed score; affinity is unchanged.",
        )

    if prev is None:
        if effective == "major":
            low, high = 20, 80
            initial = "Initial qualified major score"
        else:
            low, high = 35, 65
            initial = "Initial score"
        applied = int(_clamp(prop, low, high))
        return _result(
            prev,
            prop,
            applied,
            applied - 50,
            effective,
            prefix
            + f"{initial} from proposed {prop} is bounded to {low}..{high} "
            "around neutral 50; nothing is stored or written.",
        )

    if prop == prev:
        return _result(
            prev,
            prop,
            prev,
            0,
            effective,
            prefix + f"Proposed score equals previous ({prev}); no change.",
        )

    direction = 1 if prop > prev else -1
    cap = _IMPACT_CAPS[effective]
    base = min(abs(prop - prev), cap)

    if effective == "major":
        if direction < 0:
            factor = 0.35 + 0.65 * _smoothstep(_clamp(prev / 50.0, 0.0, 1.0))
        else:
            factor = 0.35 + 0.65 * _smoothstep(
                _clamp((100 - prev) / 50.0, 0.0, 1.0)
            )
    elif direction < 0:
        factor = _smoothstep(_clamp((prev - 20) / 30.0, 0.0, 1.0))
    else:
        factor = _smoothstep(_clamp((80 - prev) / 30.0, 0.0, 1.0))

    magnitude = _round_conservative(base * factor)
    applied = int(_clamp(prev + direction * magnitude, 0.0, 100.0))

    # Move only toward the proposal.
    if direction > 0:
        applied = min(applied, prop)
    else:
        applied = max(applied, prop)

    delta = applied - prev
    direction_word = "decrease" if direction < 0 else "increase"

    if magnitude == 0:
        if effective != "major" and direction < 0 and prev <= 20:
            detail = (
                f"{effective.capitalize()} decrease is blocked at scores of 20 or "
                "below; only a qualified major negative judgment can lower this range."
            )
        elif effective != "major" and direction > 0 and prev >= 80:
            detail = (
                f"{effective.capitalize()} increase is blocked at scores of 80 or "
                "above; only a qualified major positive judgment can raise this range."
            )
        else:
            detail = (
                f"{effective.capitalize()} {direction_word} rounded conservatively "
                f"to 0 at score {prev}; no change."
            )
    elif effective == "major":
        detail = (
            f"Qualified major {direction_word} is capped at 8 and scaled by "
            f"{factor:.3f} for caution, giving {delta:+d}."
        )
    else:
        detail = (
            f"{effective.capitalize()} {direction_word} is capped at {cap} and "
            f"boundary-scaled by {factor:.3f}, giving {delta:+d}."
        )

    return _result(prev, prop, applied, delta, effective, prefix + detail)
