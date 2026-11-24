"""Per-provider drawing charges, in integer billionths of a US dollar."""
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo

from hakimi.models.search_budget import NANO, nano


WINDOW_SECONDS = 20 * 60
WINDOW_IMAGES = 2
DAY_IMAGES = 20
NEW_YORK = ZoneInfo('America/New_York')
IMAGE_QUOTA_ERRORS = {'drawing_window_images', 'drawing_day_images'}


class BudgetError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _amount(value):
    if isinstance(value, bool):
        raise ValueError('绘图金额必须是非负美元数')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not 0 <= number <= Decimal(2**63 - 1) / NANO:
            raise ValueError('绘图金额超出可记录范围')
        return number
    except (InvalidOperation, TypeError):
        raise ValueError('绘图金额必须是非负美元数') from None


def validate_limits(settings):
    result = {}
    for key in ('reserve_usd', 'daily_usd_limit', 'monthly_usd_limit'):
        value = settings.get(key)
        if value is None or value == '':
            result[key] = None
            continue
        amount = _amount(value)
        if amount and amount.normalize().as_tuple().exponent < -9:
            raise ValueError('绘图金额最多支持九位小数')
        result[key] = format(amount.normalize(), 'f') if amount else '0'
    if any(result[k] is not None for k in ('daily_usd_limit', 'monthly_usd_limit')):
        if not result['reserve_usd'] or result['reserve_usd'] == '0':
            raise ValueError('设置绘图限额时，请填写单次预留金额')
    return result


@contextmanager
def _transaction(base):
    state = Path(base) / 'state'
    db = None
    try:
        state.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(state / 'drawing_costs.sqlite3', timeout=5)
        with db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE IF NOT EXISTS drawing_charges (
                id TEXT PRIMARY KEY, day TEXT NOT NULL, month TEXT NOT NULL,
                created_at REAL NOT NULL, reserved INTEGER, actual INTEGER,
                status TEXT NOT NULL, review INTEGER NOT NULL DEFAULT 0)''')
            db.execute('CREATE TABLE IF NOT EXISTS drawing_window '
                       '(id INTEGER PRIMARY KEY CHECK(id=1), started_at REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS drawing_charges_day ON drawing_charges(day)')
            db.execute('CREATE INDEX IF NOT EXISTS drawing_charges_created ON drawing_charges(created_at)')
            yield db
    except (sqlite3.Error, OSError, OverflowError) as error:
        raise BudgetError('drawing_budget_unavailable', '绘图费用记录暂时不可用') from error
    finally:
        if db is not None:
            db.close()


def _quota(db, now):
    local = datetime.fromtimestamp(now, NEW_YORK)
    tomorrow = local.date() + timedelta(days=1)
    midnight = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=NEW_YORK).timestamp()
    day_count, = db.execute("SELECT COUNT(*) FROM drawing_charges WHERE day=? AND status!='not_submitted'",
                            (local.date().isoformat(),)).fetchone()
    window = db.execute('SELECT started_at FROM drawing_window WHERE id=1').fetchone()
    window_reset, window_count = None, 0
    if window and now < window[0] + WINDOW_SECONDS:
        window_reset = window[0] + WINDOW_SECONDS
        window_count, = db.execute("SELECT COUNT(*) FROM drawing_charges "
                                   "WHERE created_at>=? AND status!='not_submitted'", (window[0],)).fetchone()
    wait_until = []
    if window_count >= WINDOW_IMAGES:
        wait_until.append(window_reset)
    if day_count >= DAY_IMAGES:
        wait_until.append(midnight)
    return dict(window_remaining=max(0, WINDOW_IMAGES-window_count), daily_remaining=max(0, DAY_IMAGES-day_count),
                window_reset_at=window_reset, day_reset_at=midnight, available_at=max(wait_until, default=None))


def quota_status(base):
    with _transaction(base) as db:
        return _quota(db, time.time())


def cooling_down(base):
    """Read-only prompt hint. Admission always rechecks the quota under lock."""
    path = Path(base) / 'state' / 'drawing_costs.sqlite3'
    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
            return _quota(db, time.time())['available_at'] is not None
    except sqlite3.Error:
        return False


def reserve(base, settings):
    try:
        limits = validate_limits(settings)
    except ValueError as exc:
        raise BudgetError('drawing_budget_config', str(exc)) from None
    estimate = None if limits['reserve_usd'] is None else nano(limits['reserve_usd'])
    identity = uuid.uuid4().hex
    with _transaction(base) as db:
        now = time.time()
        day = datetime.fromtimestamp(now, NEW_YORK).date().isoformat()
        quota = _quota(db, now)
        if quota['available_at'] is not None:
            code = 'drawing_day_images' if quota['daily_remaining'] == 0 else 'drawing_window_images'
            raise BudgetError(code, '绘图正在冷却。')
        for period, date_key, name, label in (
                ('daily', day, 'day', '今日'), ('monthly', day[:7], 'month', '本月')):
            cap = limits[period + '_usd_limit']
            if cap is None:
                continue
            # Records created before a reservation price was supplied must count too.
            used = db.execute('SELECT COALESCE(SUM(COALESCE(actual, reserved, ?)), 0) '
                              'FROM drawing_charges WHERE ' + name + '=?', (estimate, date_key)).fetchone()[0]
            if used + estimate > nano(cap):
                raise BudgetError('drawing_' + period + '_limit', label + '绘图额度不足')
        if quota['window_reset_at'] is None:
            db.execute('INSERT INTO drawing_window VALUES (1,?) '
                       'ON CONFLICT(id) DO UPDATE SET started_at=excluded.started_at', (now,))
        db.execute('INSERT INTO drawing_charges (id,day,month,created_at,reserved,status) '
                   "VALUES (?,?,?,?,?,'reserved')", (identity, day, day[:7], now, estimate))
    return identity


def _receipt_amount(receipt):
    if not isinstance(receipt, dict):
        return None
    amounts = set()
    for block in (receipt, receipt.get('usageMetadata'), receipt.get('usage'), receipt.get('billing')):
        if not isinstance(block, dict):
            continue
        currency = block.get('currency')
        if currency is not None and str(currency).upper() != 'USD':
            continue
        keys = ['actualCostUsd', 'costUsd', 'totalCostUsd', 'actual_cost_usd', 'cost_usd', 'total_cost_usd']
        if currency and str(currency).upper() == 'USD':
            keys.extend(['cost', 'actual_cost'])
        for key in keys:
            if key in block:
                try:
                    amounts.add(nano(_amount(block[key])))
                except ValueError:
                    return None
    return amounts.pop() if len(amounts) == 1 else None


def finish(base, identity, receipt, *, rejected=False, not_submitted=False):
    actual = _receipt_amount(receipt)
    if actual is None and (rejected or not_submitted):
        actual = 0
    with _transaction(base) as db:
        row = db.execute('SELECT reserved,actual FROM drawing_charges WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise BudgetError('drawing_budget_unavailable', '未找到本次绘图预留')
        if row[1] is not None:
            return
        if actual is None:
            db.execute("UPDATE drawing_charges SET status='pending' WHERE id=?", (identity,))
        else:
            review = row[0] is not None and actual - row[0] > nano('.10')
            status = 'settled'
            if not_submitted:
                status = 'not_submitted'
            elif rejected and not actual:
                status = 'rejected'
            db.execute('UPDATE drawing_charges SET actual=?, status=?, review=? WHERE id=?',
                       (actual, status, int(review), identity))

