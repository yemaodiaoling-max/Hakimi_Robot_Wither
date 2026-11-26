"""Rolling per-user reply budget for group conversations, shared across providers."""
import math
import re

from hakimi.core.context_window import select_context

WINDOW_SECONDS = 1800
MAX_REPLIES = 7
HISTORY_KEY = 'user_reply_history'
LIMIT_REASON = 'user_reply_limit'
_PRIVATE_SESSION = re.compile(r'private:[1-9][0-9]{0,19}\Z')


def is_private_session(session):
    return isinstance(session, str) and _PRIVATE_SESSION.fullmatch(session) is not None


def recipient(payload, reply_to=None):
    if payload.get('internal_review'):
        return None
    session = str(payload.get('session_key', ''))
    rows, target = select_context(payload)
    requested = str(reply_to if reply_to is not None else payload.get('reply_target_message_id', ''))
    selected = next((row for row in rows if requested
                     and str(row.get('message_id', '')) == requested), None)
    if reply_to is not None and selected is None:
        return recipient(payload)
    if reply_to is None and (payload.get('is_proactive') or payload.get('wake_mode') == '冷场主动抛话题'):
        return None
    user = str((selected or target).get('user_id') or '')
    if not user and session.startswith('private:'):
        user = session.split(':', 1)[1]
    return user if user and user != 'SELF' else None


def prune(history, now):
    newest = {}
    for row in history if isinstance(history, list) else ():
        if not isinstance(row, dict):
            continue
        user, trigger, at = row.get('user_id'), row.get('trigger_id'), row.get('completed_at')
        if (not isinstance(user, str) or not user or user == 'SELF'
                or not isinstance(trigger, str) or not trigger
                or type(at) not in (int, float) or not math.isfinite(at)
                or not 0 <= now-at < WINDOW_SECONDS):
            continue
        key = (user, trigger)
        newest[key] = max(at, newest.get(key, at))
    return sorted(({'user_id': user, 'trigger_id': trigger, 'completed_at': at, 'scope': 'group'}
                   for (user, trigger), at in newest.items()), key=lambda row: row['completed_at'])


def record(history, user, trigger, now):
    rows = prune(history, now)
    if user and trigger and not any(row['user_id'] == user and row['trigger_id'] == trigger for row in rows):
        rows.append({'user_id': user, 'trigger_id': trigger, 'completed_at': now})
    return prune(rows, now)


def retry_after(history, user, now):
    rows = [row for row in prune(history, now) if row['user_id'] == user] if user else []
    if len(rows) < MAX_REPLIES:
        return 0
    return max(0, rows[-MAX_REPLIES]['completed_at'] + WINDOW_SECONDS-now)
