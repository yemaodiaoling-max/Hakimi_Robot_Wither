"""Source-neutral graph display and explainable, deterministic selection."""
from __future__ import annotations
from collections import defaultdict


def display_score(edge, focus="", measure="affinity", source="unified"):
    """Newest judgement per direction, regardless of authoring tool.

    An explicit newer unknown supersedes an old score. Overview averages the
    known directions; a focus never borrows the incoming opinion.
    """
    sources = {"manual": "gemini_manual", "assisted": "dsh_assisted", "terra": "terra_assisted"}
    latest = {}
    for row in sorted(edge.get("directions", []), key=lambda row: (
            float(row.get("created_at") or 0), int(row.get("assessment_id") or 0)), reverse=True):
        if source == "unified" or row.get("provenance") == sources.get(source):
            latest.setdefault(row["direction"], row)
    endpoints = [value.split(":", 1)[1] for value in (edge["source"], edge["target"])]
    incident = bool(focus and focus in endpoints)
    direction = focus + "->" + endpoints[1 - endpoints.index(focus)] if incident else None
    if focus and not incident:
        score, basis = None, "unknown"
    elif measure == "interaction":
        score = edge["scores"].get("interaction")
        basis = "interaction"
    else:
        rows = [latest.get(direction)] if focus else list(latest.values())
        values = [row[measure] for row in rows if row and row.get("scores_available")
                  and row.get(measure) is not None]
        score = sum(values) / len(values) if values else None
        basis = ("outgoing" if focus else "bidirectional_mean" if len(values) > 1 else "single_direction")
    edge.update(display_score=score, display_basis=basis if score is not None else "unknown",
                display_direction=direction, highlighted=incident, metric_score=score)
    return score


def select_nodes(nodes, edges, limit, focus_id=None):
    """Rank evidence connectivity, interaction volume, freshness; never affinity."""
    adjacency = defaultdict(list)
    for edge in edges:
        detail = (edge.get("interaction") or {}).get("stats") or {}
        rank = (int(bool(edge.get("directions"))), int(detail.get("events") or 0),
                max(float(edge.get("updated_at") or 0), float(detail.get("last_at") or 0)))
        for node, other in ((edge["source"], edge["target"]), (edge["target"], edge["source"])):
            adjacency[node].append((other, rank))
    for node_id, node in nodes.items():
        links = adjacency[node_id]
        node["degree"] = len(links)
        node["selection_basis"] = {"supported_links": sum(rank[0] for _, rank in links),
                                   "interaction_events": sum(rank[1] for _, rank in links),
                                   "relationship_last_at": max((rank[2] for _, rank in links), default=0)}
    def rank(node_id):
        basis = nodes[node_id]["selection_basis"]
        return (-basis["supported_links"], -nodes[node_id]["degree"],
                -basis["interaction_events"], -basis["relationship_last_at"],
                -float(nodes[node_id].get("last_at") or 0), node_id)
    ranked = sorted(nodes, key=rank)
    ordered = []
    if focus_id:
        ordered.append(focus_id)
        # All direct neighbours precede unrelated people, ordered by supported
        # relationship, actual interactions and freshness rather than ID.
        direct = sorted(adjacency[focus_id], key=lambda item: (
            -item[1][0], -item[1][1], -item[1][2], rank(item[0])))
        ordered.extend(other for other, _ in direct)
    included = set(ordered)
    ordered.extend(node for node in ranked if node not in included)
    return set(ordered[:limit])
