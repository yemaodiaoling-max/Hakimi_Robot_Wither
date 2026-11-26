"""Bilibili autobiography ingestion, live presence reading, QQ gating, and physical pruning."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from hakimi.memory.lifecycle_rules import expanded_terms
from hakimi.memory.store_base import _summary, content_hash, index_text, vectorize
from hakimi.memory.text import terms

DEFAULT_PRESENCE_PATHS = (
    Path("/opt/hakimi/bili_companion/state/presence.json"),
)

_STATUS_ASK_RE = re.compile(
    r"(?:你|哈基米|猫猫|小猫|宝宝).{0,8}(?:在干嘛|在干什么|忙啥|在看啥|看什么|刷什么|干啥呢)"
    r"|(?:最近|今天|刚才|昨晚).{0,8}(?:看.*视频|刷.*b站|看.*直播|b站.*看|哔哩哔哩)"
    r"|^(?:在干嘛|在看啥|刷什么呢|看啥呢)[呀啊呢吗？?！!]*$",
    re.I,
)


def purge_record_ids(db: sqlite3.Connection, record_ids: list[int] | tuple[int, ...] | set[int]) -> int:
    """Physically delete records and all search/embedding/graph rows for those IDs."""
    ids = [int(rid) for rid in record_ids if int(rid) > 0]
    if not ids:
        return 0
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for offset in range(0, len(ids), 200):
        batch = ids[offset:offset + 200]
        marks = ",".join("?" for _ in batch)
        if "search" in tables:
            for rid in batch:
                try:
                    db.execute("DELETE FROM search WHERE rowid=?", (rid,))
                except sqlite3.Error:
                    pass
        if "edges" in tables:
            db.execute(f"DELETE FROM edges WHERE record_id IN ({marks})", batch)
        if "embeddings" in tables:
            db.execute(f"DELETE FROM embeddings WHERE record_id IN ({marks})", batch)
        if "embedding_state" in tables:
            db.execute(f"DELETE FROM embedding_state WHERE record_id IN ({marks})", batch)
        if "graph_edge_evidence" in tables:
            db.execute(f"DELETE FROM graph_edge_evidence WHERE record_id IN ({marks})", batch)
        if "graph_aliases" in tables:
            db.execute(f"DELETE FROM graph_aliases WHERE source_record_id IN ({marks})", batch)
        db.execute(f"DELETE FROM records WHERE id IN ({marks})", batch)
    if "graph_edges" in tables and "graph_edge_evidence" in tables:
        db.execute("DELETE FROM graph_edges WHERE edge_id NOT IN (SELECT DISTINCT edge_id FROM graph_edge_evidence)")
    return len(ids)


def sync_bili_autobiography(store, *, now: float | None = None) -> dict:
    """Incrementally ingest validated Bilibili autobiography episodes into MemoryStore."""
    now = time.time() if now is None else float(now)
    path = Path(store.base) / "state" / "bili_autobiography.json"
    if not path.is_file():
        return {"synced": False, "reason": "missing"}
    try:
        stat = path.stat()
        if stat.st_size > 2_000_000:
            return {"synced": False, "reason": "oversized"}
        sig = f"{stat.st_mtime_ns}:{stat.st_size}"
        if getattr(store, "_bili_autobiography_sig", None) == sig:
            return {"synced": False, "reason": "cached"}
        with store.db() as probe:
            meta = probe.execute(
                "SELECT value FROM memory_meta WHERE key='bili_autobiography_sig'"
            ).fetchone()
            if meta and meta[0] == sig:
                store._bili_autobiography_sig = sig
                return {"synced": False, "reason": "meta_cached"}
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict) or snapshot.get("version") != 1 or snapshot.get("source") != "bilibili":
            return {"synced": False, "reason": "invalid_header"}
        episodes = snapshot.get("episodes")
        if not isinstance(episodes, list):
            return {"synced": False, "reason": "invalid_episodes"}
        body = json.dumps(episodes, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if hashlib.sha256(body.encode("utf-8")).hexdigest() != snapshot.get("checksum"):
            return {"synced": False, "reason": "checksum_mismatch"}
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return {"synced": False, "reason": "read_error"}

    inserted = 0
    updated = 0
    purged = 0
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing_rows = db.execute(
            "SELECT id, key, content_hash, expires, state FROM records "
            "WHERE session='self:bilibili' AND channel='bilibili_watch'"
        ).fetchall()
        existing_by_key = {str(r["key"]): dict(r) for r in existing_rows}
        desired_keys: set[str] = set()

        for ep in episodes:
            if not isinstance(ep, dict):
                continue
            key = str(ep.get("key") or "").strip()[:64]
            text = str(ep.get("text") or "").strip()
            if not key or not text:
                continue
            try:
                occurred_at = float(ep.get("occurred_at") or 0)
                expires_at = float(ep.get("expires_at") or 0)
            except (TypeError, ValueError):
                continue
            if occurred_at <= 0 or (expires_at > 0 and expires_at <= now):
                continue
            source = str(ep.get("source") or "")[:240]
            evidence = str(ep.get("evidence") or "")[:520]
            raw_topics = ep.get("topics") if isinstance(ep.get("topics"), list) else []
            topics = [str(t).strip()[:40] for t in raw_topics if str(t).strip()][:8]
            desired_keys.add(key)

            digest = content_hash(text)
            existing = existing_by_key.get(key)
            if existing is None:
                store._put(
                    db,
                    {
                        "session_key": "self:bilibili",
                        "person_id": "SELF",
                        "origin_message_id": key,
                        "kind": "episode",
                        "key": key,
                        "note": text,
                        "created_at": occurred_at,
                        "expires_at": expires_at,
                        "source": source,
                        "evidence": evidence,
                        "topics": topics,
                        "shared": 1,
                        "channel": "bilibili_watch",
                        "provider": "bilibili",
                        "verification": "bili_digest",
                    },
                    "episode",
                )
                inserted += 1
            elif (
                existing["content_hash"] != digest
                or abs(float(existing["expires"] or 0) - expires_at) > 1.0
                or existing["state"] != "active"
            ):
                rid = int(existing["id"])
                try:
                    tokens = store._insert_search(db, rid, text, key)
                except sqlite3.Error:
                    tokens = expanded_terms(index_text(text) + " " + key, lambda value: terms(value, limit=32000))
                topics_json = json.dumps(topics, ensure_ascii=False)
                db.execute(
                    "UPDATE records SET text=?, summary=?, vector=?, content_hash=?, at=?, expires=?, "
                    "source=?, evidence=?, topics=?, state='active', shared=1 WHERE id=?",
                    (
                        text,
                        _summary(text),
                        vectorize(tokens),
                        digest,
                        occurred_at,
                        expires_at,
                        source,
                        evidence,
                        topics_json,
                        rid,
                    ),
                )
                db.execute("DELETE FROM edges WHERE record_id=?", (rid,))
                relations = [("record:" + str(rid), "in", "self:bilibili")]
                for pair in sorted(set(re.findall(r"《([^《》\n]{2,24})》|#([A-Za-z0-9_\u4e00-\u9fff]{2,24})", text)))[:8]:
                    relations.append(("record:" + str(rid), "topic", next(part for part in pair if part)))
                if key:
                    relations.append(("record:" + str(rid), "topic", key))
                db.executemany("INSERT OR IGNORE INTO edges VALUES (?,?,?,?)", [(rid, *edge) for edge in relations])
                try:
                    from hakimi.memory.graph_index import index_record
                    index_record(
                        db,
                        {
                            "id": rid,
                            "session": "self:bilibili",
                            "kind": "episode",
                            "text": text,
                            "summary": _summary(text),
                            "user_id": "SELF",
                            "key": key,
                            "source": source,
                            "at": occurred_at,
                            "nickname": "",
                            "direct_targets": "",
                            "reply_to": "",
                            "self_id": "",
                            "origin": key,
                            "supersedes_id": None,
                            "topics": topics_json,
                        },
                    )
                except (sqlite3.Error, ValueError, TypeError):
                    pass
                updated += 1

        stale_ids = [
            int(r["id"])
            for k, r in existing_by_key.items()
            if k not in desired_keys or (float(r["expires"] or 0) > 0 and float(r["expires"]) <= now)
        ]
        if stale_ids:
            purged = purge_record_ids(db, stale_ids)

        db.execute("INSERT OR REPLACE INTO memory_meta VALUES ('bili_autobiography_sig', ?)", (sig,))
    store._bili_autobiography_sig = sig
    return {"synced": True, "inserted": inserted, "updated": updated, "purged": purged}


def read_bili_presence(
    base: str | Path,
    *,
    now: float | None = None,
    max_age: float = 21600.0,
    presence_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
) -> dict | None:
    """Load the freshest live Bilibili presence snapshot within max_age seconds."""
    now = time.time() if now is None else float(now)
    candidates = (
        [Path(p) for p in presence_paths]
        if presence_paths is not None
        else [*DEFAULT_PRESENCE_PATHS, Path(base) / "state" / "hakimi_presence.json"]
    )
    best: dict | None = None
    best_ts = -1.0
    for path in candidates:
        try:
            if not path.is_file() or path.stat().st_size > 65536:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1:
                continue
            ts = float(data.get("updated_at") or 0)
            age = now - ts
            if age < -30 or age > max_age:
                continue
            if ts > best_ts:
                best_ts = ts
                best = data
        except (OSError, ValueError, TypeError):
            continue
    return best


def regular_live_room_context(target, profile_path):
    """Resolve the private account binding locally; expose only relevant room context."""
    from hakimi.core.jsonio import read_json
    profile = read_json(profile_path, {})
    if not isinstance(profile, dict):
        return None
    values = [profile.get(key) for key in ("room_id", "host_name", "qq_official_id", "observation_note")]
    if not all(isinstance(value, str) and value.strip() for value in values):
        return None
    room, name, account, note = values
    is_host = str(target.get("user_id", "")) == account
    text = str(target.get("text", ""))
    named_room = re.search(r"(?<![0-9])" + re.escape(room) + r"(?![0-9])", text)
    usual_room = re.search(
        r"(?:每天|天天|平时|经常|常|总)(?:都)?(?:去|看|蹲|守|逛|待|进)"
        r"(?:的|的是)?(?:哪个|哪家|哪位|谁的?|那个|那家|那场)?(?:[Bb]站的?)?(?:直播|房间)", text)
    stream_topic = any(word in text for word in ("直播", "开播", "代播", "在播", "房主", "主播"))
    if named_room or usual_room or (stream_topic and (is_host or name in text)):
        context = {"room_id": room, "room_owner": name, "background": note}
        if is_host:
            context["current_sender_is_room_owner"] = True
        return context
    return None


def should_inject_qq_bili_presence(text: str, *, admin_turn: bool = False) -> bool:
    """Gate live Bilibili presence injection in QQ turns to save tokens on normal chat."""
    if admin_turn:
        return True
    raw = str(text or "").strip()
    if not raw:
        return False
    return bool(_STATUS_ASK_RE.search(raw))


def format_qq_bili_presence(
    base: str | Path,
    *,
    now: float | None = None,
    presence_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
) -> dict | None:
    """Format live Bilibili presence for QQ context within a strict 135-char JSON budget."""
    pres = read_bili_presence(base, now=now, presence_paths=presence_paths)
    if not pres:
        return None
    watching_str = ""
    wn = pres.get("watching_now")
    if isinstance(wn, dict):
        if wn.get("kind") == "live" and wn.get("room_id"):
            watching_str = f"在看直播间{wn['room_id']}"
        elif wn.get("kind") == "video" and wn.get("bvid"):
            title = str(wn.get("title") or "").strip()
            watching_str = f"在看《{title[:18]}》({wn['bvid']})" if title else f"在看{wn['bvid']}"
    recent_list: list[str] = []
    for item in (pres.get("recent_watched") or [])[:2]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        bvid = str(item.get("bvid") or "").strip()
        label = f"《{title[:14]}》" if title else bvid
        if label:
            recent_list.append(label)

    if not watching_str and not recent_list:
        return None

    out: dict = {
        "note": "同魂B站近况，相关时以第一人称自然提及",
    }
    if watching_str:
        out["watching"] = watching_str
    if recent_list:
        out["recent"] = recent_list

    def _wire_len(d: dict) -> int:
        return len(json.dumps(d, ensure_ascii=False, separators=(",", ":")))

    while _wire_len(out) > 135:
        if len(out.get("recent", [])) > 1:
            out["recent"].pop()
        elif "recent" in out and len(out.get("recent", [])) == 1 and len(out["recent"][0]) > 10:
            out["recent"][0] = out["recent"][0][:10]
        elif "watching" in out and len(out["watching"]) > 18:
            out["watching"] = out["watching"][:18]
        elif "recent" in out and "watching" in out:
            out.pop("recent")
        else:
            out["note"] = "B站近况"
            if _wire_len(out) > 135:
                if "watching" in out:
                    out["watching"] = out["watching"][:12]
                break
    return out
