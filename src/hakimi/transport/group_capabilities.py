"""Locally cached QQ group capabilities; no language-model calls."""
import json
import threading
import time
from pathlib import Path
from hakimi.core.runtime_io import atomic_replace

_LOCK = threading.RLock()


def _path(base):
    return Path(base) / 'state' / 'group_capabilities.json'


def snapshot(base):
    try:
        return json.loads(_path(base).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def update(base, session, *, allow_proactive=None, receive_all=None, checked_at=None, source='bot_state'):
    with _LOCK:
        values = snapshot(base)
        row = dict(values.get(session, {}))
        if allow_proactive is not None:
            row['allow_proactive'] = bool(allow_proactive)
        if receive_all is not None:
            row['receive_all'] = bool(receive_all)
        if checked_at is not None:
            row['checked_at'] = checked_at
        row['source'] = source
        values[session] = row
        path = _path(base)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(values, ensure_ascii=False), encoding='utf-8')
        atomic_replace(temp, path)
        return row


def can_autonomous(base, session, now=None):
    if not str(session).startswith('group:'):
        return True
    row = snapshot(base).get(session)
    if row is None:
        return not (Path(base) / 'official_lane_config.json').exists()
    now = time.time() if now is None else now
    return (row.get('allow_proactive') is True and row.get('receive_all') is not False
            and now - row.get('checked_at', 0) <= 180)
