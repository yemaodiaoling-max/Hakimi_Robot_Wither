"""Read-only social affinity behaviour for Hakimi.

This module turns the *current, source-checked* Hakimi social projection
(``social_assessments`` in the local memory database) into a small behaviour
profile.  It is deliberately a read-only query helper:

* it never imports a model, touches the network, sends a message, or grants a
  tool;
* it never initialises, migrates, or writes the SQLite database;
* a missing database/table, malformed row, or bad argument
  degrades to a neutral ``unknown`` profile and never raises for callers.

Only these rows are trusted::

    subject_id   = 'hakimi'
    target_id    = the person being considered
    provider     = 'gemini' / 'dsh'
    provenance   = 'gemini_manual' / 'dsh_assisted'
    state        = 'active' (newest current judgment; 'superseded' is history)
    session      = public group for group calls / exact private chat for private
    evidence     = when available, must respect identity and scope; retention
                   does not reset a committed score

``terra`` analysis, another member's opinion, deleted/retracted rows,
are never used. Removed, pruned or expired evidence does not reset scores.
Superseded manual rows remain valid corroborating history for the narrower
direct-@ gate, but never become the current profile anchor.

Behaviour thresholds (the user-approved policy)::

    confidence < 60 or no trusted score       -> unknown,  multiplier 1.00
    affinity >= 75                            -> warm,     multiplier 1.15
    35 <= affinity <= 74                      -> neutral,  multiplier 1.00
    11 <= affinity <= 34                      -> reserved, multiplier 0.55
    0  <= affinity <= 10                      -> distant,  multiplier 0.20

Direct-``@`` wake rejection is intentionally much narrower than the profile:
only group sessions, two independent manual negative judgments, at least six
hours apart, the newest active judgment no more than 24 hours old, fresh
evidence in that newer judgment (a chat timestamp after the older judgment),
and no newer active positive or unknown judgment.  Configured owner/creator/
operator IDs and the dedicated policy file's protected IDs are always exempt.
"""
from __future__ import annotations
from hakimi import paths

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

__all__ = ["behavior_for", "should_reject_at"]

HAKIMI_SUBJECT = "hakimi"
MANUAL_PROVIDER = "gemini"
MANUAL_PROVENANCE = "gemini_manual"

PERSON_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
SESSION = re.compile(r"(group|private):[1-9][0-9]{0,19}\Z")

# Query bounds: a lookup can never walk the whole assessment history.
MAX_JUDGMENTS = 20
MAX_EVIDENCE = 8
POLICY_FILENAME = "social_behavior_policy.json"
MAX_POLICY_BYTES = 64 * 1024

VALID_MIN_CONFIDENCE = 60
WARM_MIN_AFFINITY = 75
NEUTRAL_MIN_AFFINITY = 35
RESERVED_MIN_AFFINITY = 11
DISTANT_MAX_AFFINITY = 10

GATE_MIN_CONFIDENCE = 70
GATE_MAX_AFFINITY = 10
GATE_MIN_GAP_SECONDS = 6 * 60 * 60
GATE_MAX_AGE_SECONDS = 24 * 60 * 60

BAND_MULTIPLIER = {
    "warm": 1.15,
    "neutral": 1.0,
    "reserved": 0.55,
    "distant": 0.2,
    "unknown": 1.0,
}
BAND_GUIDANCE = {
    "warm": (
        "关系印象可靠且偏暖：可以更自然地主动接话、多给一点具体帮助；"
        "只是语气和主动性变化，不改变事实标准、记忆边界、权限或安全判断；"
        "不要主动播报好感分或置信度数字。"
    ),
    "neutral": (
        "按普通群友自然相处：需要时接话，不刻意亲近也不刻意疏远；"
        "事实标准、记忆边界、权限和安全判断保持不变；"
        "不要主动播报好感分或置信度数字。"
    ),
    "reserved": (
        "关系印象偏保留：语气礼貌克制，少主动展开，先回应具体问题；"
        "技术或学习问题该完整回答仍完整回答，不改变事实、权限或安全边界；"
        "不要主动播报好感分或置信度数字。"
    ),
    "distant": (
        "关系印象较疏远：保持尊重、简短和边界感，只回应明确需要或确有帮助的内容；"
        "技术问题仍可完整、平等地回答，不羞辱、不报复，也不改变事实或权限；"
        "不要主动播报好感分或置信度数字。"
    ),
    "unknown": (
        "目前没有足够可靠的好感依据：按普通群友中性相处，不猜测、不虚构分数，"
        "也不改变事实标准、记忆边界、权限或安全判断；"
        "不要主动播报好感分或置信度数字。"
    ),
}

