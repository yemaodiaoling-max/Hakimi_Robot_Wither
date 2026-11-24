"""Bounded reply lanes sharing transport, history and cost limits."""
from contextvars import ContextVar
from contextlib import contextmanager
import os
from pathlib import Path

LANES = ('', 'at1', 'at2', 'at3')
ACTIVE = frozenset({'queued', 'primary_active', 'fallback_active'})
_current = ContextVar('hakimi_turn_lane', default=None)


def lane_id():
    value = _current.get()
    value = os.environ.get('HAKIMI_TURN_LANE', '') if value is None else value
    if value not in LANES:
        raise ValueError('unknown reply lane')
    return value


@contextmanager
def lane_context(value):
    if value not in LANES:
        raise ValueError('unknown reply lane')
    token = _current.set(value)
    try:
        yield
    finally:
        _current.reset(token)


def active_context_path(base):
    lane = lane_id()
    return Path(base)/'state'/('active_context_'+lane+'.json' if lane else 'active_context.json')


def explicit_at(payload):
    return (str(payload.get('session_key', '')).startswith('group:')
            and str(payload.get('wake_mode', '')).startswith('即时唤醒'))
