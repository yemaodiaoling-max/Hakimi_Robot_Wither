"""Explicit, idempotent repair of Gemini-manual scores lost by blank notes."""
from __future__ import annotations

import json
import sqlite3
import time

from hakimi.social.social_base import _stored_affinity_policy, _utc_day
from hakimi.social.social_fields import DEFAULT_PROVENANCE, HAKIMI_SUBJECT, _assessment_evidence_ids, _inheritance_metadata, _manual_source, _merge_inherited_evidence, _valid_evidence_rows


def repair_manual_scores(db, *, now=None, dry_run=False):
    """One-time, idempotent repair for Gemini-manual rows that lost old scores.

    Only ``social_assessments`` projection columns and their ``affinity_policy``
    metadata are updated.  Immutable ``records``/``memory_originals`` text and
    hashes are never touched.  The walk follows the real ``supersedes_id`` chain
    only; a deleted/retracted/provider-mismatched/missing-evidence link stops the
    walk instead of being skipped over.  Re-running after a successful repair is
    a no-op.
    """
    at = time.time() if now is None else float(now)
    try:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='social_assessments'"
        ).fetchone()
    except sqlite3.Error:
        exists = None
    if not exists:
        return {"ok": True, "dry_run": bool(dry_run), "repaired": 0,
                "skipped": [], "assessments": []}
    try:
        rows = db.execute(
            "SELECT * FROM social_assessments WHERE subject_id=? "
            "AND ((lower(provider)='gemini' AND provenance='gemini_manual') "
            "OR (lower(provider)='dsh' AND provenance='dsh_assisted')) "
            "AND lower(COALESCE(state,'active'))='active' "
            "AND (affinity IS NULL OR closeness IS NULL OR confidence IS NULL) "
            "ORDER BY created_at DESC,id DESC",
            (HAKIMI_SUBJECT,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {"ok": True, "dry_run": bool(dry_run), "repaired": 0,
                "skipped": [], "assessments": []}
    repaired = []
    skipped = []
    fields = ("affinity", "closeness", "confidence")
    for row in rows:
        missing = [field for field in fields if row[field] is None]
        current_ids = _assessment_evidence_ids(row)
        # Do not resurrect a score onto a row whose own visible evidence has
        # already been deleted/cleared; that would present an unverifiable value.
        current_rows = (_valid_evidence_rows(db, row["session"], current_ids, at)
                        if current_ids else [])
        if current_ids and len(current_rows) != len(current_ids):
            skipped.append({"assessment_id": int(row["id"]),
                            "reason": "current_evidence_unreadable"})
            continue
        scope = {"subject_id": row["subject_id"], "target_id": row["target_id"],
                 "session": row["session"]}
        sources = {}
        visited = set()
        ancestor_id = row["supersedes_id"]
        legacy_receipt = _stored_affinity_policy(row)
        legacy_cross_group = False
        if (ancestor_id is None and str(row["session"]).startswith("group:")
                and row["affinity"] is None
                and type(legacy_receipt.get("previous")) in (int, float)
                and legacy_receipt.get("proposed") is None
                and legacy_receipt.get("applied") is None
                and "本次未评分" in str(legacy_receipt.get("policy_reason", ""))):
            prior = db.execute(
                "SELECT * FROM social_assessments WHERE subject_id=? AND target_id=? "
                "AND provider='gemini' AND provenance=? AND state='active' "
                "AND session LIKE 'group:%' AND created_at<? "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (HAKIMI_SUBJECT, row["target_id"], DEFAULT_PROVENANCE, row["created_at"])
            ).fetchone()
            if prior is not None and prior["affinity"] == legacy_receipt["previous"]:
                ancestor_id = int(prior["id"])
                legacy_cross_group = True
        while missing and ancestor_id is not None:
            ancestor_id = int(ancestor_id)
            if ancestor_id in visited:
                break
            visited.add(ancestor_id)
            ancestor = db.execute(
                "SELECT * FROM social_assessments WHERE id=?", (ancestor_id,)
            ).fetchone()
            if ancestor is None:
                break
            source = _manual_source(db, scope, ancestor, at)
            if source is None:
                # Stop at the explicit invalid link; never jump past it.
                break
            for field in list(missing):
                value = ancestor[field]
                if value is not None:
                    sources[field] = source
                    missing.remove(field)
            ancestor_id = ancestor["supersedes_id"]
        if not sources:
            skipped.append({"assessment_id": int(row["id"]),
                            "reason": "no_verifiable_predecessor"})
            continue
        merged = _merge_inherited_evidence(current_ids, sources, sources)
        merged_rows = _valid_evidence_rows(db, row["session"], merged, at)
        if len(merged_rows) != len(merged):
            skipped.append({"assessment_id": int(row["id"]),
                            "reason": "merged_evidence_unreadable"})
            continue
        policy = _stored_affinity_policy(row)
        if legacy_cross_group:
            policy["repair_basis"] = "legacy_receipt_previous_matches_verified_public_predecessor"
        existing = policy.get("inherited")
        if not isinstance(existing, dict):
            existing = {}
        existing.update(_inheritance_metadata(sources)["inherited"])
        policy["inherited"] = existing
        policy["inherited_scores"] = existing
        policy["inherited_fields"] = sorted(existing)
        policy["source_assessment_ids"] = {
            field: item["source_assessment_id"] for field, item in existing.items()
        }
        policy["inheritance_note"] = (
            "一次性修复：最新active笔记原本缺少分数，沿用版本链或旧回执明确指向的"
            "已记入的旧评分；没有重新打分。"
        )
        if row["affinity"] is None and "affinity" in sources:
            value = int(sources["affinity"]["row"]["affinity"])
            policy.update(previous=value, proposed=None, applied=value, delta=0)
        old_reason = str(policy.get("policy_reason") or "")
        if "一次性修复" not in old_reason:
            policy["policy_reason"] = (
                (old_reason + " " if old_reason else "")
                + "一次性修复：沿用有效旧评分，原文与评分证据均未改写，也没有重新打分。"
            )
        values = {
            field: (int(sources[field]["row"][field])
                    if field in sources and row[field] is None else row[field])
            for field in fields
        }
        dates = sorted({_utc_day(item["at"]) for item in merged_rows
                        if _utc_day(item["at"])})
        times = sorted({float(item["at"]) for item in merged_rows
                        if isinstance(item["at"], (int, float)) and item["at"] > 0})
        item = {
            "assessment_id": int(row["id"]),
            "fields": {field: values[field] for field in sources},
            "source_assessment_ids": {
                field: int(source["source_assessment_id"])
                for field, source in sources.items()
            },
            "evidence_memory_ids": ["M" + str(value) for value in merged],
        }
        if not dry_run:
            db.execute(
                "UPDATE social_assessments SET affinity=?,closeness=?,confidence=?,"
                "evidence_record_ids=?,evidence_dates=?,evidence_times=?,affinity_policy=? "
                "WHERE id=?",
                (values["affinity"], values["closeness"], values["confidence"],
                 json.dumps(merged), json.dumps(dates, ensure_ascii=False),
                 json.dumps(times), json.dumps(policy, ensure_ascii=False),
                 int(row["id"])),
            )
        repaired.append(item)
    return {"ok": True, "dry_run": bool(dry_run), "repaired": len(repaired),
            "skipped": skipped, "assessments": repaired}
