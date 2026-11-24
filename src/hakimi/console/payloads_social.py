"""Console API: people, person details, relations and the relationship graph."""
from __future__ import annotations

import re
import sqlite3
import time

from hakimi.console.console_db import ApiError, _like, _name_map, _page, _paginate, _self_ids, _session_clause, db_conn
from hakimi.social import identity_links
from hakimi.social.relation_perspective import project_outgoing
from hakimi.console.graph_display import display_score, select_nodes
from hakimi.social.social_fields import HAKIMI_SUBJECT; from hakimi.social.social_query import assessment_evidence, latest_assessments, latest_by_direction; from hakimi.social.interaction_familiarity import interaction_stats, pair_familiarity


def people_payload(base, values):
    q = str(values.get("q", "") or "").strip()[:120]
    group = str(values.get("group", "") or "").strip()
    page, size = _page(values, 1, 100)
    clause, params = _session_clause(group)
    search = ""
    search_params = []
    if q:
        search = (" AND (r.user_id LIKE ? ESCAPE '\\' OR r.nickname LIKE ? ESCAPE '\\' "
                  "OR r.text LIKE ? ESCAPE '\\')")
        search_params = [_like(q)] * 3
    where = "r.user_id GLOB '[1-9]*' AND r.state!='deleted'" + clause + search
    with db_conn(base) as db:
        total = db.execute("SELECT COUNT(DISTINCT r.user_id) FROM records r WHERE " + where,
                           [*params, *search_params]).fetchone()[0]
        rows = db.execute(
            "SELECT r.user_id,COUNT(*) n,SUM(CASE WHEN r.kind='chat' THEN 1 ELSE 0 END) chats,"
            "MAX(r.at) last_at,COUNT(DISTINCT r.session) sessions,"
            "SUM(CASE WHEN r.session LIKE 'private:%' THEN 1 ELSE 0 END) private_count "
            "FROM records r WHERE " + where + " GROUP BY r.user_id ORDER BY last_at DESC, r.user_id DESC "
            "LIMIT ? OFFSET ?", [*params, *search_params, size, (page - 1) * size],
        ).fetchall()
        ids = [row["user_id"] for row in rows]
        names = _name_map(db, ids, group or None)
        assessments = {}
        if ids:
            marks = ",".join("?" for _ in ids)
            assessment_scope = " AND session=?" if group else " AND session LIKE 'group:%'"
            assessment_params = ids + ([group] if group else [])
            for row in db.execute(
                    "SELECT target_id,provenance,COUNT(*) n,MAX(created_at) last_at FROM social_assessments "
                    "WHERE state='active' AND subject_id='hakimi' AND target_id IN (" + marks + ")"
                    + assessment_scope + " GROUP BY target_id,provenance", assessment_params):
                item = assessments.setdefault(row["target_id"], {})
                item[row["provenance"]] = int(row["n"])
    result = []
    for row in rows:
        counts = assessments.get(row["user_id"], {})
        result.append({
            "id": row["user_id"], "nickname": names.get(row["user_id"], "用户" + row["user_id"]),
            "records": int(row["n"]), "chats": int(row["chats"] or 0),
            "sessions": int(row["sessions"]), "private_records": int(row["private_count"] or 0),
            "last_at": row["last_at"],
            "assisted_count": counts.get("dsh_assisted", 0),
            "terra_count": counts.get("terra_assisted", 0),
            "manual_count": counts.get("gemini_manual", 0),
        })
    paging = _paginate(total, page, size)
    paging["returned"] = len(result)
    return {"query": q, "group": group or "all", "people": result, **paging}


def _assessments_for(assessments, person):
    return [item for item in assessments if person in (str(item["subject_id"]), str(item["target_id"]))]


def _evidence_details(db, assessment, limit=8):
    evidence = assessment_evidence(db, assessment)
    return evidence[:limit]


