"""Shared primary availability, with single-use proof for genuine late replies."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
import threading
import time

_CACHE = {}
_LOCK = threading.RLock()
GATE_OWNER = "vertex_route"
CONTROL_DEFAULTS = {"mode":"auto","primary_paused":False,"user_paused":False}


def _default_state(signal="initial"):
    return {"fast":False,"streak":0,"epoch":0,"signal":signal,**CONTROL_DEFAULTS,
            "vertex_gate_active":False,"vertex_gate_reason":None,
            "primary_pause_sources":[]}


class ProviderHealth:
    def __init__(self, base, clock=time.time):
        base = Path(base).resolve()
        self.root = base.parent if base.name == "official_test_runtime" else base
        self.path = self.root / "state/provider_health.sqlite3"
        self.clock = clock

    @contextmanager
    def db(self):
        self.path.parent.mkdir(exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS health(id INTEGER PRIMARY KEY CHECK(id=1),streak INTEGER,fast INTEGER,epoch INTEGER,changed REAL,signal TEXT);
                INSERT OR IGNORE INTO health VALUES(1,0,0,0,0,'initial');
                CREATE TABLE IF NOT EXISTS takeovers(trigger TEXT PRIMARY KEY,at REAL);
                CREATE TABLE IF NOT EXISTS late_proofs(hash TEXT PRIMARY KEY,scope TEXT,expires REAL,consumed INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS control(id INTEGER PRIMARY KEY CHECK(id=1),mode TEXT,primary_paused INTEGER);
                INSERT OR IGNORE INTO control(id,mode,primary_paused) VALUES(1,'auto',0);
                CREATE TABLE IF NOT EXISTS primary_gate(owner TEXT PRIMARY KEY,active INTEGER NOT NULL DEFAULT 0,reason TEXT,changed REAL,updated REAL,previous_streak INTEGER);
            """)
            with db:
                yield db
        finally:
            db.close()
            with _LOCK:
                _CACHE.pop(str(self.path), None)

    def snapshot(self):
        if not self.path.exists():
            return _default_state()
        with _LOCK:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns,stat.st_size)
            cached = _CACHE.get(str(self.path))
            if cached and cached[0] == stamp:
                return dict(cached[1])
            db=sqlite3.connect(self.path,timeout=5)
            try:
                db.row_factory = sqlite3.Row
                row = dict(db.execute("SELECT streak,fast,epoch,changed,signal FROM health WHERE id=1").fetchone())
                # Older installations gain the control row on their first write.
                control = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='control'").fetchone()
                user_paused = False
                if control:
                    value = db.execute("SELECT mode,primary_paused FROM control WHERE id=1").fetchone()
                    if value is not None:
                        row["mode"] = value["mode"]
                        user_paused = bool(value["primary_paused"])
                row.setdefault("mode", "auto")
                gate_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='primary_gate'").fetchone()
                gate = (db.execute("SELECT active,reason FROM primary_gate WHERE owner=?",
                                   (GATE_OWNER,)).fetchone() if gate_table else None)
            finally:
                db.close()
            gate_active = bool(gate["active"]) if gate is not None else False
            gate_reason = gate["reason"] if gate is not None and gate_active else None
            row["fast"] = bool(row["fast"])
            row["user_paused"] = bool(user_paused)
            row["vertex_gate_active"] = gate_active
            row["vertex_gate_reason"] = gate_reason
            # The effective pause is the union of independent, owned sources.
            row["primary_paused"] = bool(user_paused or gate_active)
            row["primary_pause_sources"] = (
                (["user"] if user_paused else [])
                + ([gate_reason or GATE_OWNER] if gate_active else []))
            if len(_CACHE)>32:_CACHE.clear()
            _CACHE[str(self.path)] = (stamp,row)
            return dict(row)

    def takeover(self, trigger, primary_token=None, session=None):
        now = self.clock()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM takeovers WHERE at<?",(now-86400,))
            db.execute("DELETE FROM late_proofs WHERE expires<?",(now,))
            if not db.execute("INSERT OR IGNORE INTO takeovers VALUES(?,?)",(trigger,now)).rowcount:
                return False
            if primary_token:
                digest = hashlib.sha256(primary_token.encode()).hexdigest()
                db.execute("INSERT OR IGNORE INTO late_proofs(hash,scope,expires) VALUES(?,?,?)",(digest,session,now+3600))
            db.execute("UPDATE health SET streak=streak+1,fast=(streak+1>=3 OR (SELECT primary_paused FROM control WHERE id=1) OR EXISTS(SELECT 1 FROM primary_gate WHERE owner=? AND active=1)),epoch=epoch+1,changed=?,signal='fallback_takeover' WHERE id=1",(GATE_OWNER,now))
        return True

    def primary_completed(self):
        if not self.path.exists():return
        now = self.clock()
        with self.db() as db:
            db.execute("UPDATE control SET primary_paused=0 WHERE id=1")
            db.execute("UPDATE health SET streak=0,fast=CASE WHEN EXISTS(SELECT 1 FROM primary_gate WHERE owner=? AND active=1) THEN 1 ELSE 0 END,epoch=epoch+1,changed=?,signal='primary_completed' WHERE id=1 AND (streak>0 OR fast=1)",(GATE_OWNER,now))

    def late_reply(self, token, session=None):
        if not isinstance(token,str) or len(token)!=32 or not self.path.exists():return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = self.clock()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM late_proofs WHERE hash=? AND consumed=0 AND expires>=?",(digest,now)).fetchone()
            if not row or (session is not None and row["scope"]!=session):return False
            db.execute("UPDATE late_proofs SET consumed=1 WHERE hash=?",(digest,))
            db.execute("UPDATE control SET primary_paused=0 WHERE id=1")
            db.execute("UPDATE health SET streak=0,fast=CASE WHEN EXISTS(SELECT 1 FROM primary_gate WHERE owner=? AND active=1) THEN 1 ELSE 0 END,epoch=epoch+1,changed=?,signal='verified_late_primary_reply' WHERE id=1",(GATE_OWNER,now))
        return True

    def set_mode(self, mode):
        if mode not in {"auto","dsh","gemini"}:raise ValueError("mode must be auto, dsh or gemini")
        with self.db() as db:
            db.execute("UPDATE control SET mode=? WHERE id=1",(mode,))
            db.execute("UPDATE health SET epoch=epoch+1,changed=?,signal=? WHERE id=1",(self.clock(),"operator_mode_"+mode))

    def pause_primary(self):
        """An operator observation, never a guessed quota/billing failure."""
        with self.db() as db:
            db.execute("UPDATE control SET primary_paused=1 WHERE id=1")
            db.execute("UPDATE health SET fast=1,epoch=epoch+1,changed=?,signal='operator_primary_paused' WHERE id=1",(self.clock(),))

    def resume_primary(self, fast=None, streak=None):
        # Callers may preserve state that predated their own pause.
        fast = int(bool(fast)) if fast is not None else 0
        streak = int(streak) if streak is not None else 0
        now = self.clock()
        with self.db() as db:
            db.execute("UPDATE control SET primary_paused=0 WHERE id=1")
            db.execute("UPDATE health SET streak=?,fast=CASE WHEN EXISTS(SELECT 1 FROM primary_gate WHERE owner=? AND active=1) THEN 1 ELSE ? END,epoch=epoch+1,changed=?,signal='operator_primary_resumed' WHERE id=1",(streak,GATE_OWNER,fast,now))

    def set_primary_gate(self, active, reason=None, *, owner=GATE_OWNER, now=None):
        """Set one owned pause source without touching the user pause or mode.

        Returns True only when the durable gate state changed, so callers can
        avoid writing audit state on every budget check.
        """
        if type(active) is not bool:
            raise ValueError("active must be a bool")
        if not isinstance(owner,str) or not owner or len(owner)>80:
            raise ValueError("owner must be a short non-empty string")
        if reason is not None and (not isinstance(reason,str) or not reason or len(reason)>160):
            raise ValueError("reason must be a short non-empty string")
        now = self.clock() if now is None else float(now)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM primary_gate WHERE owner=?",(owner,)).fetchone()
            if row is None:
                if not active:
                    return False
                health = db.execute("SELECT streak FROM health WHERE id=1").fetchone()
                previous_streak = int(health["streak"] or 0) if health else 0
                db.execute("INSERT INTO primary_gate(owner,active,reason,changed,updated,previous_streak) VALUES(?,?,?,?,?,?)",
                           (owner,1,reason,now,now,previous_streak))
                db.execute("UPDATE health SET fast=1,epoch=epoch+1,changed=?,signal=? WHERE id=1",
                           (now,"vertex_gate_"+(reason or "active")))
                return True
            was_active = bool(row["active"])
            if not active:
                if not was_active:
                    return False
                previous_streak = row["previous_streak"]
                if previous_streak is None:
                    health = db.execute("SELECT streak FROM health WHERE id=1").fetchone()
                    previous_streak = int(health["streak"] or 0) if health else 0
                else:
                    previous_streak = int(previous_streak)
                db.execute("UPDATE primary_gate SET active=0,changed=?,updated=? WHERE owner=?",
                           (now,now,owner))
                db.execute("""UPDATE health SET streak=?,fast=CASE
                                  WHEN (SELECT primary_paused FROM control WHERE id=1)=1 THEN 1
                                  WHEN ?>=3 THEN 1 ELSE 0 END,
                              epoch=epoch+1,changed=?,signal='vertex_gate_released' WHERE id=1""",
                           (previous_streak,previous_streak,now))
                return True
            if not was_active:
                health = db.execute("SELECT streak FROM health WHERE id=1").fetchone()
                previous_streak = int(health["streak"] or 0) if health else 0
                db.execute("""UPDATE primary_gate SET active=1,reason=?,changed=?,updated=?,
                                  previous_streak=? WHERE owner=?""",
                           (reason,now,now,previous_streak,owner))
                db.execute("UPDATE health SET fast=1,epoch=epoch+1,changed=?,signal=? WHERE id=1",
                           (now,"vertex_gate_"+(reason or "active")))
                return True
            if row["reason"] == reason:
                return False
            db.execute("UPDATE primary_gate SET reason=?,changed=?,updated=? WHERE owner=?",
                       (reason,now,now,owner))
            db.execute("UPDATE health SET changed=?,signal=? WHERE id=1",
                       (now,"vertex_gate_"+(reason or "active")))
            return True

def availability(base, clock=time.time):
    try:return ProviderHealth(base,clock).snapshot()
    except (OSError,sqlite3.Error,TypeError):return _default_state("unavailable")
