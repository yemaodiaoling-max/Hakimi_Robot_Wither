"""Small monetary sub-ledger: actual settlements, outstanding liabilities retained."""
from contextlib import contextmanager
from datetime import datetime,timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo

from hakimi.core.money import NANO, nano


class BudgetUnavailable(RuntimeError):pass


class SearchBudget:
    def __init__(self,base,settings,clock=time.time):
        self.path=Path(base)/'state/search_budget.sqlite3';self.path.parent.mkdir(parents=True,exist_ok=True)
        self.settings=settings;self.clock=clock
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY,kind TEXT,day TEXT,month TEXT,reserved INTEGER,
                actual INTEGER,status TEXT,created REAL,settled REAL,review INTEGER DEFAULT 0)''')
            columns={row[1] for row in db.execute('PRAGMA table_info(calls)')}
            for name,kind in [('lower','INTEGER'),('upper','INTEGER'),('usage','TEXT')]:
                if name not in columns:db.execute('ALTER TABLE calls ADD COLUMN '+name+' '+kind)

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=2)
        try:
            db.execute('BEGIN IMMEDIATE');yield db;db.commit()
        except BaseException:db.rollback();raise
        finally:db.close()

    def reserve(self,kind,maximum,*,leave_available=0):
        now=self.clock();local=datetime.fromtimestamp(now,ZoneInfo('America/New_York'))
        day=local.strftime('%Y-%m-%d');month=day[:7];amount=nano(maximum)
        daily=nano(self.settings.get('daily_limit_usd','0.10'))
        monthly=nano(self.settings.get('monthly_limit_usd','1'))
        identity=uuid.uuid4().hex
        with self.db() as db:
            for column,value,limit in [('day',day,daily),('month',month,monthly)]:
                used=db.execute('SELECT COALESCE(SUM(COALESCE(actual,upper,reserved)),0) FROM calls WHERE '+column+'=?',(value,)).fetchone()[0]
                if used+amount+nano(leave_available)>limit:raise BudgetUnavailable('search_budget_exhausted')
            db.execute('INSERT INTO calls (id,kind,day,month,reserved,actual,status,created,settled,review) VALUES (?,?,?,?,?,NULL,?, ?,NULL,0)',
                       (identity,kind,day,month,amount,'reserved',now))
        return identity

    def settle(self,identity,actual=None,*,bounds=None,usage=None):
        with self.db() as db:
            row=db.execute('SELECT reserved,actual,status FROM calls WHERE id=?',(identity,)).fetchone()
            if not row or row[2]=='settled':return
            if actual is None:
                if bounds is not None:
                    lower,upper=map(nano,bounds)
                    if lower>upper:raise ValueError('invalid_cost_bounds')
                    db.execute("UPDATE calls SET status='unknown',lower=?,upper=?,usage=? WHERE id=?",
                               (lower,upper,json.dumps(usage or {}),identity))
                else:db.execute("UPDATE calls SET status='unknown',usage=? WHERE id=?",(json.dumps(usage or {}),identity))
                return
            amount=nano(actual)
            db.execute("UPDATE calls SET actual=?,status='settled',settled=?,review=?,usage=? WHERE id=?",
                       (amount,self.clock(),int(amount-row[0]>nano('0.10')),json.dumps(usage or {}),identity))


def deepseek_cost(usage,*,peak=False):
    """Official Flash rates verified 2026-11-12; output includes reasoning."""
    if not isinstance(usage,dict):return None
    prompt=usage.get('prompt_tokens');output=usage.get('completion_tokens')
    if type(prompt) is not int or type(output) is not int or min(prompt,output)<0:return None
    hit=usage.get('prompt_cache_hit_tokens')
    if hit is None:hit=(usage.get('prompt_tokens_details') or {}).get('cached_tokens')
    # Without cache accounting, report a bounded liability, never invented usage.
    if type(hit) is not int or not 0<=hit<=prompt:return None
    multiplier=2 if peak else 1
    return (Decimal(prompt-hit)*Decimal('.15')+Decimal(hit)*Decimal('.003')
            +Decimal(output)*Decimal('.60'))*multiplier/1_000_000


def peak_at(at,settings):
    utc=datetime.fromtimestamp(at,timezone.utc)
    if utc.weekday()>=5 or not (1<=utc.hour<4 or 6<=utc.hour<10):return False
    # Peak windows exclude public holidays. Without a supplier tariff receipt,
    # keep a reconcilable lower/upper liability instead of inventing a calendar.
    return None


def cost_bounds(usage,peak):
    if not isinstance(usage,dict) or type(usage.get('prompt_tokens')) is not int or type(usage.get('completion_tokens')) is not int:return None
    prompt=usage['prompt_tokens'];output=usage['completion_tokens']
    if min(prompt,output)<0:return None
    hit=usage.get('prompt_cache_hit_tokens',(usage.get('prompt_tokens_details') or {}).get('cached_tokens'))
    low=deepseek_cost(usage,peak=False if peak is None else peak)
    high=deepseek_cost(usage,peak=True if peak is None else peak)
    multiplier=2 if peak else 1
    if low is None:low=(Decimal(prompt)*Decimal('.003')+Decimal(output)*Decimal('.60'))*multiplier/1_000_000
    if high is None:high=(Decimal(prompt)*Decimal('.30')+Decimal(output)*Decimal('1.20'))/1_000_000
    return low,high
