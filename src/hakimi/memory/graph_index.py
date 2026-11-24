"""Write side of the memory graph: node/edge ids and incremental indexing of each stored record."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit


TYPE_PRIORITY = {"memory": 6, "person": 5, "topic": 4, "source": 3, "event": 2, "assistant": 1}
RELATION_LABELS = {
    "said": "发言",
    "authored": "写入",
    "mentions": "@提及",
    "participated_in": "参与会话",
    "discussed_topic": "讨论话题",
    "supported_by": "依据来源",
    "supersedes": "更正/取代",
    "refers_to": "关联",
    "direct_interaction": "直接互动（@/回复）",
    "co_present": "同场出现（非社会关系）",
}
KIND_LABELS = {
    "person": "人物笔记",
    "preference": "偏好",
    "lore": "考据",
    "episode": "经历",
    "reflection": "主观反思",
    "commitment": "约定",
    "chat": "原话",
    "dialogue": "短对话",
}
RELATION_WEIGHT = {
    "said": 4, "authored": 5, "direct_interaction": 6, "mentions": 5,
    "discussed_topic": 3, "supported_by": 3, "supersedes": 4,
    "refers_to": 3, "participated_in": 2, "co_present": 1,
}
QUOTE_STARTS = ("[引用", "[回复", "[转发", "[合并转发", "[CQ:forward", "[CQ:json", "[CQ:xml")


def _current_text(value):
    """Drop quoted/forwarded blocks; their CQ mentions belong to other speakers."""
    text = str(value or "")
    if not any(prefix in text for prefix in QUOTE_STARTS):
        return text
    output = []
    index = 0
    while index < len(text):
        start = next((prefix for prefix in QUOTE_STARTS if text.startswith(prefix, index)), None)
        if not start:
            output.append(text[index])
            index += 1
            continue
        depth = 0
        cursor = index
        while cursor < len(text):
            character = text[cursor]
            if character == "[":
                depth += 1
            elif character == "]":
                depth -= 1
                if depth == 0:
                    cursor += 1
                    break
            cursor += 1
        index = cursor
    return "".join(output)


def _clean(text, limit=180):
    value = str(text or "")
    value = re.sub(r"\[CQ:[^\]]*\]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def _now(value=None):
    return time.time() if value is None else float(value)


def _stable_hash(value, prefix, length=20):
    return prefix + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:length]


def person_id(user_id):
    return "person:" + str(user_id)


def memory_id(record_id):
    return "memory:" + str(record_id)


def topic_id(label):
    normal = re.sub(r"\s+", " ", str(label or "")).strip().casefold()
    return _stable_hash(normal, "topic:")


def source_id(url):
    return _stable_hash(str(url or ""), "source:")


def event_id(session, at):
    try:
        day = datetime.fromtimestamp(float(at), timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        day = "unknown"
    return "event:" + str(session) + ":" + day


def _topic_labels(record, metadata):
    labels = []
    raw_topics = []
    if isinstance(metadata.get("topics"), list):
        raw_topics.extend(metadata["topics"])
    if isinstance(record.get("topics"), str) and record["topics"]:
        try:
            parsed = json.loads(record["topics"])
            if isinstance(parsed, list):
                raw_topics.extend(parsed)
        except ValueError:
            pass
    text = str(record.get("text") or record.get("summary") or "")
    for pair in re.findall(r"《([^《》\n]{2,24})》|#([A-Za-z0-9_\u4e00-\u9fff]{2,24})", text):
        raw_topics.append(next(part for part in pair if part))
    kind = str(record.get("kind") or "preference")
    item_key = str(record.get("key") or "").strip()
    if (item_key and kind in {"lore", "episode", "commitment", "reflection", "preference"}
            and not re.fullmatch(r'(?:\d+|(?:source|discussion|dialogue|legacy)-[0-9a-f]+)', item_key)):
        raw_topics.append(item_key)
    for label in raw_topics:
        normal = re.sub(r"\s+", " ", str(label)).strip()
        if 1 <= len(normal) <= 40 and normal not in labels:
            labels.append(normal)
    return labels[:8]


def _related_ids(record, metadata):
    values = []
    if isinstance(metadata.get("related_memory_ids"), list):
        values.extend(metadata["related_memory_ids"])
    for value in values:
        text = str(value).strip()
        if text.startswith("M"):
            text = text[1:]
        if text.isdigit():
            yield int(text)


def ensure_node(db, node_id, node_type, label, scope, now):
    label = _clean(label, 80) or node_id
    db.execute(
        "INSERT INTO graph_nodes(node_id,node_type,label,scope,created_at,updated_at,state) "
        "VALUES (?,?,?,?,?,?,'active') ON CONFLICT(node_id) DO UPDATE SET "
        "label=CASE WHEN excluded.label!='' THEN excluded.label ELSE graph_nodes.label END,"
        "updated_at=MAX(graph_nodes.updated_at,excluded.updated_at),state='active'",
        (node_id, node_type, label, scope or "", now, now),
    )


def add_alias(db, node, alias, session, record_id, at):
    alias = _clean(alias, 40)
    if not alias or len(alias) < 1:
        return
    db.execute(
        "INSERT INTO graph_aliases(node_id,alias,session,source_record_id,first_seen,last_seen) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(node_id,alias,session,source_record_id) DO UPDATE SET "
        "last_seen=MAX(graph_aliases.last_seen,excluded.last_seen)",
        (node, alias, session, record_id, at, at),
    )


def add_edge(db, subject, relation, obj, session, at, record_id, *,
             evidence_kind="record", event_key=None, excerpt="", scope=None):
    if not subject or not obj or subject == obj:
        return None
    at = _now(at)
    event_key = str(event_key or ("record:" + str(record_id)))
    scope = session if scope is None else scope
    row = db.execute(
        "SELECT edge_id,first_seen,weight FROM graph_edges WHERE subject=? AND relation=? AND object=? AND session=?",
        (subject, relation, obj, session or ""),
    ).fetchone()
    if row:
        edge_id = row[0]
        db.execute(
            "UPDATE graph_edges SET first_seen=MIN(first_seen,?),last_seen=MAX(last_seen,?),state='active' "
            "WHERE edge_id=?", (at, at, edge_id))
    else:
        cursor = db.execute(
            "INSERT INTO graph_edges(subject,relation,object,session,scope_kind,first_seen,last_seen,weight,state) "
            "VALUES (?,?,?,?,?,?,?,1,'active')",
            (subject, relation, obj, session or "", "group" if str(session).startswith("group:") else "private", at, at),
        )
        edge_id = cursor.lastrowid
    db.execute(
        "INSERT INTO graph_edge_evidence(edge_id,record_id,evidence_kind,event_key,excerpt,at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(edge_id,record_id,evidence_kind,event_key) DO UPDATE SET "
        "excerpt=CASE WHEN excluded.excerpt!='' THEN excluded.excerpt ELSE graph_edge_evidence.excerpt END,"
        "at=MIN(graph_edge_evidence.at,excluded.at)",
        (edge_id, int(record_id), evidence_kind, event_key, _clean(excerpt, 180), at),
    )
    weight = db.execute("SELECT COUNT(DISTINCT event_key) FROM graph_edge_evidence WHERE edge_id=?",
                        (edge_id,)).fetchone()[0]
    db.execute("UPDATE graph_edges SET weight=? WHERE edge_id=?", (max(1, int(weight)), edge_id))
    return edge_id


def _metadata(db, record_id):
    row = db.execute("SELECT metadata FROM memory_originals WHERE record_id=? ORDER BY id DESC LIMIT 1",
                     (int(record_id),)).fetchone()
    if not row or not row[0]:
        return {}
    try:
        value = json.loads(row[0])
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _target_ids(record):
    value = record.get("direct_targets")
    targets = []
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                targets.extend(str(item) for item in parsed)
        except ValueError:
            pass
    elif isinstance(value, (list, tuple)):
        targets.extend(str(item) for item in value)
    text = _current_text(record.get("text"))
    targets.extend(re.findall(r"\[CQ:at,qq=([0-9]+)\]", text))
    return list(dict.fromkeys(item for item in targets if re.fullmatch(r"[1-9][0-9]{0,19}", item)))[:12]


def _reply_to(record):
    value = record.get("reply_to")
    if isinstance(value, (int, str)) and re.fullmatch(r"-?[0-9]{1,20}", str(value)):
        return str(value)
    match = re.search(r"\[CQ:reply,id=(-?[0-9]{1,20})\]", _current_text(record.get("text")))
    return match.group(1) if match else ""


def index_record(db, record, *, now=None):
    """Idempotently add one record's nodes/edges/evidence.  Safe to replay."""
    record = dict(record)
    record_id_value = record.get("id")
    session = str(record.get("session") or record.get("session_key") or "")
    if not isinstance(record_id_value, int) or record_id_value <= 0:
        return False
    if not re.fullmatch(r"(?:group|private):[1-9][0-9]{0,19}", session):
        return False
    kind = str(record.get("kind") or "preference")
    if kind == "dialogue":
        return False
    text = str(record.get("text") or record.get("summary") or "")
    if not text.strip():
        return False
    at = _now(record.get("at", record.get("created_at", now)))
    metadata = _metadata(db, record_id_value)
    node = memory_id(record_id_value)
    label = f"M{record_id_value} {KIND_LABELS.get(kind, '记忆')}"
    ensure_node(db, node, "memory", label, session, at)
    event = event_id(session, at)
    ensure_node(db, event, "event", _event_label(session, at), session, at)

    author = str(record.get("user_id") or "")
    if kind == "chat" and re.fullmatch(r"[1-9][0-9]{0,19}", author):
        person = person_id(author)
        ensure_node(db, person, "person", f"用户{author}", "", at)
        if record.get("nickname"):
            add_alias(db, person, record.get("nickname"), session, record_id_value, at)
        add_edge(db, person, "said", node, session, at, record_id_value,
                 excerpt=text, scope=session)
        add_edge(db, person, "participated_in", event, session, at, record_id_value,
                 evidence_kind="participation", excerpt=text, scope=session)
    elif kind != "chat":
        add_edge(db, "assistant:hakimi", "authored", node, session, at, record_id_value,
                 excerpt=text, scope=session)
        ensure_node(db, "assistant:hakimi", "assistant", "哈基米（本机）", "", at)
        if re.fullmatch(r"[1-9][0-9]{0,19}", author):
            person = person_id(author)
            ensure_node(db, person, "person", f"用户{author}", "", at)
            add_edge(db, node, "refers_to", person, session, at, record_id_value,
                     excerpt=text, scope=session)

        # Connect a chosen memory back to the actual triggering conversation.
        # Names mentioned in prose never create identities or social relations.
        origin = str(record.get('origin') or metadata.get('origin_message_id') or '')
        if origin:
            trigger = db.execute("SELECT id,text,at FROM records WHERE kind='chat' AND session=? AND origin=? AND state!='deleted' LIMIT 1",
                                 (session, origin)).fetchone()
            if trigger:
                trigger_node = memory_id(trigger[0])
                ensure_node(db, trigger_node, 'memory', f'M{trigger[0]} 原话', session, trigger[2])
                add_edge(db, node, 'supported_by', trigger_node, session, trigger[2], trigger[0],
                         evidence_kind='trigger', event_key='trigger:'+str(trigger[0]), excerpt=trigger[1])
                add_edge(db, node, 'participated_in', event, session, at, record_id_value,
                         evidence_kind='memory_event', excerpt=text)

    # Topics are explicit labels, titles, hashtags or stable item keys.
    for topic in _topic_labels(record, metadata):
        tid = topic_id(topic)
        ensure_node(db, tid, "topic", topic, "", at)
        add_edge(db, node, "discussed_topic", tid, session, at, record_id_value,
                 evidence_kind="topic", event_key="topic:" + topic, excerpt=topic, scope=session)
        if kind == "chat":
            add_edge(db, event, "discussed_topic", tid, session, at, record_id_value,
                     evidence_kind="topic", event_key="topic:" + topic, excerpt=topic, scope=session)

    # A public HTTPS source is a first-class node; it is never inferred from chat text.
    source = str(record.get("source") or "")
    if source and source.startswith('https://'):
        try:
            host = urlsplit(source).hostname or source
        except ValueError:
            host = source
        sid = source_id(source)
        ensure_node(db, sid, "source", host, "", at)
        add_edge(db, node, "supported_by", sid, session, at, record_id_value,
                 evidence_kind="source", event_key="source:" + source, excerpt=source[:180], scope=session)

    for related in _related_ids(record, metadata):
        target = db.execute("SELECT id,session FROM records WHERE id=? AND state!='deleted'",
                            (related,)).fetchone()
        if target:
            rid = memory_id(related)
            ensure_node(db, rid, "memory", f"M{related}", target[1], at)
            add_edge(db, node, "refers_to", rid, session, at, record_id_value,
                     evidence_kind="related", event_key="related:" + str(related), excerpt=text, scope=session)

    supersedes = record.get("supersedes_id")
    if type(supersedes) is int and supersedes > 0:
        old = db.execute("SELECT id,session FROM records WHERE id=?", (supersedes,)).fetchone()
        if old:
            ensure_node(db, memory_id(supersedes), "memory", f"M{supersedes}", old[1], at)
            add_edge(db, node, "supersedes", memory_id(supersedes), session, at, record_id_value,
                     evidence_kind="supersedes", event_key="supersedes:" + str(supersedes),
                     excerpt=text, scope=session)

    if kind != "chat":
        return True

    # Direct interaction is strictly observable @/reply; it is never inferred.
    self_id = str(record.get("self_id") or "")
    targets = _target_ids(record)
    for target in targets:
        if target == author or target == self_id:
            continue
        person = person_id(target)
        ensure_node(db, person, "person", f"用户{target}", "", at)
        add_edge(db, person_id(author), "mentions", person, session, at, record_id_value,
                 evidence_kind="mention", event_key="mention:" + str(record_id_value), excerpt=text, scope=session)
        add_edge(db, person_id(author), "direct_interaction", person, session, at, record_id_value,
                 evidence_kind="direct", event_key="direct:" + str(record_id_value), excerpt=text, scope=session)

    reply_ref = _reply_to(record)
    if reply_ref:
        target = db.execute("SELECT id,user_id,at FROM records WHERE kind='chat' AND session=? AND origin=? LIMIT 1",
                            (session, reply_ref)).fetchone()
        if target and str(target[1]) != author and re.fullmatch(r"[1-9][0-9]{0,19}", str(target[1])):
            person = person_id(target[1])
            ensure_node(db, person, "person", f"用户{target[1]}", "", at)
            add_edge(db, person_id(author), "direct_interaction", person, session, at, record_id_value,
                     evidence_kind="reply", event_key="direct:" + str(record_id_value),
                     excerpt=text, scope=session)

    # Co-presence is only a same-session, close-in-time observation.  Its weight
    # is one per UTC day, not one per message, so noisy groups cannot dominate.
    previous = db.execute(
        "SELECT id,user_id,at,text FROM records WHERE kind='chat' AND session=? AND user_id!=? AND at<? "
        "AND at>=? AND text!='' ORDER BY at DESC,id DESC LIMIT 1",
        (session, author, at, at - 900),
    ).fetchone()
    if previous and re.fullmatch(r"[1-9][0-9]{0,19}", str(previous[1])):
        other = str(previous[1])
        left, right = sorted((author, other))
        pair_session = session
        ensure_node(db, person_id(left), "person", f"用户{left}", "", at)
        ensure_node(db, person_id(right), "person", f"用户{right}", "", at)
        try:
            day = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OSError):
            day = "unknown"
        event_key = "co_present:" + session + ":" + day
        add_edge(db, person_id(left), "co_present", person_id(right), pair_session, at,
                 record_id_value, evidence_kind="co_present", event_key=event_key,
                 excerpt=text, scope=pair_session)
        # Provenance for the previous speaker is the previous record's own text and
        # time, never the current record's quote or timestamp.
        add_edge(db, person_id(left), "co_present", person_id(right), pair_session,
                 previous[2], previous[0], evidence_kind="co_present", event_key=event_key,
                 excerpt=str(previous[3] or ""), scope=pair_session)
    return True


