"""Validate and import the reviewed social seed file."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from hakimi.social.social_base import _utc_day
from hakimi.social.social_fields import HAKIMI_SUBJECT, SEED_PROVENANCE, SocialError, validate_fields
from hakimi.social.social_record import record_social
from hakimi.social.social_schema import ensure_schema


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SocialError("invalid_seed", f"无法读取种子文件：{exc}") from exc


def _seed_items(data):
    if not isinstance(data, dict):
        raise SocialError("invalid_seed", "种子文件必须是JSON对象")
    if str(data.get("source", "")) != "dsh_assisted":
        raise SocialError("invalid_seed", "种子必须标注source=dsh_assisted")
    items = []
    for group_name in ("people", "relations"):
        values = data.get(group_name)
        if not isinstance(values, list):
            raise SocialError("invalid_seed", f"种子缺少{group_name}数组")
        for item in values:
            if not isinstance(item, dict):
                raise SocialError("invalid_seed", f"{group_name}含非对象项")
            items.append((group_name, item))
    return data, items


def validate_seed(path, evidence_path=None):
    """Validate structure and, when given, that every M id/date is real."""
    data = _read_json(path)
    data, items = _seed_items(data)
    evidence = {}
    if evidence_path:
        evidence_data = _read_json(evidence_path)
        for person in evidence_data.get("people") or []:
            for message in person.get("messages") or []:
                if isinstance(message.get("id"), int):
                    evidence[message["id"]] = message
        for pair in evidence_data.get("pairs") or []:
            for message in pair.get("evidence") or []:
                if isinstance(message.get("id"), int):
                    evidence.setdefault(message["id"], message)
        if not evidence:
            raise SocialError("invalid_evidence", "证据文件没有可核对的聊天M编号")
    seen = set()
    report = []
    for group_name, item in items:
        seed_id = str(item.get("seed_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", seed_id):
            raise SocialError("invalid_seed", f"{group_name}项缺少合法seed_id")
        if seed_id in seen:
            raise SocialError("invalid_seed", f"seed_id重复：{seed_id}")
        seen.add(seed_id)
        if item.get("source") != SEED_PROVENANCE:
            raise SocialError("invalid_seed", f"{seed_id}的source必须是dsh_assisted")
        if item.get("provenance", SEED_PROVENANCE) != SEED_PROVENANCE:
            raise SocialError("invalid_seed", f"{seed_id}只能是辅助整理，不能伪装手写原文")
        fields = validate_fields(
            note=item.get("impression"), target_id=item.get("target_id"),
            subject_id=item.get("subject_id", HAKIMI_SUBJECT),
            relationship_type=item.get("relationship_type", "关系印象"),
            affinity=item.get("affinity"), closeness=item.get("closeness"),
            confidence=item.get("confidence"), evidence_memory_ids=item.get("evidence_memory_ids"),
            session=item.get("session", ""), provider="dsh", provenance=SEED_PROVENANCE,
            reason=item.get("reason", ""),
        )
        dates = item.get("evidence_dates") or []
        if dates and not isinstance(dates, list):
            raise SocialError("invalid_seed", f"{seed_id}的evidence_dates必须是数组")
        if evidence:
            for value in fields["evidence"]:
                message = evidence.get(value)
                if not message:
                    raise SocialError("evidence_missing", f"{seed_id}引用了不存在的M{value}")
                if not str(message.get("session", "")).startswith("group:"):
                    raise SocialError("private_evidence", f"{seed_id}引用了私聊证据M{value}")
                if dates and _utc_day(message.get("at")) not in dates:
                    raise SocialError("date_mismatch", f"{seed_id}的M{value}日期与记录不符")
        report.append({"seed_id": seed_id, "group": group_name, "fields": fields})
    return {"ok": True, "version": data.get("version"), "items": len(report), "report": report}


def import_seed(base, seed_path, *, dry_run=False):
    """Explicit, idempotent seed import; never runs on module import."""
    validated = validate_seed(seed_path)
    data = _read_json(seed_path)
    data, items = _seed_items(data)
    if dry_run:
        return {"ok": True, "dry_run": True, "items": len(items), "imported": 0,
                "duplicates": 0, "skipped": [], "validated": validated["items"]}
    from hakimi.memory.memory_store import store as store_factory
    index = store_factory(str(Path(base).resolve()))
    with index.db() as db:
        ensure_schema(db)
    imported = duplicates = 0
    skipped = []
    records = []
    assessed_at = data.get("generated_at")
    try:
        assessed_at = float(assessed_at)
    except (TypeError, ValueError):
        assessed_at = time.time()
    for group_name, item in items:
        try:
            result = record_social(
                base, item["session"], item.get("impression"), item["target_id"],
                subject_id=item.get("subject_id", HAKIMI_SUBJECT),
                relationship_type=item.get("relationship_type", "关系印象"),
                affinity=item.get("affinity"), closeness=item.get("closeness"),
                confidence=item.get("confidence"),
                evidence_memory_ids=item.get("evidence_memory_ids"), provider="dsh",
                provenance=SEED_PROVENANCE, reason=item.get("reason", ""),
                origin_message_id="seed:" + str(item["seed_id"]), seed_id=str(item["seed_id"]),
                now=assessed_at,
            )
        except SocialError as exc:
            skipped.append({"seed_id": item.get("seed_id"), "code": exc.code, "message": exc.message})
            continue
        except (sqlite3.Error, OSError):
            skipped.append({"seed_id": item.get("seed_id"), "code": "import_failed",
                            "message": "导入时数据库写入失败，未建立可见评分"})
            continue
        if result.get("duplicate"):
            duplicates += 1
        else:
            imported += 1
        records.append({"seed_id": item.get("seed_id"), "result": result})
    return {"ok": True, "dry_run": False, "items": len(items), "imported": imported,
            "duplicates": duplicates, "skipped": skipped, "records": records}
