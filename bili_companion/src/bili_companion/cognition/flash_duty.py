"""Choose the responder once per request and recover on real work."""
import logging
import sqlite3
import threading
import time
import uuid
from functools import lru_cache
from http.client import HTTPException
from pathlib import Path

from .gateway import CognitionUnavailable, HYDROGEN
from .live import BudgetPaused
from ..runtime.storage import Database


FLASH_LOW_MODEL = 'gemini-3.8-flash'
FLASH_MODEL = 'gemini-3.8-flash-high'
HYDROGEN_MODEL = 'gemini-4-hydrogen-high'
HYDROGEN_MODELS = frozenset({HYDROGEN, HYDROGEN_MODEL})
SONNET_MODELS = frozenset({'claude-sonnet-5', 'claude-sonnet-5-5'})
HAIKU_MODELS = frozenset({'claude-haiku-5', 'claude-haiku-5-5'})
LANES = ('video', 'qq', 'review')
FAILURES_BEFORE_HANDOVER = 3
FIRST_RETRY_SECONDS = 60
MAX_RETRY_SECONDS = 900
LEASE_SECONDS = 600
log = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def _database(path):
    db = Database(path)
    with db.connect(write=True) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS flash_duty (
            profile TEXT, lane TEXT, mode TEXT, generation INTEGER, failures INTEGER,
            retry_at REAL, lease TEXT, lease_until REAL, PRIMARY KEY(profile,lane))""")
    return db


def _storage(config):
    return _database(str(Path(config['state_dir']) / 'watcher.sqlite3'))


def _state(db, key, initial_mode):
    with db.connect() as conn:
        row = conn.execute('SELECT * FROM flash_duty WHERE profile=? AND lane=?', key).fetchone()
    if row is None:
        with db.connect(write=True) as conn:
            conn.execute('INSERT OR IGNORE INTO flash_duty VALUES(?,?,?,0,0,0,NULL,0)', (*key, initial_mode))
            row = conn.execute('SELECT * FROM flash_duty WHERE profile=? AND lane=?', key).fetchone()
    return row


def preferred_text_duty(config, lane):
    from .profile import load_duty_profile

    key = {'qq': 'qq_hydrogen_profile', 'review': 'qq_review_sonnet_profile',
           'review_haiku': 'qq_review_haiku_profile'}[lane]
    selected = config.get(key)
    if not selected:
        return None
    try:
        profile = load_duty_profile(config, qq_text=lane == 'qq', review=lane == 'review',
                                    review_haiku=lane == 'review_haiku', audit_costs=False)
    except CognitionUnavailable:
        return None
    model = profile.observer_model
    # The earlier shared Hydrogen duty used the high alias for both lanes.
    # Each configured model now starts on duty independently of those failures.
    return f'{selected}#{lane}:{model}', model


def duty_modes(config, *, profile=None, initial_mode='flash'):
    profile = profile or config.get('flash_duty_profile')
    if not profile:
        return {}
    with _storage(config).connect() as conn:
        rows = dict(conn.execute('SELECT lane,mode FROM flash_duty WHERE profile=?', (profile,)))
    # Existing databases use 'flash' as the state meaning the primary is on duty.
    return {lane: rows.get(lane, initial_mode) for lane in LANES}


def active_text_models(config):
    """Resolve both text duties for the panel and the QQ duty status."""
    def active_preference(preference, lane):
        route = preferred_text_duty(config, preference)
        if route and duty_modes(config, profile=route[0])[lane] == 'flash':
            return route[1]
        return None

    if config.get('qq_flash_low_profile'):
        return {'qq': active_preference('qq', 'qq') or FLASH_LOW_MODEL, 'review': FLASH_LOW_MODEL}
    flash_modes = duty_modes(config)
    active = {}
    for lane in ('qq', 'review'):
        model = active_preference(lane, lane)
        if not model:
            if lane == 'review' and config.get('qq_review_sonnet_profile'):
                model = active_preference('review_haiku', lane)
            elif flash_modes.get(lane) == 'flash':
                model = FLASH_MODEL
        if model:
            active[lane] = model
    return active


def run_text_duty(config, lane, generate, standby, *, allow_hydrogen=True):
    """Dispatch the configured QQ responder or post-turn reviewer."""
    def with_profile(route, fallback):
        if route is None:
            return fallback(None)
        profile, model = route
        return run_flash(config, lane, lambda: generate(model, False), fallback,
                         recovery=lambda: generate(model, True), duty_profile=profile)

    if config.get('qq_flash_low_profile') and lane in ('qq', 'review'):
        def flash_low(_):
            return generate(FLASH_LOW_MODEL, False)
        if lane == 'qq' and allow_hydrogen:
            return with_profile(preferred_text_duty(config, 'qq'), flash_low)
        return flash_low(None)

    def with_flash(_):
        return run_flash(config, lane, lambda: generate(FLASH_MODEL, False), standby,
                         recovery=lambda: generate(FLASH_MODEL, True))

    def with_haiku(_):
        return with_profile(preferred_text_duty(config, 'review_haiku'), standby)

    if lane == 'qq' and not allow_hydrogen:
        return with_flash(None)
    fallback = with_haiku if lane == 'review' and config.get('qq_review_sonnet_profile') else with_flash
    return with_profile(preferred_text_duty(config, lane), fallback)


def _provider_error(exc):
    if isinstance(exc, CognitionUnavailable):
        return exc.code not in {'turn_ownership_lost', 'vertex_turn_ownership_lost',
                                'stale_child_epoch', 'model_budget_exhausted', 'request_cost_cap'}
    return isinstance(exc, (OSError, HTTPException))


def _failed(db, key, generation, error):
    code = getattr(error, 'cause_code', getattr(error, 'code', None))
    threshold = 1 if code == 'upstream_pool_unavailable' else FAILURES_BEFORE_HANDOVER
    with db.connect(write=True) as conn:
        updated = conn.execute("""UPDATE flash_duty SET failures=failures+1
            WHERE profile=? AND lane=? AND generation=? AND mode='flash'""", (*key, generation))
        if updated.rowcount:
            switched = conn.execute("""UPDATE flash_duty SET mode='hybrid', generation=generation+1,
                failures=0, retry_at=?, lease=NULL, lease_until=0
                WHERE profile=? AND lane=? AND failures>=?""",
                (time.time()+FIRST_RETRY_SECONDS, *key, threshold))
            if switched.rowcount:
                log.info('Companion duty switched to standby for %s', key[1])


def _recover(db, key, generation, lease, callback):
    try:
        callback()
    except Exception as exc:
        with db.connect(write=True) as conn:
            row = conn.execute("""SELECT failures FROM flash_duty
                WHERE profile=? AND lane=? AND generation=? AND lease=?""", (*key, generation, lease)).fetchone()
            if row is None:
                return
            failures = min(row['failures'] + int(_provider_error(exc)), 8)
            delay = min(FIRST_RETRY_SECONDS * 2 ** failures, MAX_RETRY_SECONDS)
            if getattr(exc, 'code', None) == 'provider_model_unrouted':
                delay = min(900 * 2 ** max(0, failures - 1), 3600)
            conn.execute("""UPDATE flash_duty SET failures=?, retry_at=?, lease=NULL, lease_until=0
                WHERE profile=? AND lane=? AND generation=? AND lease=?""",
                (failures, time.time()+delay, *key, generation, lease))
        if not _provider_error(exc) and not isinstance(exc, BudgetPaused):
            log.debug('Flash recovery deferred for %s (%s)', key[1], type(exc).__name__)
        return
    with db.connect(write=True) as conn:
        changed = conn.execute("""UPDATE flash_duty SET mode='flash', generation=generation+1,
            failures=0, retry_at=0, lease=NULL, lease_until=0
            WHERE profile=? AND lane=? AND generation=? AND mode='hybrid' AND lease=?""",
            (*key, generation, lease))
        if changed.rowcount:
            log.info('Companion primary recovered for %s', key[1])


def finish_recovery(config, lane, generation, lease, callback):
    """A short-lived QQ video worker can hand a claimed request to another process."""
    db = _storage(config)
    key = (config['flash_duty_profile'], lane)
    with db.connect() as conn:
        live = conn.execute("""SELECT 1 FROM flash_duty WHERE profile=? AND lane=?
            AND generation=? AND mode='hybrid' AND lease=? AND lease_until>?""",
            (*key, generation, lease, time.time())).fetchone()
    if live:
        _recover(db, key, generation, lease, callback)


def _start_recovery(db, key, state, callback, launch):
    now = time.time()
    if (callback is None and launch is None) or state['retry_at'] > now or state['lease_until'] > now:
        return
    lease = uuid.uuid4().hex
    generation = state['generation']
    try:
        with db.connect(write=True) as conn:
            claimed = conn.execute("""UPDATE flash_duty SET lease=?, lease_until=?
                WHERE profile=? AND lane=? AND generation=? AND mode='hybrid'
                AND retry_at<=? AND lease_until<=?""",
                (lease, now+LEASE_SECONDS, *key, generation, now, now))
            if not claimed.rowcount:
                return
        if launch is None:
            threading.Thread(target=_recover, args=(db, key, generation, lease, callback),
                             name='flash-recovery-'+key[1], daemon=True).start()
        else:
            launch(generation, lease)
    except (OSError, RuntimeError, sqlite3.Error, ValueError, TypeError) as exc:
        # A failed background launch must never lose a usable foreground answer.
        log.warning('Could not start Flash recovery for %s (%s)', key[1], type(exc).__name__)
        try:
            with db.connect(write=True) as conn:
                conn.execute("""UPDATE flash_duty SET lease=NULL, lease_until=0, retry_at=?
                    WHERE profile=? AND lane=? AND generation=? AND lease=?""",
                    (time.time()+FIRST_RETRY_SECONDS, *key, generation, lease))
        except sqlite3.Error:
            pass  # The lease also expires after a worker or database restart.


def run_flash(config, lane, flash, hybrid, *, recovery=None, launch_recovery=None,
              duty_profile=None, start_in_standby=False):
    """Callbacks generate and validate output. Only the foreground result goes to the caller."""
    profile = duty_profile or config.get('flash_duty_profile')
    if not profile:
        return hybrid(None)
    if lane not in LANES:
        raise ValueError('unknown Flash duty')
    db = _storage(config)
    key = (profile, lane)
    state = _state(db, key, 'hybrid' if start_in_standby else 'flash')
    if state['mode'] == 'hybrid':
        # Give the user's task first claim on the shared budget. A worker running
        # a long recovery never holds a transaction or delays the hybrid model.
        try:
            result = hybrid(None)
        except BudgetPaused:
            raise
        except Exception as exc:
            if _provider_error(exc):
                _start_recovery(db, key, state, recovery, launch_recovery)
            raise
        _start_recovery(db, key, state, recovery, launch_recovery)
        return result
    try:
        result = flash()
    except Exception as exc:
        if not _provider_error(exc):
            raise
        _failed(db, key, state['generation'], exc)
        return hybrid(exc)
    with db.connect(write=True) as conn:
        conn.execute("""UPDATE flash_duty SET failures=0 WHERE profile=? AND lane=?
            AND generation=? AND mode='flash' AND failures>0""", (*key, state['generation']))
    return result