def _event_label(session, at):
    try:
        day = datetime.fromtimestamp(float(at), timezone.utc).strftime("%m-%d")
    except (TypeError, ValueError, OSError):
        day = "??-??"
    kind, _, value = str(session).partition(":")
    return f"{'群' if kind == 'group' else '私聊'}{value}·{day}"


INDEX_SQL = """
CREATE TABLE IF NOT EXISTS graph_nodes (
 node_id TEXT PRIMARY KEY, node_type TEXT NOT NULL, label TEXT NOT NULL,
 scope TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
 state TEXT NOT NULL DEFAULT 'active');
CREATE INDEX IF NOT EXISTS graph_nodes_type ON graph_nodes(node_type,updated_at);
CREATE INDEX IF NOT EXISTS graph_nodes_label ON graph_nodes(node_type,label);
CREATE TABLE IF NOT EXISTS graph_aliases (
 node_id TEXT NOT NULL, alias TEXT NOT NULL, session TEXT NOT NULL DEFAULT '',
 source_record_id INTEGER NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
 PRIMARY KEY(node_id,alias,session,source_record_id));
CREATE INDEX IF NOT EXISTS graph_aliases_node ON graph_aliases(node_id,last_seen);
CREATE TABLE IF NOT EXISTS graph_edges (
 edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
 subject TEXT NOT NULL, relation TEXT NOT NULL, object TEXT NOT NULL,
 session TEXT NOT NULL DEFAULT '', scope_kind TEXT NOT NULL DEFAULT 'group',
 first_seen REAL NOT NULL, last_seen REAL NOT NULL, weight INTEGER NOT NULL DEFAULT 1,
 state TEXT NOT NULL DEFAULT 'active',
 UNIQUE(subject,relation,object,session));
CREATE INDEX IF NOT EXISTS graph_edges_subject ON graph_edges(subject,relation,state);
CREATE INDEX IF NOT EXISTS graph_edges_object ON graph_edges(object,relation,state);
CREATE INDEX IF NOT EXISTS graph_edges_session ON graph_edges(session,relation,state);
CREATE TABLE IF NOT EXISTS graph_edge_evidence (
 edge_id INTEGER NOT NULL, record_id INTEGER NOT NULL,
 evidence_kind TEXT NOT NULL, event_key TEXT NOT NULL,
 excerpt TEXT NOT NULL DEFAULT '', at REAL NOT NULL,
 PRIMARY KEY(edge_id,record_id,evidence_kind,event_key));
CREATE INDEX IF NOT EXISTS graph_evidence_record ON graph_edge_evidence(record_id);
"""