def _staleness(db, assessment):
    ids = []
    for value in assessment.get("evidence_memory_ids") or []:
        text = str(value).upper().removeprefix("M")
        if text.isdigit():
            ids.append(int(text))
    last_at = float(assessment.get("created_at") or 0)
    if ids:
        marks = ",".join("?" for _ in ids)
        row = db.execute("SELECT MAX(at) FROM records WHERE id IN (" + marks + ") AND state!='deleted'",
                         ids).fetchone()
        last_at = max(last_at, float(row[0] or 0))
    target = str(assessment.get("target_id") or "")
    session = str(assessment.get("session") or "")
    new_messages = 0
    if target and session:
        row = db.execute(
            "SELECT COUNT(*) FROM records WHERE kind='chat' AND state!='deleted' AND session=? AND at>? "
            "AND (user_id=? OR direct_targets LIKE ?)",
            (session, last_at, target, '%"' + target + '"%'),
        ).fetchone()
        new_messages = int(row[0] or 0)
    return {
        "based_on_through": last_at, "new_messages": new_messages,
        "needs_update": new_messages > 0,
    }


def person_payload(base, values):
    target = str(values.get("id", "") or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,19}", target):
        raise ApiError(400, "invalid_person", "人物ID格式无效")
    group = str(values.get("group", "") or "").strip()
    clause, params = _session_clause(group)
    with db_conn(base) as db:
        identity = db.execute(
            "SELECT user_id,COUNT(*) n,SUM(CASE WHEN kind='chat' THEN 1 ELSE 0 END) chats,"
            "MIN(at) first_at,MAX(at) last_at,COUNT(DISTINCT session) sessions "
            "FROM records r WHERE r.user_id=? AND r.state!='deleted'" + clause + " GROUP BY r.user_id",
            [target, *params]).fetchone()
        if not identity:
            raise ApiError(404, "person_not_found", "人物在当前范围内没有可见记录")
        names = _name_map(db, [target], group or None)
        alias_rows = db.execute(
            "SELECT nickname,at FROM records r WHERE r.user_id=? AND nickname!=''" + clause +
            " ORDER BY at DESC LIMIT 8", [target, *params]).fetchall()
        aliases = list(dict.fromkeys(str(row["nickname"])[:80] for row in alias_rows))
        sessions = [row[0] for row in db.execute(
            "SELECT DISTINCT session FROM records r WHERE r.user_id=? AND state!='deleted'" + clause +
            " ORDER BY session", [target, *params])]
        manual_rows = db.execute(
            "SELECT r.id,r.kind,r.key,r.session,r.at,r.state,r.source,r.evidence,r.verification,"
            "o.text,o.content_hash,o.provider,o.channel,o.created_at FROM records r "
            "JOIN memory_originals o ON o.record_id=r.id WHERE r.user_id=? AND r.state!='deleted' "
            "AND r.kind!='chat' AND lower(COALESCE(o.provider,'')) LIKE 'gemini%'" + clause +
            " ORDER BY o.created_at DESC,r.id DESC LIMIT 80", [target, *params]).fetchall()
        manual = []
        latest_keys = set()
        for row in manual_rows:
            item_key = (str(row["kind"]), str(row["key"] or ""))
            is_latest = item_key not in latest_keys
            if is_latest:
                latest_keys.add(item_key)
            manual.append({
                "memory_id": "M" + str(row["id"]), "kind": row["kind"], "key": row["key"],
                "session": row["session"], "time": row["created_at"] or row["at"], "state": row["state"],
                "source": row["source"], "verification": row["verification"],
                "text": row["text"], "hash": row["content_hash"], "provider": row["provider"],
                "channel": row["channel"], "relationship_type": "",
                "provenance": "gemini_manual", "is_latest": is_latest,
            })
        # Newest per stable key first; older versions stay available as history.
        manual.sort(key=lambda item: (not item["is_latest"], -(float(item["time"] or 0))), reverse=False)
        assessments = _assessments_for(latest_by_direction(latest_assessments(db, session=group or None)), target)
        for item in assessments:
            item["evidence"] = _evidence_details(db, item)
            item["staleness"] = _staleness(db, item)
        assisted = [item for item in assessments if item["provenance"] == "dsh_assisted"]
        terra = [item for item in assessments if item["provenance"] == "terra_assisted"]
        handwritten_scores = [item for item in assessments if item["provenance"] == "gemini_manual"]
        self_values = _self_ids(db)
        stats = interaction_stats(db, session=group or None, self_ids=self_values, limit=50000)
        hakimi_score, hakimi_basis, hakimi_stats = pair_familiarity(stats, target, "hakimi")
        partners = []
        for key in stats:
            if target not in key:
                continue
            other = key[0] if key[1] == target else key[1]
            if other == target:
                continue
            score, basis, detail = pair_familiarity(stats, target, other)
            partners.append({
                "id": other, "name": "", "score": score, "basis": basis, "stats": detail,
            })
        partners.sort(key=lambda item: (item["stats"] or {}).get("last_at", 0), reverse=True)
        partner_names = _name_map(db, [item["id"] for item in partners if item["id"] != "hakimi"],
                                  group or None)
        for item in partners:
            item["name"] = ("哈基米（本机）" if item["id"] == "hakimi"
                            else partner_names.get(item["id"], "用户" + item["id"]))
        private_seen = db.execute(
            "SELECT 1 FROM records WHERE user_id=? AND session LIKE 'private:%' AND state!='deleted' LIMIT 1",
            (target,)).fetchone()
        related = [{
            "id": "M" + str(row["id"]), "kind": row["kind"], "session": row["session"],
            "time": row["at"], "state": row["state"], "key": row["key"],
            "preview": str(row["text"] or "")[:180],
            "has_original": bool(row["original_id"]),
            "provider": row["model"] or row["channel"],
        } for row in db.execute(
            "SELECT r.id,r.kind,r.session,r.at,r.state,r.key,r.text,r.original_id,r.model,r.channel "
            "FROM records r WHERE r.user_id=? AND r.state!='deleted'" + clause +
            " ORDER BY r.at DESC,r.id DESC LIMIT 20", [target, *params]).fetchall()]
        updated = max([identity["last_at"] or 0, *(item["created_at"] for item in assessments),
                       *(item["time"] for item in manual)], default=0)
        relation_ids = {target}
        for item in assessments:
            for value in (str(item["subject_id"]), str(item["target_id"])):
                if value != HAKIMI_SUBJECT:
                    relation_ids.add(value)
        relation_names = _name_map(db, relation_ids, group or None)
        relation_names[target] = names.get(target, "用户" + target)
        relation_names[HAKIMI_SUBJECT] = "哈基米（本机）"
    from hakimi.social.social_behavior import behavior_for
    behavior = behavior_for(base, target, group or 'group:1')
    try:
        identity_history = identity_links.identity_history_for_person(
            base, target, group or "")
    except (OSError, sqlite3.Error, ValueError):
        identity_history = None
    if not identity_history:
        identity_history = {
            "available": False, "links": [],
            "labels": {"record": "历史原文", "review": "旧摘要",
                       "not_current": "旧记录，不是当前新发生的话"},
        }
    return {
        "behavior": behavior,
        "person": {
            "id": target, "nickname": names.get(target, "用户" + target), "aliases": aliases,
            "sessions": sessions, "records": int(identity["n"]), "chats": int(identity["chats"] or 0),
            "first_at": identity["first_at"], "last_at": identity["last_at"],
            "has_private": bool(private_seen),
        },
        "impressions": {
            "manual": manual,
            "assisted": assisted,
            "terra": terra,
            "handwritten_scores": handwritten_scores,
        },
        "relations": assessments,
        "interaction": {
            "with_hakimi": {"score": hakimi_score, "basis": hakimi_basis, "stats": hakimi_stats},
            "partners": partners[:10],
        },
        "memories": related,
        "identity_history": identity_history,
        "people": relation_names,
        "updated_at": updated,
        "labels": {
            "manual": "她记下的印象 · Gemini 手写原文",
            "assisted": "辅助整理 · DSH 根据公开证据整理，不是当事人内心",
            "terra": "Terra 观察 · 两小时抽样，辅助推断，不是当事人内心；按证据更新，不做时间过期",
            "interaction": "互动熟悉度 · 只统计真实@/引用，不是好感",
        },
    }


