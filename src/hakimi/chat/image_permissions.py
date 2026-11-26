"""Daemon-owned media registry. Chat messages never supply the permission list."""
from hakimi import paths
from contextlib import closing
import json
import re
import sqlite3
import time
from pathlib import Path

BASE_DIR = paths.base()
VIDEO_FRAME_TTL = 1800


def _read(base):
    path = base / "state" / "trusted_image_registry.json"
    if path.is_symlink() or path.resolve() != path:
        return {}
    try:
        with path.open("rb") as stream:
            raw = stream.read(524289)
        result = json.loads(raw) if len(raw) <= 524288 else {}
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def register_cached_image(session, path, base=BASE_DIR, now=None):
    """Called ONLY by the daemon immediately after successful media caching.

    The caller gives the cache function's return value, never text scraped from
    a message or a model-supplied path. This function is not a bot CLI tool.
    """
    if not isinstance(session, str) or not re.fullmatch(r"(?:group|private):[1-9][0-9]*", session):
        return
    now = time.time() if now is None else now
    root = base / "state" / "img_cache"
    candidate = Path(path).resolve()
    if root.resolve() != root or candidate.parent != root or not candidate.is_file():
        return
    if not candidate.name.startswith("img_" + session.replace(":", "_") + "_"):
        return
    registry = _read(base)
    # A bounded registry, separate from all conversation payloads.
    registry = {key: [r for r in rows if isinstance(r, dict) and
                     isinstance(r.get("at"), (int, float)) and 0 <= now - r["at"] < 1800]
                for key, rows in registry.items() if isinstance(rows, list)}
    registry = {k: v for k, v in registry.items() if v}
    entries = [r for r in registry.get(session, []) if r.get("path") != str(candidate)]
    entries.append({"path": str(candidate), "at": now})
    registry[session] = entries[-60:]
    registry = dict(list(registry.items())[-64:])
    target = base / "state" / "trusted_image_registry.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def allowed_images_for_session(session, base=BASE_DIR, now=None):
    """Read only the trusted daemon registry, never recent_context_messages."""
    now = time.time() if now is None else now
    result = []
    root = base / "state" / "img_cache"
    if root.resolve() != root or root.is_symlink():
        return []
    for row in _read(base).get(session, [])[-60:]:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            continue
        at = row.get("at", 0)
        if not isinstance(at, (int, float)) or not 0 <= now - at < 1800:
            continue
        candidate = Path(row["path"])
        if candidate.is_symlink():
            continue
        path = candidate.resolve()
        if (path.parent == root and path.is_file() and
                path.name.startswith("img_" + session.replace(":", "_") + "_")):
            result.append(str(path))
    result.extend(_video_frames(session, base, now))
    return list(dict.fromkeys(result))


def register_video_frame(session, owner, path, base):
    """Register an image decoded by the video worker for the current turn."""
    path = Path(path)
    if not _video_frame_valid(session, path, base) or not isinstance(owner, str) or not owner:
        return
    now = time.time()
    with closing(sqlite3.connect(base / 'state' / 'video_frames.sqlite3')) as db, db:
        db.execute('''CREATE TABLE IF NOT EXISTS frames (
            session TEXT, owner TEXT, path TEXT, expires REAL, PRIMARY KEY(session, owner))''')
        db.execute('DELETE FROM frames WHERE expires <= ?', (now,))
        db.execute('INSERT OR REPLACE INTO frames VALUES (?,?,?,?)',
                   (session, owner, str(path), now + VIDEO_FRAME_TTL))


def _video_frame_valid(session, path, base):
    root = base / 'state' / 'img_cache'
    return bool(isinstance(session, str) and re.fullmatch(r'(?:group|private):[1-9][0-9]*', session)
                and root.resolve() == root and path.parent == root and not path.is_symlink() and path.is_file()
                and re.fullmatch('img_' + session.replace(':', '_') + r'_video_[a-f0-9]{64}\.jpg', path.name))


def _video_frames(session, base, now, owner=None):
    database = base / 'state' / 'video_frames.sqlite3'
    if database.is_symlink() or not database.is_file():
        return []
    try:
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            query = 'SELECT path FROM frames WHERE session=? AND expires>?'
            parameters = (session, now)
            if owner is not None:
                query += ' AND owner=?'
                parameters += (owner,)
            rows = db.execute(query + ' ORDER BY expires LIMIT 60', parameters).fetchall()
        return [row[0] for row in rows if _video_frame_valid(session, Path(row[0]), base)]
    except (OSError, sqlite3.Error):
        return []


def video_frame_for_turn(session, owner, base):
    registered = _video_frames(session, Path(base).resolve(), time.time(), owner) if owner else []
    return Path(registered[0]) if registered else None