_PROTECTED_CONTAINERS = {
    "owner", "owners", "creator", "creators", "operator", "operators",
    "admin", "admins", "protection", "protected", "protected_user",
}
_PROTECTED_KEY = re.compile(
    r"(?:owner|creator|operator|admin|protected)", re.IGNORECASE
)
_PROTECTED_ID_HINT = re.compile(
    r"(?:id|ids|user|users|qq|qqs|uid|uids|account|accounts)", re.IGNORECASE
)
_PROTECTED_ROLE_VALUES = {"owner", "creator", "operator", "admin", "protected"}
_PROTECTED_FLAGS = {"owner", "creator", "operator", "admin", "protected",
                    "is_owner", "is_creator", "is_operator", "is_admin"}


# ---------------------------------------------------------------------------
# small, dependency-free helpers
# ---------------------------------------------------------------------------

def _database_path(base) -> Path:
    """Accept either a base directory or an explicit sqlite file path."""
    path = Path(base)
    if path.suffix.lower() in {".sqlite3", ".sqlite", ".db"}:
        return path
    return path / "state" / "memory.sqlite3"


def _runtime_base(base) -> Path:
    path = Path(base)
    if path.suffix.lower() in {".sqlite3", ".sqlite", ".db"}:
        return path.parent
    return path


def _readonly_connection(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    db = None
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=2.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db
    except (OSError, sqlite3.Error, ValueError):
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass
        return None


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    try:
        return db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _safe_value(row, key: str, default=""):
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


def _as_int_score(value) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,3}", value.strip()):
        value = int(value.strip())
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or not 0 <= value <= 100:
        return None
    return value


