"""Normalised, evidence-backed memory graph.

Only observable facts are added here:
  * a chat record is authored by its official user id;
  * @/reply creates a directed direct_interaction;
  * two consecutive different speakers create co_present (never a social label);
  * notes refer to topics/sources/related memories and explicit versions.

The graph is intentionally SQLite-only.  It has no inference step, no model call
and no server-side graph database.
"""
from __future__ import annotations

import json
import re
from hakimi.memory.graph_index import INDEX_SQL, KIND_LABELS, QUOTE_STARTS, RELATION_LABELS, RELATION_WEIGHT, TYPE_PRIORITY, _clean, _current_text, _event_label, _metadata, _now, _related_ids, _reply_to, _stable_hash, _target_ids, _topic_labels, add_alias, add_edge, ensure_node, event_id, index_record, memory_id, person_id, source_id, topic_id  # noqa: F401 (re-export)


    # Never copy an alias into the global graph_nodes.label.  A private nickname
    # must not be visible through a public graph; labels are derived per request
    # from aliases whose source record is inside the caller's scope.


def _alias_map(db, node_ids, scope_sql, params):
    """Aliases whose source record is visible under exactly the same scope."""
    if not node_ids:
        return {}
    marks = ",".join("?" for _ in node_ids)
    rows = db.execute(
        "SELECT a.node_id,a.alias,a.session,a.source_record_id,a.first_seen,a.last_seen "
        "FROM graph_aliases a JOIN records r ON r.id=a.source_record_id "
        f"WHERE a.node_id IN ({marks}) AND " + scope_sql +
        " AND (r.kind!='chat' OR r.text!='') "
        "ORDER BY a.last_seen DESC,a.rowid DESC LIMIT 800",
        [*node_ids, *params],
    ).fetchall()
    result = {}
    for row in rows:
        result.setdefault(row[0], []).append({
            "alias": row[1], "session": row[2], "source_record_id": row[3],
            "first_seen": row[4], "last_seen": row[5],
        })
    return result


def _visible_edges(db, scope_sql, params, now, limit=20000):
    sql = (
        "SELECT ge.edge_id,ge.subject,ge.relation,ge.object,ge.session,ge.scope_kind,"
        "ge.first_seen,ge.last_seen,ge.weight,"
        "e.record_id,e.evidence_kind,e.event_key,"
        "CASE WHEN r.kind='chat' AND r.text='' THEN '' ELSE e.excerpt END AS excerpt,"
        "e.at AS evidence_at,"
        "r.kind AS record_kind,r.state AS record_state,r.original_id,"
        "CASE WHEN r.kind='chat' AND r.text='' THEN 1 ELSE 0 END AS raw_expired "
        "FROM graph_edges ge JOIN graph_edge_evidence e ON e.edge_id=ge.edge_id "
        "JOIN records r ON r.id=e.record_id "
        "WHERE ge.state='active' AND " + scope_sql + " "
        "ORDER BY ge.last_seen DESC,ge.edge_id,e.at DESC LIMIT ?"
    )
    rows = db.execute(sql, [*params, int(limit) + 1]).fetchall()
    truncated = len(rows) > int(limit)
    if truncated:
        rows = rows[:int(limit)]
    edges = {}
    for row in rows:
        edge = edges.get(row[0])
        if edge is None:
            edge = {
                "edge_id": row[0], "source": row[1], "target": row[3], "relation": row[2],
                "session": row[4], "scope": row[4], "first_seen": row[13], "last_seen": row[13],
                "weight": 1, "evidence": [], "evidence_count": 0,
                "event_keys": set(),
            }
            edges[row[0]] = edge
        edge["first_seen"] = min(edge["first_seen"], row[13])
        edge["last_seen"] = max(edge["last_seen"], row[13])
        key = row[11]
        if key in edge["event_keys"]:
            continue
        edge["event_keys"].add(key)
        edge["evidence_count"] += 1
        edge["evidence"].append({
            "record_id": row[9], "memory_id": "M" + str(row[9]),
            "kind": row[10], "record_kind": row[14], "state": row[15],
            "at": row[13], "excerpt": row[12],
            "original": bool(row[16]), "raw_expired": bool(row[17]),
        })
    for edge in edges.values():
        # Weight and time range come only from evidence visible in this scope, not
        # from the global edge row that may include another private session.
        edge["weight"] = max(1, len(edge["event_keys"]))
        edge["event_keys"] = len(edge["event_keys"])
    return list(edges.values()), truncated


