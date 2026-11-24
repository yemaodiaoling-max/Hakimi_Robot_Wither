"""Observable interaction familiarity from real @-mentions and quotes (not affinity)."""
from __future__ import annotations

import sqlite3

from hakimi.social.social_base import PERSON_ID, _reply_target, _row_targets, _utc_day


def familiar_score(stats):
    """Explainable 0..100 observation with a deliberate low-sample unknown."""
    if not stats:
        return None, {"events": 0, "days": 0, "mutual_days": 0, "reason": "还没有可核对的@或引用回复"}
    events = int(stats.get("events", 0))
    days = len(stats.get("days") or ())
    mutual_days = len(stats.get("mutual_days") or ())
    if events < 3 or days < 2:
        return None, {
            "events": events, "days": days, "mutual_days": mutual_days,
            "reason": "样本少，先记为待了解；单纯同场或少量搭话不等于熟悉",
        }
    score = min(100, min(50, events * 4) + min(30, days * 5) + min(20, mutual_days * 5))
    reason = (f"真实@/引用{events}次，覆盖{days}天"
              + (f"，其中{mutual_days}天双向" if mutual_days else "，暂时只有单向")
              + "；只表示公开互动熟悉度，不表示好感或亲密关系")
    return score, {"events": events, "days": days, "mutual_days": mutual_days, "reason": reason}


def interaction_stats(db, *, session=None, self_ids=(), limit=50000):
    """Aggregate only observable @/reply events into canonical person pairs.

    Co-presence alone is never counted.  The canonical pair keys are either
    numeric person ids or the literal ``hakimi``.  ``session=None`` is the whole
    public-group scope; private chats are aggregated only when named explicitly.
    """
    clause = "WHERE r.kind='chat' AND r.state!='deleted'"
    params = []
    if session:
        clause += " AND r.session=?"
        params.append(session)
    else:
        clause += " AND r.session LIKE 'group:%'"
    rows = db.execute(
        "SELECT r.id,r.origin,r.user_id,r.session,r.at,r.direct_targets,r.reply_to,r.self_id,r.text "
        "FROM records r " + clause + " ORDER BY r.at DESC,r.id DESC LIMIT ?",
        [*params, max(100, min(int(limit), 100000))],
    ).fetchall()
    self_set = {str(value) for value in self_ids if value}
    self_set.update(str(row["self_id"]) for row in rows if row["self_id"])
    origins = {}
    for row in rows:
        origins[(row["session"], str(row["origin"]))] = (str(row["user_id"]), row["at"])
    stats = {}
    for row in rows:
        author = str(row["user_id"])
        if not PERSON_ID.fullmatch(author):
            continue
        refs = _row_targets(row)
        reply_to = str(row["reply_to"] or "") or _reply_target(row["text"])
        if reply_to:
            target = origins.get((row["session"], reply_to))
            if target:
                refs.add(target[0])
        if not refs and not str(row["text"] or "").strip():
            # Retention may erase chat text after the graph projection has
            # already recorded an observed @/reply.  Recover the endpoint from
            # that evidence instead of silently dropping the interaction.
            try:
                recovered = db.execute(
                    "SELECT ge.object FROM graph_edges ge "
                    "JOIN graph_edge_evidence e ON e.edge_id=ge.edge_id "
                    "WHERE e.record_id=? AND ge.relation='direct_interaction' "
                    "AND ge.subject=? AND ge.state='active'",
                    (row["id"], "person:" + author),
                ).fetchall()
            except sqlite3.Error:
                recovered = []
            for value in recovered:
                target = str(value[0] or "").split(":", 1)[-1]
                if PERSON_ID.fullmatch(target):
                    refs.add(target)
        refs = {value for value in refs if PERSON_ID.fullmatch(value)}
        refs.discard(author)
        author_node = "hakimi" if author in self_set else author
        for target in refs:
            target_node = "hakimi" if target in self_set else target
            if author_node == target_node:
                continue
            key = tuple(sorted((author_node, target_node)))
            item = stats.setdefault(key, {
                "events": 0, "days": set(), "mutual_days": set(),
                "directions": {}, "direction_events": {}, "last_at": 0,
                "evidence": [], "sessions": set(),
            })
            item["events"] += 1
            day = _utc_day(row["at"])
            if day:
                item["days"].add(day)
                # Only the actual speaker's direction gains this day.  Adding
                # both endpoints here used to turn every one-way @ into mutual.
                item["directions"].setdefault(author_node, set()).add(day)
            item["direction_events"][author_node] = item["direction_events"].get(author_node, 0) + 1
            item["last_at"] = max(item["last_at"], float(row["at"] or 0))
            item["sessions"].add(row["session"])
            if len(item["evidence"]) < 20:
                item["evidence"].append(int(row["id"]))
    for item in stats.values():
        # A mutual day means both directions had at least one event on that UTC
        # day.  Keep this explicit rather than inferring closeness.
        direction_items = list(item["directions"].items())
        if len(direction_items) >= 2:
            (_, days_a), (_, days_b) = direction_items[0], direction_items[1]
            item["mutual_days"] = set(days_a) & set(days_b)
        item["sessions"] = sorted(item["sessions"])
    return stats


def pair_stats(stats, left, right):
    key = tuple(sorted((str(left), str(right))))
    item = stats.get(key)
    if not item:
        return None
    directions = item.get("directions") or {}
    return {
        "left": key[0], "right": key[1],
        "events": item["events"],
        "days": sorted(item.get("days") or ()),
        "mutual_days": sorted(item.get("mutual_days") or ()),
        "direction_events": dict(item.get("direction_events") or {}),
        "direction_days": {key: sorted(value) for key, value in directions.items()},
        "last_at": item.get("last_at", 0),
        "evidence_memory_ids": ["M" + str(value) for value in item.get("evidence", [])],
        "sessions": item.get("sessions", []),
    }


def pair_familiarity(stats, left, right):
    item = pair_stats(stats, left, right)
    score, basis = familiar_score(item)
    return score, basis, item
