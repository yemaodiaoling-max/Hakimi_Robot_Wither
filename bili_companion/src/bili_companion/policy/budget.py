"""Integer micro-USD reservations. Uncertain calls retain their full liability."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_CEILING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

AUTONOMOUS_VIDEO_ONLY = 'autonomous_video_only'
BUDGET_LANES = ('autonomous_video', 'qq_duty', 'qq_video', 'review', 'other')


def micros(amount):
    value = Decimal(str(amount))
    if not value.is_finite() or value < 0:
        raise ValueError("invalid USD amount")
    return int((value * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def new_york_date(timestamp):
    utc = datetime.fromtimestamp(timestamp, timezone.utc)
    try:
        return utc.astimezone(ZoneInfo("America/New_York"))
    except ZoneInfoNotFoundError:
        # US statutory DST since 2007; explicit supported range, no silent UTC.
        if utc.year < 2007:
            raise ValueError("install tzdata for dates before 2007")
        def sunday(month, first_day):
            day = datetime(utc.year, month, first_day)
            return first_day + (6 - day.weekday()) % 7
        start = datetime(utc.year, 3, sunday(3, 8), 7, tzinfo=timezone.utc)
        end = datetime(utc.year, 11, sunday(11, 1), 6, tzinfo=timezone.utc)
        offset = -4 if start <= utc < end else -5
        return utc.astimezone(timezone(timedelta(hours=offset)))


class BudgetLedger:
    def __init__(self, database, monthly="10", daily="1", *, budget_scope=None,
                 lane='other'):
        self.database = database
        self.monthly, self.daily = micros(monthly), micros(daily)
        self.reply_daily = 0
        if budget_scope not in (None, AUTONOMOUS_VIDEO_ONLY):
            raise ValueError(f"unknown budget scope: {budget_scope!r}")
        self.budget_scope = budget_scope
        self.lane = self._validate_lane(lane)

        with database.connect(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS budget (
                id TEXT PRIMARY KEY, month TEXT, day TEXT, reserved INTEGER,
                actual INTEGER, state TEXT, created REAL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS budget_lanes (
                request_id TEXT PRIMARY KEY, lane TEXT NOT NULL)""")
            db.execute("CREATE TABLE IF NOT EXISTS budget_freeze (id TEXT PRIMARY KEY, acknowledged INTEGER DEFAULT 0)")

    @staticmethod
    def _validate_lane(lane):
        if lane not in BUDGET_LANES:
            raise ValueError(f"unknown budget lane: {lane!r}")
        return lane

    def is_capped(self, *, lane=None):
        lane = self.lane if lane is None else self._validate_lane(lane)
        return self.budget_scope is None or lane == 'autonomous_video'

    def _available(self, db, month, day):
        # Older unsettled calls remain monthly liabilities until settlement.
        daily, monthly = db.execute("""SELECT
            COALESCE(SUM(CASE WHEN day=? THEN
                CASE WHEN state='settled' THEN actual ELSE reserved END ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN state='settled' THEN actual ELSE reserved END), 0)
            FROM budget AS b LEFT JOIN budget_lanes AS l ON l.request_id=b.id
            WHERE (month=? OR state IS NOT 'settled')
                AND (? IS NULL OR COALESCE(l.lane, 'legacy') NOT IN ('qq_duty', 'qq_video', 'review', 'other'))""",
            (day, month, self.budget_scope)).fetchone()
        return max(0, self.daily - daily), max(0, self.monthly - monthly)

    def available(self, now, *, lane=None):
        """Return available (daily, monthly) micro-USD, or None when uncapped."""
        if not self.is_capped(lane=lane):
            return None
        local = new_york_date(now)
        with self.database.connect() as db:
            return self._available(db, local.strftime("%Y-%m"), local.strftime("%Y-%m-%d"))

    def reserve(self, request_id, upper_usd, now, *, lane=None):
        lane = self.lane if lane is None else self._validate_lane(lane)
        upper = micros(upper_usd)
        if upper <= 0:
            raise ValueError("reservation must be positive")
        local = new_york_date(now)
        month, day = local.strftime("%Y-%m"), local.strftime("%Y-%m-%d")
        with self.database.connect(write=True) as db:
            # Legacy budget_freeze rows are audit history, never a global gate.
            old = db.execute("SELECT * FROM budget WHERE id=?", (request_id,)).fetchone()
            if old:
                if old['reserved'] != upper:
                    raise ValueError("request ID reused with different upper bound")
                return False  # Never permits a second external attempt.
            if self.is_capped(lane=lane):
                daily, monthly = self._available(db, month, day)
                if upper > daily or upper > monthly:
                    return False
            if self.budget_scope is None and request_id.startswith('bili-model-comment-reply-'):
                replies = db.execute("""SELECT COALESCE(SUM(CASE WHEN state='settled'
                    THEN actual ELSE reserved END),0) FROM budget
                    WHERE day=? AND id LIKE 'bili-model-comment-reply-%'""", (day,)).fetchone()[0]
                if replies + upper > self.reply_daily:
                    return False
            db.execute("INSERT INTO budget VALUES (?,?,?,?,NULL,'reserved',?)",
                       (request_id, month, day, upper, now))
            db.execute("INSERT INTO budget_lanes VALUES (?,?)", (request_id, lane))
            return True

    def settle(self, request_id, actual_usd):
        actual = micros(actual_usd)
        with self.database.connect(write=True) as db:
            row = db.execute("SELECT * FROM budget WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise KeyError(request_id)
            if row['state'] == 'settled':
                if actual != row['actual']:
                    raise ValueError("conflicting settlement")
                return
            db.execute("UPDATE budget SET actual=?,state='settled' WHERE id=?", (actual, request_id))
            # Every difference is charged in full. Above USD .10 retain a
            # pending-review marker in the legacy table; it never blocks calls.
            excess = actual - row['reserved']
            if excess > 100_000:
                db.execute("INSERT OR IGNORE INTO budget_freeze(id) VALUES (?)", (request_id,))

    def acknowledge_overrun(self, request_id):
        """Legacy audit acknowledgement; never changes reservation eligibility."""
        with self.database.connect(write=True) as db:
            if not db.execute("UPDATE budget_freeze SET acknowledged=1 WHERE id=?", (request_id,)).rowcount:
                raise KeyError(request_id)

    def ambiguous(self, request_id):
        with self.database.connect(write=True) as db:
            result = db.execute("UPDATE budget SET state='unknown' WHERE id=? AND state!='settled'", (request_id,))
            if not result.rowcount and not db.execute("SELECT 1 FROM budget WHERE id=?", (request_id,)).fetchone():
                raise KeyError(request_id)

    def entries(self):
        with self.database.connect() as db:
            return [dict(row) for row in db.execute("""SELECT b.*, COALESCE(l.lane, 'legacy') AS lane
                FROM budget AS b LEFT JOIN budget_lanes AS l ON l.request_id=b.id ORDER BY b.created""")]