def relation_payload(base, values):
    left = str(values.get("a", "") or "").strip()
    right = str(values.get("b", "") or "").strip()
    for value in (left, right):
        if value != HAKIMI_SUBJECT and not re.fullmatch(r"[1-9][0-9]{0,19}", value):
            raise ApiError(400, "invalid_relation", "关系端点格式无效")
    if left == right:
        raise ApiError(400, "invalid_relation", "关系两端不能相同")
    group = str(values.get("group", "") or "").strip()
    with db_conn(base) as db:
        assessments = latest_by_direction([
            item for item in latest_assessments(db, session=group or None)
            if {str(item["subject_id"]), str(item["target_id"])} == {left, right}
        ])
        for item in assessments:
            item["evidence"] = _evidence_details(db, item)
            item["staleness"] = _staleness(db, item)
        names = _name_map(db, [value for value in (left, right) if value != HAKIMI_SUBJECT], group or None)
        stats = interaction_stats(db, session=group or None, self_ids=_self_ids(db), limit=50000)
        score, basis, detail = pair_familiarity(stats, left, right)
    if not assessments and not detail:
        raise ApiError(404, "relation_not_found", "没有这段关系的可见依据")
    labels = {"hakimi": "哈基米（本机）"}
    labels.update({value: names.get(value, "用户" + value) for value in (left, right)
                   if value != HAKIMI_SUBJECT})
    directions = [{
        "direction": item["direction"], "provenance": item["provenance"],
        "relationship_type": item["relationship_type"], "affinity": item["affinity"],
        "closeness": item["closeness"], "confidence": item["confidence"],
        "scores_available": item["scores_available"], "reason": item["reason"],
        "created_at": item["created_at"], "assessment_id": item["id"],
    } for item in assessments]
    return {
        "left": {"id": left, "name": labels.get(left, left)},
        "right": {"id": right, "name": labels.get(right, right)},
        "assessments": assessments,
        "directions": directions,
        "interaction": {"score": score, "basis": basis, "stats": detail},
        "policy": "方向明确：subject->target。辅助整理不是当事人内心；互动熟悉度不是好感。",
    }


