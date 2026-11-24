"""First-person Bilibili autobiography condensation, live presence, and bounded pruning."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from bili_companion.runtime.storage import Database
from bili_companion.sync.types import SENSITIVE, canonical

DEFAULT_EPISODE_LIMIT = 240
HIGH_TTL_SECONDS = 60 * 86400
NORMAL_TTL_SECONDS = 14 * 86400
LOW_TTL_SECONDS = 5 * 86400
LIVE_HIGH_TTL_SECONDS = 45 * 86400
LIVE_NORMAL_TTL_SECONDS = 14 * 86400

_EVIDENCE_REF_RE = re.compile(r"\[[0-9,\s]+\]")
_TITLE_RE = re.compile(r"《([^《》\n]{1,64})》")
_PLACEHOLDER_REASON_RE = re.compile(r"^恢复既有视频")
_TOPIC_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,18}|[\u4e00-\u9fff]{2,8}")
_TOPIC_STOPWORDS = frozenset({
    "视频", "展示", "介绍", "讨论", "随后", "然后", "开篇", "开头", "画面",
    "内容", "网友", "博主", "主讲人", "表示", "提到", "认为", "自己", "一个",
})


def _readonly(path: str | Path) -> sqlite3.Connection:
    resolved = Path(path).resolve(strict=True)
    db = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def _atomic_write_json(target_path: str | Path, payload: dict) -> None:
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".bili-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _extract_title(text: str) -> str:
    match = _TITLE_RE.search(str(text or ""))
    return match.group(1).strip() if match else ""


def _clean_text_lines(raw: str, *, strip_title: str = "") -> str:
    text = _EVIDENCE_REF_RE.sub("", str(raw or ""))
    if strip_title:
        text = text.replace(f"《{strip_title}》", "")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines() if ln.strip()]
    deduped: list[str] = []
    for ln in lines:
        if any(ln == prev or (len(ln) >= 12 and ln in prev) for prev in deduped):
            continue
        deduped.append(ln)
    joined = " ".join(deduped)
    return re.sub(r"\s+", " ", joined).strip()


def _clean_reason(raw: str) -> str:
    reason = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not reason or _PLACEHOLDER_REASON_RE.match(reason):
        return ""
    return reason


def _coverage_label(coverage_list: list[dict]) -> str:
    starts: list[float] = []
    ends: list[float] = []
    modalities: set[str] = set()
    for item in coverage_list:
        if not isinstance(item, dict):
            continue
        mod = item.get("modality")
        if mod in {"audio", "video", "subtitle", "mixed"}:
            modalities.add(mod)
        try:
            s = float(item.get("start"))
            e = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        if 0 <= s < e:
            starts.append(s)
            ends.append(e)
    if "mixed" in modalities or ("audio" in modalities and "video" in modalities):
        mod_zh = "音画"
    elif "video" in modalities:
        mod_zh = "画面"
    elif "audio" in modalities:
        mod_zh = "声音"
    elif "subtitle" in modalities:
        mod_zh = "字幕"
    else:
        mod_zh = "片段"
    if starts and ends:
        return f"{int(min(starts))}~{int(max(ends))}s{mod_zh}"
    return f"已看{mod_zh}"


def _extract_topics(base_topics: list[str], title: str, body: str) -> list[str]:
    topics = list(base_topics)
    if title and 2 <= len(title) <= 24 and title not in topics:
        topics.append(title)
    for tok in _TOPIC_TOKEN_RE.findall(f"{title} {body}"):
        if tok in _TOPIC_STOPWORDS or tok in topics:
            continue
        if tok.isdigit():
            continue
        topics.append(tok)
        if len(topics) >= 6:
            break
    return topics[:6]


def write_presence_snapshot(
    watcher_db_path: str | Path,
    target_path: str | Path,
    *,
    now: float | None = None,
) -> dict:
    """Export a compact (<1.8KB) live-presence snapshot from watcher.sqlite3."""
    now = time.time() if now is None else float(now)
    wpath = Path(watcher_db_path)
    if not wpath.is_file():
        return {}

    watching_now = None
    recent_watched: list[dict] = []
    recent_danmaku: list[dict] = []

    with closing(_readonly(wpath)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "watcher_state" in tables:
            row = db.execute("SELECT value FROM watcher_state WHERE id=1").fetchone()
            if row:
                try:
                    st = json.loads(row[0])
                except (ValueError, TypeError):
                    st = {}
                if st.get("live_session") or st.get("state") == "WATCHING_PRIORITY_LIVE":
                    watching_now = {
                        "kind": "live",
                        "room_id": str(st.get("room_id") or ""),
                        "live_session": str(st.get("live_session") or "")[:64],
                        "position_sec": round(float(st.get("live_position") or 0), 1),
                        "state": str(st.get("state") or "WATCHING_PRIORITY_LIVE"),
                    }
                elif isinstance(st.get("video"), dict) and st["video"].get("bvid"):
                    v = st["video"]
                    bvid = str(v["bvid"])
                    reason = ""
                    summary = ""
                    if "video_history" in tables:
                        vh = db.execute(
                            "SELECT selection_reason, summary FROM video_history WHERE bvid=?",
                            (bvid,),
                        ).fetchone()
                        if vh:
                            reason = _clean_reason(vh["selection_reason"])
                            summary = str(vh["summary"] or "")
                    title = str(v.get("title") or "").strip() or _extract_title(summary)
                    watching_now = {
                        "kind": "video",
                        "bvid": bvid,
                        "title": title[:80],
                        "position_sec": round(float(v.get("position") or 0), 1),
                        "duration_sec": round(float(v["duration"]), 1) if v.get("duration") else None,
                        "observations": int(v.get("observations") or 0),
                        "reason": reason[:80],
                    }

        if "video_history" in tables:
            has_comments = "comment_receipts" in tables
            rows = db.execute(
                "SELECT bvid, observations, interest, summary, status, updated_at "
                "FROM video_history WHERE observations>0 AND summary!='' "
                "ORDER BY updated_at DESC LIMIT 3"
            ).fetchall()
            for r in rows:
                bvid = str(r["bvid"])
                title = _extract_title(r["summary"])[:60]
                gist = _clean_text_lines(r["summary"], strip_title=title)[:90]
                comment_text = ""
                if has_comments:
                    crow = db.execute(
                        "SELECT text FROM comment_receipts WHERE bvid=? AND text!='' "
                        "ORDER BY at DESC LIMIT 1",
                        (bvid,),
                    ).fetchone()
                    if crow:
                        comment_text = str(crow["text"] or "")[:50]
                recent_watched.append({
                    "bvid": bvid,
                    "title": title,
                    "interest": str(r["interest"] or "neutral"),
                    "status": str(r["status"] or ""),
                    "updated_at": round(float(r["updated_at"] or 0), 1),
                    "gist": gist,
                    "my_comment": comment_text,
                })

        if "danmaku_attempts" in tables:
            drows = db.execute(
                "SELECT room, text, at FROM danmaku_attempts "
                "WHERE state='sent' AND at>=? ORDER BY at DESC LIMIT 2",
                (now - 43200,),
            ).fetchall()
            for dr in drows:
                recent_danmaku.append({
                    "room": str(dr["room"]),
                    "text": str(dr["text"] or "")[:60],
                    "at": round(float(dr["at"] or 0), 1),
                })

    payload = {
        "version": 1,
        "updated_at": round(now, 3),
        "watching_now": watching_now,
        "recent_watched": recent_watched,
        "recent_danmaku": recent_danmaku,
    }
    while len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 1800:
        if any(len(w["gist"]) > 40 for w in payload["recent_watched"]):
            for w in payload["recent_watched"]:
                w["gist"] = w["gist"][:40]
        elif len(payload["recent_watched"]) > 1:
            payload["recent_watched"].pop()
        elif payload["recent_danmaku"]:
            payload["recent_danmaku"].pop()
        else:
            break

    _atomic_write_json(target_path, payload)
    return payload


def _build_video_text(
    *,
    bvid: str,
    title: str,
    cov_label: str,
    reason: str,
    body: str,
    my_comment: str,
) -> str:
    title_part = f"《{title}》" if title else ""
    header = f"[B站看片] {title_part}({bvid}，已看{cov_label})："
    reason_part = f"想看「{reason[:40]}」；" if reason else ""
    comment_part = f"；我留评：「{my_comment[:45]}」" if my_comment else ""
    max_body = max(24, 220 - len(header) - len(reason_part) - len(comment_part))
    trimmed_body = body[:max_body].rstrip("，； ")
    if not trimmed_body:
        trimmed_body = body[:max_body]
    return f"{header}{reason_part}{trimmed_body}{comment_part}"[:220]


def build_autobiography_episodes(
    native_db_path: str | Path,
    watcher_db_path: str | Path,
    *,
    account_id: str = "hakimi-bilibili",
    now: float | None = None,
    limit: int = DEFAULT_EPISODE_LIMIT,
) -> list[dict]:
    """Condense native Bilibili watch records into bounded first-person episodes."""
    now = time.time() if now is None else float(now)
    npath = Path(native_db_path)
    wpath = Path(watcher_db_path)
    if not npath.is_file() and not wpath.is_file():
        return []

    video_notes: dict[str, tuple[str, int]] = {}
    live_notes: dict[tuple[str, str], str] = {}
    live_evidence_groups: dict[tuple[str, str], list[dict]] = {}

    if npath.is_file():
        with closing(_readonly(npath)) as ndb:
            ntables = {r[0] for r in ndb.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "notes" in ntables:
                for row in ndb.execute("SELECT id, scope, text, revision FROM notes").fetchall():
                    try:
                        sc = json.loads(row["scope"])
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(sc, list) or len(sc) != 4 or sc[0] != "bilibili" or sc[1] != account_id:
                        continue
                    if sc[2] == "video":
                        video_notes[str(sc[3])] = (str(row["text"] or ""), int(row["revision"] or 1))
                    elif sc[2] == "live_room":
                        live_notes[(str(sc[3]), str(row["id"]))] = str(row["text"] or "")
            if "evidence" in ntables:
                ev_rows = ndb.execute(
                    "SELECT scope, session, observed, text, payload FROM evidence "
                    "WHERE platform='bilibili' AND account=? AND scope LIKE '%\"live_room\"%' "
                    "ORDER BY observed DESC",
                    (account_id,),
                ).fetchall()
                for er in ev_rows:
                    try:
                        payload = json.loads(er["payload"])
                    except (ValueError, TypeError):
                        continue
                    if payload.get("attribution") != "observation":
                        continue
                    mod = payload.get("modality")
                    if mod not in {"audio", "video", "subtitle", "mixed"}:
                        continue
                    key = (str(er["scope"]), str(er["session"]))
                    live_evidence_groups.setdefault(key, []).append({
                        "observed": float(er["observed"] or 0),
                        "text": str(er["text"] or ""),
                        "start": float(payload.get("media_start") or 0),
                        "end": float(payload.get("media_end") or 0),
                        "modality": mod,
                    })

    comments_by_bvid: dict[str, str] = {}
    danmaku_by_session: dict[str, list[str]] = {}
    video_rows: list[sqlite3.Row] = []

    if wpath.is_file():
        with closing(_readonly(wpath)) as wdb:
            wtables = {r[0] for r in wdb.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "comment_receipts" in wtables:
                for cr in wdb.execute(
                    "SELECT bvid, text FROM comment_receipts "
                    "WHERE account=? AND text!='' ORDER BY at ASC, rowid ASC",
                    (account_id,),
                ).fetchall():
                    comments_by_bvid[str(cr["bvid"])] = str(cr["text"] or "").strip()
            if "danmaku_attempts" in wtables:
                for dr in wdb.execute(
                    "SELECT session, text FROM danmaku_attempts "
                    "WHERE account=? AND state='sent' AND text!='' ORDER BY at DESC",
                    (account_id,),
                ).fetchall():
                    danmaku_by_session.setdefault(str(dr["session"]), []).append(str(dr["text"]).strip())
            if "video_history" in wtables:
                video_rows = list(wdb.execute(
                    "SELECT bvid, selection_reason, observations, interest, summary, coverage, updated_at "
                    "FROM video_history WHERE observations>0 AND summary!='' "
                    "ORDER BY updated_at DESC"
                ).fetchall())

    episodes: list[dict] = []
    seen_keys: set[str] = set()

    for vr in video_rows:
        bvid = str(vr["bvid"] or "").strip()
        if not bvid:
            continue
        obs = int(vr["observations"] or 0)
        interest = str(vr["interest"] or "neutral")
        my_comment = comments_by_bvid.get(bvid, "")
        note_text, note_rev = video_notes.get(bvid, ("", obs))
        raw_summary = note_text or str(vr["summary"] or "")
        if obs <= 0 or not raw_summary.strip():
            continue
        if obs == 1 and interest == "low" and not my_comment:
            continue

        if interest == "high" or bool(my_comment) or obs >= 4:
            importance = "high"
            ttl = HIGH_TTL_SECONDS
        elif interest == "low" and obs >= 2:
            importance = "low"
            ttl = LOW_TTL_SECONDS
        else:
            importance = "normal"
            ttl = NORMAL_TTL_SECONDS

        occurred_at = float(vr["updated_at"] or now)
        expires_at = occurred_at + ttl
        if expires_at <= now:
            continue

        title = _extract_title(raw_summary)
        body = _clean_text_lines(raw_summary, strip_title=title)
        if not body or SENSITIVE.search(body):
            continue
        try:
            cov_list = json.loads(vr["coverage"] or "[]")
        except (ValueError, TypeError):
            cov_list = []
        cov_label = _coverage_label(cov_list if isinstance(cov_list, list) else [])
        reason = _clean_reason(vr["selection_reason"])
        text = _build_video_text(
            bvid=bvid,
            title=title,
            cov_label=cov_label,
            reason=reason,
            body=body,
            my_comment=my_comment,
        )
        if SENSITIVE.search(text):
            continue
        key = f"B站观看·{bvid}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        episodes.append({
            "kind": "video",
            "key": key,
            "text": text,
            "source": f"https://www.bilibili.com/video/{bvid}",
            "evidence": f"bilibili:video:{bvid} ({cov_label})",
            "topics": _extract_topics(["B站", "视频"], title, body),
            "occurred_at": occurred_at,
            "expires_at": expires_at,
            "importance": importance,
            "revision": max(1, int(note_rev)),
        })

    for (scope_json, session_id), items in live_evidence_groups.items():
        try:
            sc = json.loads(scope_json)
            room_id = str(sc[3])
        except (ValueError, TypeError, IndexError):
            continue
        sent_danmaku = danmaku_by_session.get(session_id, [])
        if len(items) < 2 and not sent_danmaku:
            continue
        occurred_at = max(it["observed"] for it in items)
        importance = "high" if len(sent_danmaku) >= 2 else "normal"
        ttl = LIVE_HIGH_TTL_SECONDS if importance == "high" else LIVE_NORMAL_TTL_SECONDS
        expires_at = occurred_at + ttl
        if expires_at <= now:
            continue

        snippets: list[str] = []
        for it in items:
            t = _clean_text_lines(it["text"])
            if not t or SENSITIVE.search(t):
                continue
            if any(t == prev or (len(t) >= 10 and t in prev) for prev in snippets):
                continue
            snippets.append(t)
            if len(snippets) >= 3:
                break
        body = "；".join(snippets)
        if not body:
            continue
        cov_label = _coverage_label(items)
        danmaku_part = (
            f"；发了弹幕「{' / '.join(d[:24] for d in sent_danmaku[:2])}」"
            if sent_danmaku
            else ""
        )
        header = f"[B站直播] 房间{room_id}({cov_label})："
        max_body = max(24, 220 - len(header) - len(danmaku_part))
        text = f"{header}{body[:max_body].rstrip('，； ')}{danmaku_part}"[:220]
        if SENSITIVE.search(text):
            continue
        sess_hash = hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:8]
        key = f"B站直播·{sess_hash}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        episodes.append({
            "kind": "live",
            "key": key,
            "text": text,
            "source": f"https://live.bilibili.com/{room_id}",
            "evidence": f"bilibili:live:{room_id}:session:{session_id[:48]} ({cov_label})",
            "topics": _extract_topics(["B站", "直播", f"房间{room_id}"], "", body),
            "occurred_at": occurred_at,
            "expires_at": expires_at,
            "importance": importance,
            "revision": len(items),
        })

    episodes.sort(key=lambda e: (1 if e["importance"] == "high" else 0, float(e["occurred_at"])), reverse=True)
    return episodes[:max(1, int(limit))]


def write_autobiography_snapshot(
    episodes: list[dict],
    target_path: str | Path,
    *,
    now: float | None = None,
) -> int:
    """Atomically write checksummed Bilibili autobiography episodes JSON."""
    now = time.time() if now is None else float(now)
    checksum = hashlib.sha256(canonical(episodes).encode("utf-8")).hexdigest()
    payload = {
        "version": 1,
        "source": "bilibili",
        "generated_at": round(now, 3),
        "checksum": checksum,
        "episodes": episodes,
    }
    _atomic_write_json(target_path, payload)
    return len(episodes)


def prune_bili_databases(
    native_db_path: str | Path,
    watcher_db_path: str | Path | None = None,
    bridge_db_path: str | Path | None = None,
    *,
    account_id: str = "hakimi-bilibili",
    now: float | None = None,
    max_age_days: float = 45.0,
    max_evidence: int = 4000,
) -> dict:
    """Delete stale/overflow evidence, orphan notes, and acked/expired bridge outbox payloads."""
    now = time.time() if now is None else float(now)
    cutoff = now - max(1.0, float(max_age_days)) * 86400
    protected_scopes: set[str] = set()

    if watcher_db_path and Path(watcher_db_path).is_file():
        with closing(_readonly(watcher_db_path)) as wdb:
            if wdb.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watcher_state'").fetchone():
                row = wdb.execute("SELECT value FROM watcher_state WHERE id=1").fetchone()
                if row:
                    try:
                        st = json.loads(row[0])
                    except (ValueError, TypeError):
                        st = {}
                    for k in ("video", "bookmark"):
                        v = st.get(k)
                        if isinstance(v, dict) and v.get("bvid"):
                            protected_scopes.add(json.dumps(["bilibili", account_id, "video", str(v["bvid"])], separators=(",", ":")))
                    if st.get("live_session") and st.get("room_id"):
                        protected_scopes.add(json.dumps(["bilibili", account_id, "live_room", str(st["room_id"])], separators=(",", ":")))

    stats = {"deleted_aged_evidence": 0, "deleted_overflow_evidence": 0, "deleted_notes": 0, "deleted_outbox": 0}
    npath = Path(native_db_path)
    if npath.is_file():
        ndb_wrap = Database(npath)
        with ndb_wrap.connect(write=True) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "qq_viewing_sources" in tables:
                for qr in db.execute(
                    "SELECT bvid FROM qq_viewing_sources WHERE observed>=?",
                    (now - 7 * 86400,),
                ).fetchall():
                    if qr["bvid"]:
                        protected_scopes.add(json.dumps(["bilibili", account_id, "video", str(qr["bvid"])], separators=(",", ":")))

            if "evidence" in tables:
                old_rows = db.execute(
                    "SELECT id, scope FROM evidence WHERE observed<?",
                    (cutoff,),
                ).fetchall()
                to_delete = [r["id"] for r in old_rows if str(r["scope"]) not in protected_scopes]
                for idx in range(0, len(to_delete), 200):
                    batch = to_delete[idx:idx + 200]
                    marks = ",".join("?" for _ in batch)
                    db.execute(f"DELETE FROM evidence WHERE id IN ({marks})", batch)
                stats["deleted_aged_evidence"] = len(to_delete)

                total = db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
                if total > max(100, int(max_evidence)):
                    excess = total - max(100, int(max_evidence))
                    cand_rows = db.execute(
                        "SELECT id, scope FROM evidence ORDER BY observed ASC, rowid ASC LIMIT ?",
                        (excess * 2 + 200,),
                    ).fetchall()
                    overflow_ids: list[str] = []
                    for r in cand_rows:
                        if str(r["scope"]) in protected_scopes:
                            continue
                        overflow_ids.append(r["id"])
                        if len(overflow_ids) >= excess:
                            break
                    for idx in range(0, len(overflow_ids), 200):
                        batch = overflow_ids[idx:idx + 200]
                        marks = ",".join("?" for _ in batch)
                        db.execute(f"DELETE FROM evidence WHERE id IN ({marks})", batch)
                    stats["deleted_overflow_evidence"] = len(overflow_ids)

                if "notes" in tables:
                    live_ev_ids = {r[0] for r in db.execute("SELECT id FROM evidence").fetchall()}
                    note_rows = db.execute("SELECT id, scope, evidence_ids FROM notes").fetchall()
                    dead_notes: list[str] = []
                    for nr in note_rows:
                        if str(nr["scope"]) in protected_scopes:
                            continue
                        try:
                            ev_ids = json.loads(nr["evidence_ids"] or "[]")
                        except (ValueError, TypeError):
                            ev_ids = []
                        if not ev_ids or not any(eid in live_ev_ids for eid in ev_ids):
                            dead_notes.append(nr["id"])
                    for idx in range(0, len(dead_notes), 200):
                        batch = dead_notes[idx:idx + 200]
                        marks = ",".join("?" for _ in batch)
                        db.execute(f"DELETE FROM notes WHERE id IN ({marks})", batch)
                    stats["deleted_notes"] = len(dead_notes)

                if "memory_jobs" in tables:
                    db.execute("DELETE FROM memory_jobs WHERE scope NOT IN (SELECT DISTINCT scope FROM evidence)")

    if bridge_db_path and Path(bridge_db_path).is_file():
        bdb_wrap = Database(bridge_db_path)
        with bdb_wrap.connect(write=True) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "sync_outbox" in tables:
                cur = db.execute(
                    "DELETE FROM sync_outbox WHERE state IN ('acked', 'cancelled', 'expired') AND expires<=?",
                    (now,),
                )
                stats["deleted_outbox"] = int(cur.rowcount or 0)
            if "sync_inbox" in tables:
                db.execute("DELETE FROM sync_inbox WHERE expires<=?", (now,))
            if "sync_exported" in tables and "bridge_manifest" in tables:
                db.execute(
                    "DELETE FROM sync_exported WHERE kind='retraction' "
                    "AND source_id NOT IN (SELECT source_id FROM bridge_manifest WHERE active=1)"
                )

    return stats