def _visible_totals(db, scope_sql, params):
    """Exact totals for a truncated scan; never present a row cap as exact."""
    sql = (
        "WITH ve AS (SELECT ge.edge_id,ge.subject,ge.object FROM graph_edges ge "
        "WHERE ge.state='active' AND EXISTS (SELECT 1 FROM graph_edge_evidence e "
        "JOIN records r ON r.id=e.record_id WHERE e.edge_id=ge.edge_id AND " + scope_sql + ")) "
        "SELECT (SELECT count(*) FROM ve), "
        "(SELECT count(*) FROM (SELECT subject AS node FROM ve UNION SELECT object AS node FROM ve))"
    )
    row = db.execute(sql, params).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def _visible_memory_ids(db, scope_sql, params, memory_ids):
    """Filter memory endpoints by the target record's own scope."""
    values = sorted({int(value) for value in memory_ids})
    visible = set()
    for start in range(0, len(values), 500):
        chunk = values[start:start + 500]
        marks = ",".join("?" for _ in chunk)
        rows = db.execute(
            f"SELECT r.id FROM records r WHERE r.id IN ({marks}) AND " + scope_sql,
            [*chunk, *params],
        ).fetchall()
        visible.update(int(row[0]) for row in rows)
    return visible


def _select_overview(db, edges, node_rows, all_ids, limit, scope_sql, params):
    """Connected, varied overview: people/relations first, then balanced types.

    Chat leaves never get to consume the whole cap while people and typed
    relations are present.  Manually-authored notes outrank transient chat when
    the record set is large.
    """
    adjacency = {}
    degree, weight, last_seen, direct_degree = {}, {}, {}, {}
    for edge in edges:
        for node in (edge["source"], edge["target"]):
            adjacency.setdefault(node, []).append(edge)
            degree[node] = degree.get(node, 0) + 1
            weight[node] = weight.get(node, 0) + min(int(edge.get("weight") or 1), 20)
            last_seen[node] = max(last_seen.get(node, 0), edge.get("last_seen", 0) or 0)
        if edge.get("relation") in {"direct_interaction", "mentions", "said", "authored"}:
            direct_degree[edge["source"]] = direct_degree.get(edge["source"], 0) + 1
            direct_degree[edge["target"]] = direct_degree.get(edge["target"], 0) + 1

    manual = {}
    try:
        for row in db.execute("SELECT r.id,r.kind,r.original_id,r.at FROM records r "
                              "WHERE r.kind!='chat' AND " + scope_sql, params).fetchall():
            manual[int(row[0])] = (str(row[1]), bool(row[2]), row[3] or 0)
    except Exception:
        manual = {}

    kind_rank = {"person": 7, "lore": 6, "preference": 5, "episode": 4,
                 "commitment": 3, "reflection": 2, "dialogue": 1, "chat": 0}

    def score(node):
        node_type = node_rows.get(node, {}).get("type", "")
        if node_type == "person":
            return (2 if direct_degree.get(node) else 1, direct_degree.get(node, 0),
                    weight.get(node, 0), degree.get(node, 0), last_seen.get(node, 0), node)
        if node_type == "memory":
            try:
                record_id = int(node.split(":", 1)[1])
            except (IndexError, ValueError):
                record_id = 0
            info = manual.get(record_id)
            if info:
                return (1, kind_rank.get(info[0], 1), weight.get(node, 0),
                        degree.get(node, 0), last_seen.get(node, 0), node)
            return (0, 0, weight.get(node, 0), degree.get(node, 0),
                    last_seen.get(node, 0), node)
        if node_type in {"topic", "source"}:
            return (1, weight.get(node, 0), degree.get(node, 0), last_seen.get(node, 0), node)
        return (0, weight.get(node, 0), degree.get(node, 0), last_seen.get(node, 0), node)

    by_type = {}
    for node in all_ids:
        by_type.setdefault(node_rows.get(node, {}).get("type", ""), []).append(node)
    for nodes in by_type.values():
        nodes.sort(key=score, reverse=True)

    fractions = [("person", .24), ("topic", .14), ("source", .08),
                 ("event", .10), ("assistant", .04), ("memory", .38)]
    selected = set()
    for node_type, fraction in fractions:
        quota = max(1, int(limit * fraction))
        quota = min(quota, max(0, limit - len(selected)))
        for node in by_type.get(node_type, [])[:quota]:
            selected.add(node)

    # Fill remaining places from connected nodes, then from useful long-term
    # leftovers if the visible graph is disconnected.
    while len(selected) < limit:
        connected = {}
        for node in selected:
            for edge in adjacency.get(node, []):
                other = edge["target"] if edge["source"] == node else edge["source"]
                if other not in selected:
                    connected[other] = edge
        pool = connected or (all_ids - selected)
        if not pool:
            break
        eligible = [node for node in pool if node not in selected]
        if not eligible:
            break
        best = max(eligible, key=score)
        selected.add(best)
    return selected