def _as_float(value, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if result != result or result in (float("inf"), float("-inf")):
        return default
    return result


def _optional_timestamp(value) -> float | None:
    """Parse an optional REAL/string timestamp; missing or bad means no bound."""
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _person_id(value) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        value = str(value)
    text = str(value or "").strip()
    return text if PERSON_ID.fullmatch(text) else ""


def _scope_for_session(session) -> tuple[str, str] | None:
    text = str(session or "").strip()
    match = SESSION.fullmatch(text)
    if not match:
        return None
    return match.group(1), text


def _config_for(base, config) -> dict:
    if isinstance(config, dict):
        return config
    if isinstance(config, (str, Path)):
        try:
            return json.loads(Path(config).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
    if config is not None:
        return {}
    try:
        return json.loads((Path(base) / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def _numeric_ids(value) -> set[str]:
    found: set[str] = set()

    def take(item, depth=0):
        if depth > 4:
            return
        if isinstance(item, dict):
            for nested_key, nested in item.items():
                take(nested_key, depth + 1)
                take(nested, depth + 1)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for nested in item:
                take(nested, depth + 1)
        elif isinstance(item, bool) or item is None:
            return
        elif isinstance(item, int):
            if item > 0:
                found.add(str(item))
        elif isinstance(item, str):
            text = item.strip()
            if PERSON_ID.fullmatch(text):
                found.add(text)
            elif re.fullmatch(r"[0-9]+", text) and int(text) > 0:
                found.add(str(int(text)))

    take(value)
    return found


def _direct_targets(value) -> set[str]:
    """Read explicit @ targets without treating arbitrary text as a target."""
    if isinstance(value, (list, tuple, set, frozenset)):
        items = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return set()
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            items = parsed
        else:
            items = re.findall(r"[1-9][0-9]{0,19}", text)
    else:
        return set()
    result = set()
    for item in items:
        person = _person_id(item)
        if person:
            result.add(person)
    return result


def _integer_ids(value) -> set[str]:
    """Read only the bounded, flat ``protected_user_ids`` policy list."""
    if not isinstance(value, (list, tuple)):
        return set()
    result = set()
    for item in value:
        person = _person_id(item)
        if person:
            result.add(person)
    return result


def _policy_paths(base) -> list[Path]:
    roots = [paths.base()]
    try:
        runtime = _runtime_base(base)
        roots.append(runtime)
        # An explicit base/state/memory.sqlite3 path still finds a policy at
        # the deployment root, not only beside the database file.
        if (Path(base).suffix.lower() in {".sqlite3", ".sqlite", ".db"}
                and runtime.name == "state"):
            roots.append(runtime.parent)
    except (OSError, TypeError, ValueError):
        pass
    roots.append(paths.DEFAULTS_DIR)  # packaged default, formerly the copy beside the code
    result = []
    seen = set()
    for root in roots:
        path = root / POLICY_FILENAME
        try:
            key = path.resolve()
        except (OSError, RuntimeError):
            key = path
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _policy_protected_ids(base) -> set[str]:
    """Load the bounded owner policy from module root and runtime base.

    A missing, oversized, non-object, or malformed file contributes nothing;
    the caller keeps the conservative configured-ID behaviour.
    """
    result: set[str] = set()
    for path in _policy_paths(base):
        try:
            if not path.is_file() or path.stat().st_size > MAX_POLICY_BYTES:
                continue
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        result.update(_integer_ids(data.get("protected_user_ids")))
    return result


def _protected_ids(config: dict, base=None) -> set[str]:
    """Collect explicitly configured owner/creator/operator IDs, never names.

    The live deployment has used both flat keys (``owner_ids``) and nested
    sections (``bot_profile.creator_ids``).  Accepting the common spellings is
    backward compatible; nicknames, cards and display names are intentionally
    ignored.  The dedicated owner policy file is always merged, even when a
    config dict/path was supplied by the caller.
    """
    result: set[str] = set()
    if base is not None:
        result.update(_policy_protected_ids(base))
    if not isinstance(config, dict):
        return result

    def walk(node, depth=0):
        if depth > 5 or not isinstance(node, dict):
            return
        for raw_key, value in node.items():
            key = str(raw_key).lower()
            if "group" in key:
                continue
            if key in _PROTECTED_CONTAINERS:
                # A container may be a list, a flat ID, or a mapping such as
                # {"id": 101} / {"101": true}; accept all numeric forms.
                result.update(_numeric_ids(value))
                if isinstance(value, dict):
                    walk(value, depth + 1)
                continue
            if _PROTECTED_KEY.search(key) and _PROTECTED_ID_HINT.search(key):
                result.update(_numeric_ids(value))
                continue
            if isinstance(value, dict):
                walk(value, depth + 1)

    walk(config)

    # user_bias may mark a person by role/flags instead of a top-level ID list.
    user_bias = config.get("user_bias")
    if isinstance(user_bias, dict):
        users = user_bias.get("users")
        if isinstance(users, dict):
            for user_id, profile in users.items():
                if not isinstance(profile, dict):
                    continue
                role = str(profile.get("role", "")).strip().lower()
                label = str(profile.get("attitude", "")).strip().lower()
                flags = {str(key).lower() for key, value in profile.items()
                         if value is True and str(key).lower() in _PROTECTED_FLAGS}
                if role in _PROTECTED_ROLE_VALUES or label in _PROTECTED_ROLE_VALUES or flags:
                    person = _person_id(user_id)
                    if person:
                        result.add(person)
    return result


# ---------------------------------------------------------------------------
# evidence parsing / read-only query
# ---------------------------------------------------------------------------

def _parse_evidence_ids(value) -> tuple[list[int], bool]:
    if value is None or value == "":
        return [], True
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return [], False
    if not isinstance(value, (list, tuple)):
        return [], False
    result: list[int] = []
    valid = True
    for item in value:
        if isinstance(item, bool):
            valid = False
            continue
        text = str(item).strip().upper().removeprefix("M")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", text):
            valid = False
            continue
        number = int(text)
        if number not in result:
            result.append(number)
    if len(result) > MAX_EVIDENCE:
        # The writer caps evidence at eight; never let a malformed row make the
        # lookup unbounded, and do not trust a projection whose evidence was
        # truncated here.
        return result[:MAX_EVIDENCE], False
    return result, valid


def _load_evidence(db: sqlite3.Connection, ids: set[int]) -> dict[int, sqlite3.Row]:
    if not ids or not _table_exists(db, "records"):
        return {}
    values = sorted(ids)
    marks = ",".join("?" for _ in values)
    try:
        # ``SELECT *`` keeps this helper working with older/simplified records
        # tables; optional columns are read defensively below.
        rows = db.execute(
            "SELECT * FROM records WHERE id IN (" + marks + ")",
            values,
        ).fetchall()
    except sqlite3.Error:
        return {}
    result: dict[int, sqlite3.Row] = {}
    for row in rows:
        try:
            result[int(row["id"])] = row
        except (KeyError, TypeError, ValueError):
            continue
    return result


def _evidence_survives(row, scope: str, session: str, target: str, now: float) -> bool:
    if row is None:
        return False
    state = str(_safe_value(row, "state", "active") or "active").strip().lower()
    if state in {"deleted", "retracted"}:
        return False
    kind = str(_safe_value(row, "kind", "") or "").strip().lower()
    if kind == "dialogue":
        return False
    evidence_session = str(_safe_value(row, "session", "") or "")
    if scope == "group":
        if not evidence_session.startswith("group:") or not SESSION.fullmatch(evidence_session):
            return False
    elif evidence_session != session:
        return False
    # Old databases may not have the column yet; 0/empty means no expiry.
    expires = _optional_timestamp(_safe_value(row, "expires", None))
    if expires is not None and expires > 0 and expires <= now:
        return False
    if not str(_safe_value(row, "text", "") or "").strip():
        return False
    speaker = str(_safe_value(row, "user_id", "") or "").strip()
    targets = _direct_targets(_safe_value(row, "direct_targets", ""))
    return speaker == target or target in targets


def _parse_judgment(row, scope: str, session: str, now: float) -> dict | None:
    subject = str(_safe_value(row, "subject_id", "") or "").strip().lower()
    target = str(_safe_value(row, "target_id", "") or "").strip()
    provider = str(_safe_value(row, "provider", "") or "").strip().lower()
    provenance = str(_safe_value(row, "provenance", "") or "").strip().lower()
    author = str(_safe_value(row, "author", "") or "").strip().lower()
    state = str(_safe_value(row, "state", "active") or "active").strip().lower()
    row_session = str(_safe_value(row, "session", "") or "")
    if (subject != HAKIMI_SUBJECT or (provider, provenance) not in
            {('gemini','gemini_manual'),('dsh','dsh_assisted')}
            or state not in {"active", "superseded"}
            or author == "terra" or not SESSION.fullmatch(row_session)):
        return None
    if scope == "group" and not row_session.startswith("group:"):
        return None
    if scope == "private" and row_session != session:
        return None
    try:
        assessment_id = int(row["id"])
    except (KeyError, TypeError, ValueError):
        return None
    evidence_ids, evidence_parsed = _parse_evidence_ids(
        _safe_value(row, "evidence_record_ids", "[]")
    )
    created_at = _as_float(_safe_value(row, "created_at", 0), -1.0)
    if created_at < 0:
        return None
    expires_at = _optional_timestamp(
        _safe_value(row, "expires_at", _safe_value(row, "expires", None))
    )
    try:
        policy = json.loads(_safe_value(row, "affinity_policy", "{}") or "{}")
        inherited = isinstance(policy, dict) and bool(policy.get("inherited_scores", {}).get("affinity"))
    except (ValueError, TypeError, AttributeError):
        inherited = False
    original = str(_safe_value(row, "original_record_id", "") or "")
    seed = str(_safe_value(row, "seed_id", "") or "")
    return {
        "assessment_id": assessment_id,
        "affinity_inherited": inherited,
        "subject_id": subject,
        "target_id": target,
        "session": row_session,
        "scope": scope,
        "author": author,
        "provider": provider,
        "provenance": provenance,
        "state": state,
        "affinity": _as_int_score(_safe_value(row, "affinity", None)),
        "closeness": _as_int_score(_safe_value(row, "closeness", None)),
        "confidence": _as_int_score(_safe_value(row, "confidence", None)),
        "created_at": created_at,
        "expires_at": expires_at,
        "expired": bool(expires_at is not None and expires_at > 0
                        and expires_at <= now),
        "evidence_ids": evidence_ids,
        "evidence_parsed": evidence_parsed,
        "original_record_id": original,
        "seed_id": seed,
        # A replay copies evidence/origin; it is not an independent judgment.
        "identity": (tuple(evidence_ids), original, seed),
        "evidence_ok": False,
        "evidence_times": {},
    }


def _read_judgments(base, person: str, scope: str, session: str,
                    now: float) -> list[dict]:
    """Bounded, parameterised, read-only lookup; returns newest first.

    Active rows are current judgments; superseded rows are retained only as
    corroborating history (the deletion/retraction states are excluded).
    """
    path = _database_path(base)
    db = _readonly_connection(path)
    if db is None:
        return []
    try:
        if not _table_exists(db, "social_assessments"):
            return []
        params: list[Any] = [HAKIMI_SUBJECT, person]
        if scope == "group":
            scope_sql = "session LIKE 'group:%'"
        else:
            scope_sql = "session=?"
            params.append(session)
        params.append(MAX_JUDGMENTS)
        try:
            rows = db.execute(
                "SELECT * FROM social_assessments "
                "WHERE subject_id=? AND target_id=? "
                "AND lower(COALESCE(state,'active')) IN ('active','superseded') "
                "AND ((lower(provider)='gemini' AND lower(provenance)='gemini_manual') "
                "OR (lower(provider)='dsh' AND lower(provenance)='dsh_assisted')) "
                "AND COALESCE(lower(author),'') != 'terra' "
                "AND " + scope_sql + " "
                "ORDER BY created_at DESC,id DESC LIMIT ?",
                params,
            ).fetchall()
        except sqlite3.Error:
            return []
        judgments = []
        for row in rows:
            item = _parse_judgment(row, scope, session, now)
            if item is not None:
                judgments.append(item)
        all_ids = {value for item in judgments for value in item["evidence_ids"]}
        evidence = _load_evidence(db, all_ids)
        for item in judgments:
            evidence_ok = bool(item["evidence_parsed"] and item["evidence_ids"])
            times = {}
            identity_ok = True
            for value in item["evidence_ids"]:
                row = evidence.get(value)
                if row is not None:
                    source_session = str(_safe_value(row, "session", "") or "")
                    scope_ok = (source_session.startswith("group:") if scope == "group"
                                else source_session == session)
                    speaker = str(_safe_value(row, "user_id", "") or "").strip()
                    involved = speaker == person or person in _direct_targets(_safe_value(row, "direct_targets", ""))
                    identity_ok = identity_ok and scope_ok and involved
                if not _evidence_survives(row, scope, session, person, now):
                    evidence_ok = False
                    continue
                at = _optional_timestamp(_safe_value(row, "at", None))
                if at is not None:
                    times[value] = at
            item["identity_ok"] = identity_ok
            item["evidence_ok"] = evidence_ok
            item["evidence_times"] = times
        judgments.sort(key=lambda item: (item["created_at"], item["assessment_id"]), reverse=True)
        return judgments
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------------------
# policy projection
# ---------------------------------------------------------------------------

def _band_for(affinity: int | None, confidence: int | None) -> str:
    if affinity is None or confidence is None or confidence < VALID_MIN_CONFIDENCE:
        return "unknown"
    if affinity >= WARM_MIN_AFFINITY:
        return "warm"
    if affinity >= NEUTRAL_MIN_AFFINITY:
        return "neutral"
    if affinity >= RESERVED_MIN_AFFINITY:
        return "reserved"
    return "distant"


def _empty_profile(person: str, session: str, scope: str, reason: str) -> dict:
    return {
        "subject_id": HAKIMI_SUBJECT,
        "person_id": person,
        "session": session,
        "scope": scope,
        "source": MANUAL_PROVENANCE,
        "band": "unknown",
        "affinity": None,
        "confidence": None,
        "trusted_affinity": None,
        "closeness": None,
        "score_known": False,
        "known": False,
        "initiative_multiplier": BAND_MULTIPLIER["unknown"],
        "guidance": BAND_GUIDANCE["unknown"],
        "reason": reason,
        "assessment_id": None,
        "created_at": None,
        "age_seconds": None,
        "evidence_count": 0,
        "evidence_memory_ids": [],
        "protected": False,
        "at_gate": False,
    }


def _profile_from(judgments: list[dict], person: str, session: str,
                  scope: str, now: float) -> dict:
    active = [item for item in judgments if item["state"] == "active"]
    if not active:
        return _empty_profile(person, session, scope, "no_current_judgment")
    latest = active[0]
    profile = _empty_profile(person, session, scope, "ok")
    profile.update({
        "assessment_id": latest["assessment_id"],
        "created_at": latest["created_at"],
        "age_seconds": max(0, int(now - latest["created_at"])),
        "evidence_count": len(latest["evidence_ids"]),
        "evidence_memory_ids": ["M" + str(value) for value in latest["evidence_ids"]],
    })
    # Scores persist independently of raw evidence retention. Evidence remains
    # necessary for punitive @ gating below, not for ordinary score continuity.
    if not latest.get("identity_ok", True):
        profile["reason"] = "evidence_missing_or_expired"
        return profile
    affinity = latest["affinity"]
    closeness = latest.get("closeness")
    confidence = latest["confidence"]
    # Backfill neutral affinity anchor when early closeness interaction had affinity_delta=0.
    if (affinity is None and isinstance(closeness, (int, float)) and closeness > 0
            and confidence is not None and confidence >= VALID_MIN_CONFIDENCE):
        affinity = 50
    band = _band_for(affinity, confidence)
    profile.update({
        "band": band,
        "affinity": affinity,
        "closeness": closeness,
        "confidence": confidence,
        "trusted_affinity": affinity if band != "unknown" else None,
        "score_known": band != "unknown",
        "known": band != "unknown",
        "initiative_multiplier": BAND_MULTIPLIER[band],
        "guidance": BAND_GUIDANCE[band],
        "reason": ("ok" if band != "unknown" else
                   "low_confidence" if confidence is not None
                   and confidence < VALID_MIN_CONFIDENCE else "score_missing"),
    })
    return profile


def _is_negative_judgment(item: dict) -> bool:
    return (
        item["evidence_ok"]
        and not item.get("affinity_inherited", False)
        and item["affinity"] is not None
        and item["affinity"] <= GATE_MAX_AFFINITY
        and item["confidence"] is not None
        and item["confidence"] >= GATE_MIN_CONFIDENCE
    )


def _has_fresh_evidence(newest: dict, older: dict) -> bool:
    """Newer judgment must add evidence observed after the older judgment.

    Reusing the exact same evidence set (even with a different original row)
    is not new corroboration.  The added evidence's chat timestamp must also be
    strictly after the older judgment was created.
    """
    older_ids = set(older["evidence_ids"])
    for value in newest["evidence_ids"]:
        if value in older_ids:
            continue
        at = newest["evidence_times"].get(value)
        if at is not None and at > older["created_at"]:
            return True
    return False


def _gate_from(judgments: list[dict], now: float) -> bool:
    """True only for the narrow two-independent-judgments rejection gate."""
    active = [item for item in judgments if item["state"] == "active"]
    if not active:
        return False
    latest = active[0]
    age = now - latest["created_at"]
    if latest["expired"] or age < 0 or age > GATE_MAX_AGE_SECONDS:
        return False

    # The newest active row is the anchor.  A prior superseded row may be the
    # historical corroboration, but a newer active positive/unknown is the
    # anchor and therefore cancels rather than falling back to an older pair.
    older = None
    seen: set = set()
    for item in judgments:
        if item["assessment_id"] == latest["assessment_id"]:
            continue
        if item["identity"] in seen:
            continue
        seen.add(item["identity"])
        if item["created_at"] >= latest["created_at"]:
            continue
        older = item
        break
    if older is None or older["expired"]:
        return False
    if not _is_negative_judgment(latest) or not _is_negative_judgment(older):
        return False
    if latest["created_at"] - older["created_at"] < GATE_MIN_GAP_SECONDS:
        return False
    return _has_fresh_evidence(latest, older)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def behavior_for(base, person_id, session, now=None, config=None) -> dict:
    """Return a compact JSON-safe behaviour profile for one person/session.

    Unknown, malformed, missing-schema, or unreadable inputs return a neutral
    profile with ``affinity=None`` and multiplier ``1.0``; no score is invented.
    """
    person = _person_id(person_id)
    scope_info = _scope_for_session(session)
    session_text = str(session or "").strip()
    if not person or scope_info is None:
        scope = "none"
        profile = _empty_profile(person, session_text, scope, "invalid_identity_or_session")
        cfg = _config_for(base, config)
        profile["protected"] = bool(person and person in _protected_ids(cfg, base))
        return profile

    scope, session_text = scope_info
    at = time.time() if now is None else _as_float(now, time.time())
    cfg = _config_for(base, config)
    protected = person in _protected_ids(cfg, base)
    try:
        judgments = _read_judgments(base, person, scope, session_text, at)
    except (OSError, sqlite3.Error, ValueError):
        judgments = []
    profile = _profile_from(judgments, person, session_text, scope, at)
    profile["protected"] = protected
    profile["at_gate"] = bool(not protected and _gate_from(judgments, at))
    return profile


def should_reject_at(base, session, person_id, now=None, config=None) -> bool:
    """Whether this sender's *direct group @* should not create an instant wake.

    This is only a wake veto.  It never deletes, edits, logs out, or hides the
    incoming message; callers must still record/observe the message and may use
    ordinary non-@ participation paths.
    """
    scope_info = _scope_for_session(session)
    person = _person_id(person_id)
    if scope_info is None or not person:
        return False
    scope, session_text = scope_info
    if scope != "group":
        return False
    cfg = _config_for(base, config)
    if person in _protected_ids(cfg, base):
        return False
    at = time.time() if now is None else _as_float(now, time.time())
    try:
        judgments = _read_judgments(base, person, scope, session_text, at)
    except (OSError, sqlite3.Error, ValueError):
        return False
    return _gate_from(judgments, at)


def group_initiative_score(session, score, social):
    """Extra continuous admission cost for the requested group only.

    Smoothstep of the 60/40 social blend: 20/20 has zero gain, 100/100
    has unit gain. Comparing gain*score with the old positive threshold
    is equivalent to raising that threshold by 1/gain. Unknown scores
    cannot establish eligibility for this additional unsolicited pathway.
    """
    if session != 'group:1142791485379513241':
        return score
    affinity, closeness = social.get('affinity'), social.get('closeness')
    if affinity is None or closeness is None:
        return 0.0
    blend = .6 * max(0, min(100, affinity)) + .4 * max(0, min(100, closeness))
    x = max(0.0, min(1.0, (blend - 20.0) / 80.0))
    gain = x*x*(3.0-2.0*x)
    return gain * score