def _graph_metric(values):
    """Resolve legacy metric names and the separate 好感/亲近 + 来源 controls."""
    raw = str(values.get("metric", "unified") or "unified").strip().lower()
    source = str(values.get("source", "") or "").strip().lower()
    measure = str(values.get("measure", "") or "").strip().lower()
    if raw in {"affinity", "closeness"}:
        measure = raw
    elif raw in {"manual_affinity", "manual:affinity", "manual-closeness", "manual_closeness"}:
        source, measure = "manual", ("closeness" if "closeness" in raw else "affinity")
    elif raw in {"assisted_affinity", "assisted:affinity", "assisted-closeness", "assisted_closeness"}:
        source, measure = "assisted", ("closeness" if "closeness" in raw else "affinity")
    elif raw in {"terra_affinity", "terra:affinity", "terra-closeness", "terra_closeness"}:
        source, measure = "terra", ("closeness" if "closeness" in raw else "affinity")
    elif raw in {"manual", "assisted", "terra"}:
        source = source or raw
    elif raw in {"interaction", "familiarity"}:
        measure = "interaction"
    if measure not in {"affinity", "closeness", "interaction"}:
        measure = "affinity"
    if measure == "interaction":
        source = "interaction"
    elif source not in {"unified", "manual", "assisted", "terra"}:
        source = "unified"
    label = {
        ("unified", "affinity"): "好感", ("unified", "closeness"): "亲近程度",
        ("manual", "affinity"): "亲笔 · 好感", ("manual", "closeness"): "亲笔 · 亲近程度",
        ("assisted", "affinity"): "辅助 · 好感", ("assisted", "closeness"): "辅助 · 亲近程度",
        ("terra", "affinity"): "Terra 观察 · 好感", ("terra", "closeness"): "Terra 观察 · 亲近程度",
        ("interaction", "interaction"): "互动熟悉度",
    }[(source, measure)]
    return source, measure, label