def _node_rows(db, node_ids):
    if not node_ids:
        return {}
    marks = ",".join("?" for _ in node_ids)
    rows = db.execute(
        f"SELECT node_id,node_type,label,scope,created_at,updated_at,state FROM graph_nodes "
        f"WHERE node_id IN ({marks})", list(node_ids),
    ).fetchall()
    return {row[0]: {"id": row[0], "type": row[1], "label": row[2], "scope": row[3],
                     "created_at": row[4], "updated_at": row[5], "state": row[6]} for row in rows}


def _record_visible(store, record_id_value, session, users, cross_groups):
    scope, params = store.scope(session, users, cross_groups, include_inactive=True)
    with store.db() as db:
        row = db.execute("SELECT r.id,r.session,r.kind,r.state FROM records r WHERE r.id=? AND " + scope,
                         [int(record_id_value), *params]).fetchone()
    return dict(row) if row else None


def _focus_node(db, store, *, session, users, cross_groups, focus_type, focus_id, topic):
    if focus_type == "memory" or (focus_id and str(focus_id).upper().startswith("M")):
        value = str(focus_id).upper().removeprefix("M")
        if not value.isdigit():
            return None, "invalid memory id"
        record = _record_visible(store, int(value), session, users, cross_groups)
        if not record:
            return None, "memory not visible in this scope"
        return memory_id(int(value)), None
    if focus_type == "person":
        value = str(focus_id or "")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", value):
            return None, "person_id must be an official QQ user id"
        return person_id(value), None
    if focus_type == "topic" or topic:
        label = re.sub(r"\s+", " ", str(topic or focus_id or "")).strip()
        if not 1 <= len(label) <= 60:
            return None, "topic must be 1-60 characters"
        row = db.execute("SELECT node_id FROM graph_nodes WHERE node_type='topic' AND label=? LIMIT 1",
                         (label,)).fetchone()
        if not row:
            return topic_id(label), None
        return row[0], None
    return None, "focus type is required"


