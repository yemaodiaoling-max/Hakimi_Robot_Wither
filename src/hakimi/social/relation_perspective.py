"""Read a directed opinion without borrowing its reverse direction."""
from __future__ import annotations


PROVENANCE_SOURCES = {
    "gemini_manual": "manual", "dsh_assisted": "assisted", "terra_assisted": "terra",
}


def outgoing_assessments(directions, subject, target):
    """Newest per source for this exact subject→target, including unknowns."""
    result = {}
    expected = str(subject) + "->" + str(target)
    for item in sorted(directions, key=lambda row: (
            float(row.get("created_at") or 0), int(row.get("assessment_id") or 0)), reverse=True):
        source = PROVENANCE_SOURCES.get(item.get("provenance"))
        if source and source not in result and item.get("direction") == expected:
            result[source] = item
    return result


def project_outgoing(edge, subject):
    """Project only score fields; preserve all directional evidence for detail."""
    node = "assistant:hakimi" if subject == "hakimi" else "person:" + subject
    incident = node in (edge["source"], edge["target"])
    other = edge["target"] if edge["source"] == node else edge["source"]
    target = other.split(":", 1)[-1] if incident else ""
    selected = outgoing_assessments(edge.get("directions", []), subject, target)
    edge["perspective"] = {
        "subject_id": subject, "target_id": target or None,
        "direction": subject + "->" + target if incident else None,
        "viewpoint": "self" if subject == "hakimi" else "observed",
    }
    for source in PROVENANCE_SOURCES.values():
        item = selected.get(source) or {}
        available = bool(item.get("scores_available"))
        for measure in ("affinity", "closeness"):
            edge["measures"][measure][source] = item.get(measure) if available else None
        edge["scores"][source] = edge["measures"]["affinity"][source]
        edge["closeness"][source] = edge["measures"]["closeness"][source]
        edge["confidence"][source] = item.get("confidence") if available else None