def _selected_score(edge, source, measure):
    if measure == "interaction":
        return edge["scores"].get("interaction")
    values = (edge.get("measures") or {}).get(measure) or {}
    return values.get(source)


def graph_payload(base, values):
    group = str(values.get("group", "") or "").strip()
    clause, params = _session_clause(group)
    try:
        limit = max(20, min(int(values.get("limit", 35)), 160))
    except (TypeError, ValueError):
        limit = 35
    try:
        threshold = max(0.0, min(float(values.get("threshold", 0)), 100.0))
    except (TypeError, ValueError):
        threshold = 0.0
    source, measure, metric_label = _graph_metric(values)
    metric = "interaction" if measure == "interaction" else source
    focus = str(values.get("focus", "") or "").strip()
    with db_conn(base) as db:
        scan_limit = 500
        rows = db.execute(
            "SELECT r.user_id,COUNT(*) n,SUM(CASE WHEN r.kind='chat' THEN 1 ELSE 0 END) chats,"
            "MAX(r.at) last_at,COUNT(DISTINCT r.session) sessions "
            "FROM records r WHERE r.user_id GLOB '[1-9]*' AND r.state!='deleted'" + clause +
            " GROUP BY r.user_id ORDER BY last_at DESC, r.user_id DESC LIMIT ?",
            [*params, scan_limit]).fetchall()
        people_total = db.execute(
            "SELECT COUNT(DISTINCT r.user_id) FROM records r WHERE r.user_id GLOB '[1-9]*' "
            "AND r.state!='deleted'" + clause, params).fetchone()[0]
        rows = [dict(row) for row in rows]
        # Full-library search may focus a person outside the recent 500 scan.
        # Fetch that exact id rather than pretending the scanned subset is total.
        if focus and focus != HAKIMI_SUBJECT:
            if not re.fullmatch(r"[1-9][0-9]{0,19}", focus):
                raise ApiError(400, "invalid_focus", "焦点人物ID格式无效")
            if not any(row["user_id"] == focus for row in rows):
                exact = db.execute(
                    "SELECT r.user_id,COUNT(*) n,SUM(CASE WHEN r.kind='chat' THEN 1 ELSE 0 END) chats,"
                    "MAX(r.at) last_at,COUNT(DISTINCT r.session) sessions "
                    "FROM records r WHERE r.user_id=? AND r.state!='deleted'" + clause +
                    " GROUP BY r.user_id", [focus, *params]).fetchone()
                if exact:
                    rows.append(dict(exact))
        current_assessments = latest_assessments(db, session=group or None)
        stats = interaction_stats(db, session=group or None, self_ids=_self_ids(db), limit=50000)
        participant_ids = {row["user_id"] for row in rows}
        for item in current_assessments:
            for value in (str(item["subject_id"]), str(item["target_id"])):
                if value != HAKIMI_SUBJECT and re.fullmatch(r"[1-9][0-9]{0,19}", value):
                    participant_ids.add(value)
        for key in stats:
            for value in key:
                if value != HAKIMI_SUBJECT and re.fullmatch(r"[1-9][0-9]{0,19}", value):
                    participant_ids.add(value)
        missing_ids = sorted(participant_ids - {row["user_id"] for row in rows})
        if missing_ids:
            marks = ",".join("?" for _ in missing_ids)
            extra = db.execute(
                "SELECT r.user_id,COUNT(*) n,SUM(CASE WHEN r.kind='chat' THEN 1 ELSE 0 END) chats,"
                "MAX(r.at) last_at,COUNT(DISTINCT r.session) sessions "
                "FROM records r WHERE r.user_id IN (" + marks + ") AND r.state!='deleted'" + clause +
                " GROUP BY r.user_id", [*missing_ids, *params]).fetchall()
            rows.extend(dict(row) for row in extra)
            known = {row["user_id"] for row in rows}
            for value in missing_ids:
                if value not in known:
                    rows.append({"user_id": value, "n": 0, "chats": 0, "last_at": 0, "sessions": 0})
        names = _name_map(db, [row["user_id"] for row in rows], group or None)
        nodes = {}

        def add_person_row(row):
            user_id = str(row["user_id"])
            node_id = "person:" + user_id
            if node_id not in nodes:
                nodes[node_id] = {
                    "id": node_id, "type": "person", "official_id": user_id,
                    "label": names.get(user_id, "用户" + user_id),
                    "records": int(row.get("n") or 0), "chats": int(row.get("chats") or 0),
                    "sessions": int(row.get("sessions") or 0), "last_at": row.get("last_at") or 0,
                    "degree": 0, "scores": {},
                }
            return node_id

        for row in rows:
            add_person_row(row)
        bot_needed = (focus == HAKIMI_SUBJECT
                      or any(item["subject_id"] == HAKIMI_SUBJECT or item["target_id"] == HAKIMI_SUBJECT
                             for item in current_assessments)
                      or any("hakimi" in key for key in stats))
        if bot_needed:
            nodes["assistant:hakimi"] = {
                "id": "assistant:hakimi", "type": "assistant", "official_id": HAKIMI_SUBJECT,
                "label": "哈基米（本机）", "records": 0, "chats": 0, "sessions": 0,
                "last_at": 0, "degree": 0, "scores": {},
            }
        assessments = current_assessments
        edges = {}

        def node_for(person):
            return "assistant:hakimi" if str(person) == HAKIMI_SUBJECT else "person:" + str(person)

        def edge_for(left, right):
            first, second = sorted((left, right))
            key = first + "|" + second
            if key not in edges:
                edges[key] = {
                    "source": first, "target": second, "direction": [],
                    "scores": {"manual": None, "assisted": None, "terra": None, "interaction": None},
                    "closeness": {"manual": None, "assisted": None, "terra": None},
                    "confidence": {"manual": None, "assisted": None, "terra": None},
                    "measures": {
                        "affinity": {"manual": None, "assisted": None, "terra": None},
                        "closeness": {"manual": None, "assisted": None, "terra": None},
                    },
                    "provenances": {}, "relationship_type": "", "reason": "",
                    "assessments": [], "directions": [], "updated_at": 0, "evidence": [],
                    "interaction": None, "_label_at": 0, "_source_at": {},
                }
            return edges[key]

        seen_direction = set()
        for item in assessments:
            left, right = node_for(item["subject_id"]), node_for(item["target_id"])
            if left not in nodes or right not in nodes:
                continue
            edge = edge_for(left, right)
            direction_key = (item["session"], item["direction"], item["provenance"])
            if direction_key in seen_direction:
                continue
            seen_direction.add(direction_key)
            stamp = float(item["created_at"] or 0)
            edge["direction"].append(item["direction"])
            edge["assessments"].append(item["id"])
            edge["updated_at"] = max(edge["updated_at"], stamp)
            if item["provenance"] not in edge["provenances"]:
                edge["provenances"][item["provenance"]] = item["created_at"]
            # Newest record wins the edge label.  DSH must not overwrite an
            # existing Gemini judgement merely because its provenance differs.
            if not edge["relationship_type"] or stamp >= edge["_label_at"]:
                edge["relationship_type"] = item["relationship_type"]
                edge["reason"] = item["reason"]
                edge["_label_at"] = stamp
            metric_key = ("manual" if item["provenance"] == "gemini_manual"
                          else "terra" if item["provenance"] == "terra_assisted" else "assisted")
            if item["scores_available"]:
                previous = float(edge["_source_at"].get(metric_key) or -1)
                if stamp >= previous:
                    edge["_source_at"][metric_key] = stamp
                    edge["scores"][metric_key] = item["affinity"]
                    edge["closeness"][metric_key] = item["closeness"]
                    edge["confidence"][metric_key] = item["confidence"]
                    edge["measures"]["affinity"][metric_key] = item["affinity"]
                    edge["measures"]["closeness"][metric_key] = item["closeness"]
            edge["directions"].append({
                "direction": item["direction"], "session": item["session"],
                "subject_id": item["subject_id"], "target_id": item["target_id"],
                "viewpoint": "self" if item["subject_id"] == HAKIMI_SUBJECT else "observed",
                "provenance": item["provenance"], "relationship_type": item["relationship_type"],
                "affinity": item["affinity"], "closeness": item["closeness"],
                "confidence": item["confidence"], "scores_available": item["scores_available"],
                "scores_note": item["scores_note"], "reason": item["reason"],
                "created_at": item["created_at"], "assessment_id": item["id"],
                "evidence_memory_ids": item["evidence_memory_ids"][:8],
            })

        for key, value in stats.items():
            if "hakimi" in key:
                other = key[0] if key[1] == "hakimi" else key[1]
                left, right = "assistant:hakimi", "person:" + other
            else:
                left, right = "person:" + key[0], "person:" + key[1]
            if left == right or left not in nodes or right not in nodes:
                continue
            edge = edge_for(left, right)
            score, basis, detail = pair_familiarity(stats, key[0], key[1])
            edge["scores"]["interaction"] = score
            edge["measures"].setdefault("interaction", {})["value"] = score
            edge["interaction"] = {"score": score, "basis": basis, "stats": detail}

        # Filter before ranking so hidden edges cannot consume the top-N budget.
        total_edges = len(edges)
        eligible_edges = []
        for edge in edges.values():
            score = display_score(edge, focus, measure, source)
            if threshold > 0 and (score is None or score < threshold):
                continue
            eligible_edges.append(edge)
        total_people = max(len(nodes), int(people_total) + (1 if "assistant:hakimi" in nodes else 0))
        focus_id = ("assistant:hakimi" if focus == HAKIMI_SUBJECT else "person:" + focus) if focus else None
        if focus_id and focus_id not in nodes:
            raise ApiError(404, "focus_not_found", "焦点人物在当前范围内不存在")
        eligible_ids = {value for edge in eligible_edges for value in (edge["source"], edge["target"])}
        if threshold > 0:
            candidates = {key: value for key, value in nodes.items() if key in eligible_ids or key == focus_id}
        else:
            candidates = nodes
        selected = select_nodes(candidates, eligible_edges, limit, focus_id)
        chosen_edges = []
        for edge in eligible_edges:
            if edge["source"] not in selected or edge["target"] not in selected:
                continue
            if focus and measure != "interaction":
                project_outgoing(edge, focus)
            score = edge["display_score"]
            edge["metric_score"] = score
            edge["metric_source"] = source
            edge["measure"] = measure
            edge["has_score"] = (any(value is not None for value in edge["scores"].values())
                                 or any(value is not None for value in edge["closeness"].values()))
            edge["evidence"] = edge["evidence"][:3]
            edge.pop("_label_at", None)
            edge.pop("_source_at", None)
            chosen_edges.append(edge)
        chosen_edges.sort(key=lambda item: (
            item["metric_score"] if item["metric_score"] is not None else -1,
            item["updated_at"], len(item["assessments"]),
        ), reverse=True)
        incident_edges = {}
        for edge in chosen_edges:
            for endpoint in (edge["source"], edge["target"]):
                incident_edges.setdefault(endpoint, []).append(edge)
        output_nodes = []
        for node_id in sorted(selected, key=lambda value: (
                nodes[value]["degree"], nodes[value]["last_at"] or 0, value), reverse=True):
            node = nodes[node_id]
            incident = incident_edges.get(node_id, [])
            def best(metric):
                values = [edge["scores"].get(metric) for edge in incident
                          if edge["scores"].get(metric) is not None]
                return max(values) if values else None
            selected_values = [edge["display_score"] for edge in incident]
            selected_values = [value for value in selected_values if value is not None]
            node["metric_score"] = max(selected_values) if selected_values else None
            node["measure_scores"] = {
                "affinity": {"manual": best("manual"), "assisted": best("assisted"), "terra": best("terra")},
                "closeness": {
                    "manual": max((edge["closeness"].get("manual") for edge in incident
                                   if edge["closeness"].get("manual") is not None), default=None),
                    "assisted": max((edge["closeness"].get("assisted") for edge in incident
                                     if edge["closeness"].get("assisted") is not None), default=None),
                    "terra": max((edge["closeness"].get("terra") for edge in incident
                                  if edge["closeness"].get("terra") is not None), default=None),
                },
                "interaction": best("interaction"),
            }
            node["scores"] = {
                "manual": best("manual"), "assisted": best("assisted"),
                "terra": best("terra"),
                "interaction": best("interaction"),
                "closeness": max(
                    (value for value in node["measure_scores"]["closeness"].values()
                     if value is not None), default=None),
            }
            output_nodes.append(node)
        return {
            "nodes": output_nodes, "edges": chosen_edges,
            "counts": {
                "nodes": len(output_nodes), "edges": len(chosen_edges),
                "total_people_in_scope": total_people, "total_edges_in_scope": total_edges,
                "omitted_nodes": max(0, total_people - len(output_nodes)),
                "omitted_edges": max(0, total_edges - len(chosen_edges)),
                "scan_limit_people": scan_limit,
                "scan_truncated": int(people_total) > scan_limit,
                "scope_note": "总量为全库同范围去重人数；普通视图只扫描最近500人，焦点人物按ID补齐",
                "truncated": total_people > len(output_nodes) or total_edges > len(chosen_edges),
            },
            "selection": {"policy": "direct_neighbours_then_supported_connectivity" if focus else "supported_connectivity",
                          "ranking": ["supported_links", "degree", "interaction_events", "relationship_last_at", "last_at", "id"],
                          "threshold_before_limit": True, "eligible_edges": len(eligible_edges)},
            "limit": limit, "threshold": threshold, "metric": metric,
            "measure": measure, "source": source, "metric_label": metric_label,
            "group": group or "all",
            "focus": focus or None,
            "perspective": {"subject_id": focus or HAKIMI_SUBJECT,
                            "mode": "outgoing" if focus else "overview",
                            "viewpoint": "observed" if focus and focus != HAKIMI_SUBJECT else "self"},
            "legend": {
                "manual": "亲笔评分：模型通过 memory.social 写下的主观印象与可选分数",
                "assisted": "辅助推测：DSH 根据公开证据整理，不是当事人内心",
                "terra": "Terra 观察：gpt-6.1-terra 两小时抽样，有来源日期与置信度；按证据更新，不做时间过期",
                "interaction": "互动熟悉度：只统计真实@/引用、双向与跨日，不是好感",
                "affinity": "好感：可按亲笔/DSH辅助/Terra观察分别查看",
                "closeness": "亲近程度：可按来源分别查看，未知不补零",
                "unknown": "没有评分为待了解，不画假数字",
            },
            "generated_at": time.time(),
        }
