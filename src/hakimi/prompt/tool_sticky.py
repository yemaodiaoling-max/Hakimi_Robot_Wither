"""Per-session memory of recently needed tools, for a stable cache prefix.

Gemini serializes tool declarations ahead of the contents, so a declared set
that flips with every message invalidates the whole cached prefix. A tool that
a session needed recently stays declared for WINDOW_SECONDS after its last
use. This file is only a hint: it never grants a tool, callers intersect it
with what the current turn permits, and a missing or damaged file means "no
recent tools".
"""
import json
import os
import threading
import time
from pathlib import Path

WINDOW_SECONDS = 45 * 60
MAX_SESSIONS = 256
MAX_TOOLS = 64
# Re-writing an unchanged set is skipped until its timestamps are this old.
REFRESH_SECONDS = 60
FILE = 'state/tool_sticky.json'
_lock = threading.Lock()


def _load(path, now):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    rows = {}
    for key, tools in value.items():
        if not isinstance(key, str) or not isinstance(tools, dict):
            continue
        kept = {name: at for name, at in tools.items()
                if isinstance(name, str) and type(at) in (int, float)
                and now - WINDOW_SECONDS < at <= now + REFRESH_SECONDS}
        if kept:
            rows[key] = dict(sorted(kept.items(), key=lambda item: item[1])[-MAX_TOOLS:])
    return rows


def _save(path, rows):
    if len(rows) > MAX_SESSIONS:
        newest = sorted(rows.items(), key=lambda item: max(item[1].values()))
        rows = dict(newest[-MAX_SESSIONS:])
    tmp = path.with_name(f'{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                                  separators=(',', ':')), encoding='utf-8')
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def touch(base, session_key, names=(), *, now=None):
    """Record ``names`` as used now and return the session's recent tools."""
    if not isinstance(session_key, str) or not session_key:
        return set()
    now = time.time() if now is None else now
    path = Path(base) / FILE
    names = {name for name in names if isinstance(name, str)}
    with _lock:
        rows = _load(path, now)
        tools = rows.get(session_key, {})
        if any(now - tools.get(name, -WINDOW_SECONDS) >= REFRESH_SECONDS for name in names):
            tools = dict(tools, **{name: now for name in names})
            rows[session_key] = tools
            _save(path, rows)
    return set(tools)
