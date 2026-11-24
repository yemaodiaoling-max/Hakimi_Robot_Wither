from collections import Counter
from contextlib import closing
import logging
from pathlib import Path
import sqlite3

from bili_companion.policy.budget import AUTONOMOUS_VIDEO_ONLY, BudgetLedger, micros
from bili_companion.runtime.storage import Database
from .profile import ActivatedProfile


def _qq_video_call_ids(path):
    ids = set()
    try:
        if not path.is_file():
            return ids
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.25)) as db:
            for table, key in (('qq_model_calls', 'id'), ('qq_request_calls', 'model_request_id')):
                columns = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
                if key in columns:
                    ids.update(row[0] for row in db.execute(f'SELECT {key} FROM {table}'))
    except (OSError, sqlite3.Error):
        logging.getLogger(__name__).warning('QQ video call audit unavailable for budget history')
    return ids


def classify_budget_history(ledger, state_dir):
    """Label verified historical sources without changing the accounting rows."""
    counts = Counter()
    if ledger.budget_scope != AUTONOMOUS_VIDEO_ONLY:
        return counts
    qq_ids = _qq_video_call_ids(Path(state_dir) / 'qq_requests.sqlite3')
    with ledger.database.connect(write=True) as db:
        watcher_columns = {row[1] for row in db.execute('PRAGMA table_info(watcher_calls)')}
        if {'request_id', 'kind'} <= watcher_columns:
            unclassified = db.execute("""SELECT b.id, w.kind FROM budget AS b
                LEFT JOIN budget_lanes AS l ON l.request_id=b.id
                LEFT JOIN watcher_calls AS w ON w.request_id=b.id WHERE l.request_id IS NULL""")
        else:
            unclassified = db.execute("""SELECT b.id, NULL FROM budget AS b
                LEFT JOIN budget_lanes AS l ON l.request_id=b.id WHERE l.request_id IS NULL""")
        for request_id, kind in unclassified.fetchall():
            if not isinstance(request_id, str):
                continue
            if request_id in qq_ids:
                lane = 'qq_video'
            elif kind == 'video' or request_id.startswith('bili-model-video-selection-'):
                lane = 'autonomous_video'
            elif kind == 'live' or request_id.startswith((
                    'companion-text-', 'bili-model-live-observe-', 'bili-model-live-decision-',
                    'bili-model-live-child-expression-', 'bili-model-live-child-memory-',
                    'bili-model-comment-reply-')):
                lane = 'other'
            else:
                continue
            db.execute('INSERT INTO budget_lanes (request_id, lane) VALUES (?, ?)', (request_id, lane))
            counts[lane] += 1
    return counts


def video_budget(config, *, lane='other'):
    monthly, daily = ActivatedProfile.budgets(config, monthly_cap=96)
    ledger = BudgetLedger(Database(Path(config['state_dir'])/'watcher.sqlite3'), monthly=monthly, daily=daily,
                          budget_scope=config.get('budget_scope'), lane=lane)
    ledger.reply_daily = micros(config.get('reply_daily_reserve_usd', 0))
    if lane == 'autonomous_video':
        classify_budget_history(ledger, config['state_dir'])
    return ledger