def graph_data(store, *, session, users=(), cross_groups=False, focus_type=None, focus_id=None,
               topic=None, depth=1, limit=40, evidence_limit=3, now=None, rebuild=True):
    """Return a bounded, scope-checked graph payload for tools and the HTML view."""
    now = _now(now)
    if rebuild:
        try:
            store.sync()
            store.rebuild_graph(limit=512)
        except Exception:
            pass
    depth = 2 if int(depth or 1) >= 2 else 1
    limit = max(8, min(int(limit or 40), 80))
    evidence_limit = max(1, min(int(evidence_limit or 3), 8))
    scope_sql, params = store.scope(session, users, cross_groups, include_inactive=True)
    with store.db() as db:
        edges, scan_truncated = _visible_edges(db, scope_sql, params, now)
        # Private-to-public links must not expose the private endpoint merely
        # because the source record is in a visible group.
        memory_endpoints = set()
        for edge in edges:
            for node in (edge["source"], edge["target"]):
                if node.startswith("memory:"):
                    try:
                        memory_endpoints.add(int(node.split(":", 1)[1]))
                    except (IndexError, ValueError):
                        pass
        visible_memories = _visible_memory_ids(db, scope_sql, params, memory_endpoints)
        def endpoint_visible(node):
            if node.startswith("memory:"):
                try:
                    return int(node.split(":", 1)[1]) in visible_memories
                except (IndexError, ValueError):
                    return False
            return True
        edges = [edge for edge in edges
                 if endpoint_visible(edge["source"]) and endpoint_visible(edge["target"])]
        focus, error = (None, None)
        if focus_type or focus_id or topic:
            focus, error = _focus_node(db, store, session=session, users=users,
                                       cross_groups=cross_groups, focus_type=focus_type,
                                       focus_id=focus_id, topic=topic)
            if error:
                return {"focus": None, "nodes": [], "edges": [], "error": error,
                        "scope": "all_groups" if cross_groups else "current", "legend": legend()}
        adjacency = {}
        for edge in edges:
            adjacency.setdefault(edge["source"], []).append(edge)
            adjacency.setdefault(edge["target"], []).append(edge)
        all_ids = set(adjacency)
        node_rows = _node_rows(db, all_ids)

        if focus:
            if focus not in all_ids and not focus.startswith("memory:"):
                return {"focus": focus, "nodes": [], "edges": [], "error": "node not visible in this scope",
                        "scope": "all_groups" if cross_groups else "current", "legend": legend()}
            selected = {focus}
            frontier = {focus}
            per_hop = max(4, limit - 1) if depth == 1 else max(3, (limit - 1) // 2)
            for hop in range(depth):
                candidates = {}
                for node in frontier:
                    for edge in adjacency.get(node, []):
                        other = edge["target"] if edge["source"] == node else edge["source"]
                        if other in selected or not other.startswith(("person:", "topic:", "memory:", "source:", "event:", "assistant:")):
                            continue
                        priority = RELATION_WEIGHT.get(edge["relation"], 1)
                        current = candidates.get(other)
                        rating = (priority, min(int(edge.get("weight") or 1), 20), edge.get("last_seen", 0))
                        if current is None or rating > current[0]:
                            candidates[other] = (rating, edge)
                ranked = sorted(candidates.items(), key=lambda item: item[1][0], reverse=True)
                remaining = max(0, limit - len(selected))
                take = min(per_hop, remaining)
                next_frontier = set()
                for node, _ in ranked[:take]:
                    selected.add(node)
                    next_frontier.add(node)
                frontier = next_frontier
                if not frontier or len(selected) >= limit:
                    break
        else:
            selected = _select_overview(db, edges, node_rows, all_ids, limit, scope_sql, params)

        chosen_edges = [edge for edge in edges
                        if edge["source"] in selected and edge["target"] in selected]
        edge_cap = max(12, min(240, limit * 3))
        chosen_edges.sort(key=lambda edge: (RELATION_WEIGHT.get(edge["relation"], 1),
                                            edge.get("weight", 1), edge.get("last_seen", 0)), reverse=True)
        chosen_edges = chosen_edges[:edge_cap]
        if not selected and chosen_edges:
            selected = {edge["source"] for edge in chosen_edges} | {edge["target"] for edge in chosen_edges}
        node_rows = _node_rows(db, selected)
        aliases = _alias_map(db, [node for node in selected if node.startswith("person:")],
                             scope_sql, params)

        degree = {}
        for edge in chosen_edges:
            degree[edge["source"]] = degree.get(edge["source"], 0) + 1
            degree[edge["target"]] = degree.get(edge["target"], 0) + 1
        nodes = []
        for node in sorted(selected, key=lambda value: (TYPE_PRIORITY.get(node_rows.get(value, {}).get("type", ""), 0), value), reverse=True):
            row = node_rows.get(node)
            if not row:
                continue
            item = dict(row)
            visible_incident = adjacency.get(node, [])
            if visible_incident:
                item['created_at'] = min(edge['first_seen'] for edge in visible_incident)
                item['updated_at'] = max(edge['last_seen'] for edge in visible_incident)
            item["degree"] = degree.get(node, 0)
            if node.startswith("person:"):
                official_id = node.split(":", 1)[1]
                item["official_id"] = official_id
                visible_aliases = aliases.get(node, [])
                # The stored node label is deliberately neutral.  A display label
                # is chosen only from aliases visible in this exact scope.
                item["label"] = visible_aliases[0]["alias"] if visible_aliases else f"用户{official_id}"
                item["aliases"] = visible_aliases[:3]
            if node.startswith("memory:"):
                item["memory_id"] = "M" + node.split(":", 1)[1]
            nodes.append(item)

        output_edges = []
        for edge in chosen_edges:
            item = {key: value for key, value in edge.items() if key not in {"event_keys", "evidence"}}
            item["relation_label"] = RELATION_LABELS.get(edge["relation"], edge["relation"])
            item["direct"] = edge["relation"] in {"direct_interaction", "mentions"}
            item["co_present"] = edge["relation"] == "co_present"
            evidence = sorted(edge["evidence"], key=lambda row: row.get("at") or 0, reverse=True)
            omitted_evidence = max(0, len(evidence) - evidence_limit)
            item["evidence"] = evidence[:evidence_limit]
            item["evidence_omitted"] = omitted_evidence
            item["evidence_count"] = edge.get("evidence_count", len(evidence))
            output_edges.append(item)
        if scan_truncated:
            total_edges, total_nodes = _visible_totals(db, scope_sql, params)
            total_nodes = max(total_nodes, len(all_ids))
            totals_exact = False
        else:
            total_nodes = len(all_ids)
            total_edges = len(edges)
            totals_exact = True
        payload = {
            "scope": "all_groups" if cross_groups else "current",
            "session_key": session,
            "focus": focus,
            "nodes": nodes,
            "edges": output_edges,
            "counts": {
                "nodes": len(nodes), "edges": len(output_edges),
                "total_nodes_in_scope": total_nodes, "total_edges_in_scope": total_edges,
                "omitted_nodes": max(0, total_nodes - len(nodes)),
                "omitted_edges": max(0, total_edges - len(output_edges)),
                "scan_truncated": scan_truncated, "totals_exact": totals_exact,
            },
            "limit": limit, "depth": depth,
            "sampled": scan_truncated or total_nodes > len(nodes) or total_edges > len(output_edges),
            "generated_at": now,
            "legend": legend(),
            "policy": {
                "direct_interaction": "只来自可观察的@或引用回复，不推断亲密度",
                "co_present": "同一会话相近时间的同场出现，不代表朋友/恋人或任何社会关系",
                "weight": "按去重的独立事件/证据计数，co_present按天计，不按消息刷量",
            },
        }
        if focus and focus.startswith("person:"):
            try:
                from hakimi.social.social_query import person_social_relations
                payload["social_relations"] = person_social_relations(
                    db, focus.split(":", 1)[1], session=session, cross_groups=cross_groups)
                payload["policy"]["social"] = (
                    "关系评分按subject->target标注；亲笔与辅助整理分开，"
                    "证据原文失效则数值降级为待了解；不跨私聊。")
            except Exception:
                payload["social_relations"] = []
        return payload


def compact_graph(data, *, char_budget=2400):
    """Compact projection for the native tool; the HTML viewer keeps full data."""
    try:
        budget = max(800, min(int(char_budget), 8000))
    except (TypeError, ValueError):
        budget = 2400
    source_nodes = data.get("nodes") or []
    source_edges = data.get("edges") or []
    nodes = []
    for node in source_nodes:
        item = {key: node[key] for key in
                ("id", "type", "label", "scope", "degree", "memory_id", "official_id")
                if key in node}
        nodes.append(item)
    edges = []
    for edge in source_edges:
        item = {key: edge[key] for key in
                ("source", "target", "relation", "relation_label", "weight",
                 "evidence_count", "direct", "co_present")
                if key in edge}
        evidence = edge.get("evidence") or []
        if evidence:
            first = evidence[0]
            item["evidence"] = [{
                key: (str(first.get(key))[:90] if key == "excerpt" else first.get(key))
                for key in ("memory_id", "record_kind", "state", "at", "excerpt", "raw_expired")
                if key in first
            }]
        edges.append(item)

    # Keep edge endpoints ahead of unrelated leaves so a compact projection still
    # has connected relationships instead of a pile of isolated memory nodes.
    by_id = {node["id"]: node for node in nodes}
    ordered, seen_nodes = [], set()
    for edge in source_edges:
        for endpoint in (edge.get("source"), edge.get("target")):
            if not endpoint or endpoint in seen_nodes:
                continue
            seen_nodes.add(endpoint)
            ordered.append(by_id.get(endpoint, {
                "id": endpoint, "type": endpoint.split(":", 1)[0],
                "label": endpoint, "degree": 0,
            }))
    ordered.extend(node for node in nodes if node["id"] not in seen_nodes)
    nodes = ordered

    # Start with a small balanced projection instead of trimming a huge payload
    # edge-by-edge until no relationships remain.
    nodes = nodes[:max(4, min(len(nodes), min(16, max(5, budget // 200))))]
    edges = edges[:max(2, min(len(edges), min(18, max(4, budget // 150))))]
    node_ids = {node["id"] for node in nodes}
    edges = [edge for edge in edges
             if edge.get("source") in node_ids and edge.get("target") in node_ids]
    if source_edges and not edges:
        first = source_edges[0]
        for endpoint in (first.get("source"), first.get("target")):
            if endpoint and endpoint not in node_ids:
                nodes.append({"id": endpoint, "type": endpoint.split(":", 1)[0],
                              "label": endpoint, "degree": 0})
                node_ids.add(endpoint)
        edges = [{key: first[key] for key in
                  ("source", "target", "relation", "relation_label", "weight",
                   "evidence_count", "direct", "co_present") if key in first}]
        if first.get("evidence"):
            item = dict(edges[0])
            item["evidence"] = [{key: (str(first["evidence"][0].get(key))[:90]
                                       if key == "excerpt" else first["evidence"][0].get(key))
                                 for key in ("memory_id", "record_kind", "state", "at", "excerpt", "raw_expired")
                                 if key in first["evidence"][0]}]
            edges = [item]

    social_relations = []
    for item in (data.get("social_relations") or [])[:8]:
        social_relations.append({
            "direction": item.get("direction"),
            "relationship_type": item.get("relationship_type"),
            "provenance": item.get("provenance"),
            "affinity": item.get("affinity"),
            "closeness": item.get("closeness"),
            "confidence": item.get("confidence"),
            "scores_available": item.get("scores_available"),
            "scores_note": item.get("scores_note") or "",
            "reason": str(item.get("reason") or "")[:140],
            "evidence_memory_ids": (item.get("evidence_memory_ids") or [])[:6],
            "assessment_id": item.get("assessment_id"),
            "original_memory_id": item.get("original_memory_id"),
        })

    payload = {
        "focus": data.get("focus"),
        "scope": data.get("scope"),
        "session_key": data.get("session_key"),
        "nodes": nodes,
        "edges": edges,
        "social_relations": social_relations,
        "counts": dict(data.get("counts") or {}),
        "legend": {"relations": {
            key: value for key, value in (data.get("legend") or {}).get("relations", {}).items()
            if key in {edge.get("relation") for edge in source_edges}
            or key in {"co_present", "direct_interaction"}
        }},
        "policy": data.get("policy") or {},
        "char_budget": budget,
        "followup": ("需要更多证据或完整图时，用 memory.read 读取M编号，"
                     "或运行 memory_graph_view.py 导出查看完整图。"),
    }

    def size():
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    projection_truncated = False
    payload["truncated"] = True
    payload["chars"] = 0
    for _ in range(300):
        counts = payload["counts"]
        counts["projected_nodes"] = len(payload["nodes"])
        counts["projected_edges"] = len(payload["edges"])
        counts["omitted_in_projection"] = max(0, len(source_nodes) - len(payload["nodes"]))
        payload["truncated"] = bool(projection_truncated or counts.get("scan_truncated")
                                    or data.get("sampled")
                                    or len(source_edges) > len(payload["edges"]))
        payload["chars"] = size()
        if payload["chars"] <= budget:
            break
        if len(payload["social_relations"]) > 1:
            payload["social_relations"].pop()
            projection_truncated = True
            continue
        if payload["edges"]:
            payload["edges"].pop()
            projection_truncated = True
            continue
        if payload["nodes"]:
            payload["nodes"].pop()
            projection_truncated = True
            continue
        break
    # Do not leave edges that reference a projected-away endpoint.
    node_ids = {node["id"] for node in payload["nodes"]}
    kept_edges = [edge for edge in payload["edges"]
                  if edge.get("source") in node_ids and edge.get("target") in node_ids]
    if len(kept_edges) != len(payload["edges"]):
        projection_truncated = True
    payload["edges"] = kept_edges
    counts = payload["counts"]
    counts["projected_nodes"] = len(payload["nodes"])
    counts["projected_edges"] = len(payload["edges"])
    counts["omitted_in_projection"] = max(0, len(source_nodes) - len(payload["nodes"]))
    payload["truncated"] = bool(projection_truncated or counts.get("scan_truncated")
                                or data.get("sampled") or len(source_edges) > len(payload["edges"]))
    payload["chars"] = size()
    return payload


def legend():
    return {
        "node_types": {"person": "人物（官方ID）", "topic": "话题", "event": "会话事件",
                       "memory": "记忆条目", "source": "来源", "assistant": "本机助手"},
        "relations": RELATION_LABELS,
        "size": "节点大小仅表示在当前有限图中的连接度；连线粗细为 log2(独立证据数)",
        "co_present": "虚线=同场出现，不是社会关系",
        "direct": "实线粗边=可观察的直接@/回复",
    }
