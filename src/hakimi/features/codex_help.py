"""Durable, shared, budgeted expert requests. Models never get queue/file access."""
from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3
import time
import traceback
import unicodedata
from pathlib import Path

from hakimi.core.runtime_io import read_json_cached
from hakimi.prompt.prompt_compaction import clean_text

REVISION = "2026-10-24-codex-help-v1"
DEFAULTS = {"enabled": False, "max_calls_per_hour": 6, "max_calls_per_day": 18,
            "max_session_calls_per_hour": 2, "max_fault_calls_per_day": 6,
            "max_complex_calls_per_hour": 4, "max_complex_calls_per_day": 12,
            "max_total_tokens_per_day": 200000, "token_reservation": 12000,
            "max_pending": 32, "timeout_seconds": 70, "wait_seconds": 50,
            "min_call_interval_seconds": 15, "cache_seconds": 900, "fault_merge_seconds": 43200}


def project_root(base):
    base = Path(base).resolve()
    return base.parent if base.name == "official_test_runtime" else base


def settings(base):
    cfg = read_json_cached(project_root(base) / "codex_help_config.json", {})
    result = dict(DEFAULTS)
    if isinstance(cfg, dict):
        result["enabled"] = cfg.get("enabled") is True
        for key in DEFAULTS.keys() - {"enabled"}:
            value = cfg.get(key)
            if type(value) in (int, float) and 0 < value <= DEFAULTS[key] * 10:
                result[key] = value
    return result


def scrub(text, limit=2000):
    text = str(text)[:limit * 2]
    text = re.sub(r"(?i)(?:bearer\s+\S+|\bsk-[a-z0-9_-]{8,}|(?:api[_ -]?key|appsecret|owner_token|access_token|authorization)\s*[=:]\s*\S+)", "[redacted]", text)
    text = re.sub(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s，。；\"']+", "[local path]", text)
    return text[:limit]


def question_brief(payload, question, base):
    from hakimi.prompt.group_profiles import resolve_profile
    session = payload.get("session_key", "")
    profile, _ = resolve_profile(session, Path(base))
    messages, budget = [], 2200
    for row in reversed(payload.get("recent_context_messages", [])[-8:]):
        if not isinstance(row, dict) or row.get("session_key", session) != session or budget <= 0:
            continue
        text = scrub(clean_text(str(row.get("text", ""))), min(700, budget))
        budget -= len(text)
        messages.append({"role": "hakimi" if row.get("user_id") == "SELF" else "peer", "text": text})
    return {"kind": "complex", "session_key": session, "question": scrub(question, 1000),
            "messages": list(reversed(messages)), "profile": profile["name"]}


class HelpQueue:
    def __init__(self, base, clock=time.time):
        self.root = project_root(base)
        self.path = self.root / "state/codex_help.sqlite3"
        self.clock = clock
        self.cfg = settings(base)
        self.path.parent.mkdir(exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                  id INTEGER PRIMARY KEY, fingerprint TEXT, kind TEXT, scope TEXT, brief TEXT,
                  state TEXT, created REAL, updated REAL, expires REAL, attempts INTEGER DEFAULT 0,
                  repeats INTEGER DEFAULT 1, started REAL, finished REAL, result TEXT, error TEXT,
                  input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0);
                CREATE INDEX IF NOT EXISTS jobs_fingerprint ON jobs(fingerprint,updated);
                CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state,created);
                CREATE INDEX IF NOT EXISTS jobs_started ON jobs(started);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, brief, *, deadline=None):
        if not self.cfg["enabled"]:
            return {"accepted": False, "reason": "disabled"}
        if brief.get("kind") not in ("complex", "fault"):
            raise ValueError("invalid help kind")
        scope = brief.get("session_key", "operator") if brief["kind"] == "complex" else "operator"
        if brief["kind"] == "complex" and not re.fullmatch(r"(?:group|private):[1-9][0-9]{0,19}", scope):
            raise ValueError("invalid help session")
        encoded = json.dumps(brief, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode()) > 20000:
            raise ValueError("help brief too large")
        normalized = unicodedata.normalize("NFKC", encoded).lower()
        normalized = re.sub(r"\s+", "", normalized)
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        now = self.clock()
        merge = self.cfg["cache_seconds" if brief["kind"] == "complex" else "fault_merge_seconds"]
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET state='expired',finished=?,error='request_expired' WHERE state='queued' AND expires<=?", (now, now))
            db.execute("DELETE FROM jobs WHERE created<? AND state IN ('completed','failed','expired')", (now-7*86400,))
            old = db.execute("SELECT * FROM jobs WHERE fingerprint=? AND created>=? ORDER BY id DESC LIMIT 1", (digest, now - merge)).fetchone()
            if old is None and brief["kind"] == "fault":
                old = db.execute("SELECT * FROM jobs WHERE kind='fault' AND json_extract(brief,'$.component')=? AND created>=? ORDER BY id DESC LIMIT 1",
                                 (brief.get("component"), now-120)).fetchone()
            if old:
                # Failed/unknown requests are merged too: restarting cannot cause a retry storm.
                db.execute("UPDATE jobs SET repeats=repeats+1, updated=? WHERE id=?", (now, old["id"]))
                return {"accepted": True, "id": old["id"], "merged": True, "state": old["state"]}
            if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= self.cfg["max_pending"]:
                return {"accepted": False, "reason": "queue_full"}
            expiry = now + (150 if brief["kind"] == "complex" else 86400)
            if deadline is not None:
                expiry = min(expiry, deadline)
            cursor = db.execute("INSERT INTO jobs(fingerprint,kind,scope,brief,state,created,updated,expires) VALUES(?,?,?,?,?,?,?,?)",
                                (digest, brief["kind"], scope, encoded, "queued", now, now, expiry))
            return {"accepted": True, "id": cursor.lastrowid, "merged": False, "state": "queued"}

    def claim(self):
        if not self.cfg["enabled"]:
            return None
        now = self.clock()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            # A crashed/in-doubt invocation counts as spent and is never silently replayed.
            db.execute("UPDATE jobs SET state='failed',error='worker_interrupted',finished=? WHERE state='running' AND started<?", (now, now-self.cfg["timeout_seconds"]-30))
            db.execute("UPDATE jobs SET state='expired',error='request_expired',finished=? WHERE state='queued' AND expires<=?", (now, now))
            if db.execute("SELECT 1 FROM jobs WHERE state='running'").fetchone():
                return None
            pause = db.execute("SELECT value FROM meta WHERE key='paused_until'").fetchone()
            if pause and float(pause[0]) > now:
                return None
            calls = db.execute("SELECT * FROM jobs WHERE attempts>0 AND started>?", (now-86400,)).fetchall()
            hour = [r for r in calls if r["started"] > now-3600]
            tokens = sum(r["input_tokens"] + r["output_tokens"] or self.cfg["token_reservation"] for r in calls)
            if (len(hour) >= self.cfg["max_calls_per_hour"] or len(calls) >= self.cfg["max_calls_per_day"]
                    or tokens + self.cfg["token_reservation"] > self.cfg["max_total_tokens_per_day"]
                    or (calls and now-max(r["started"] for r in calls) < self.cfg["min_call_interval_seconds"])):
                return None
            for row in db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY CASE kind WHEN 'complex' THEN 0 ELSE 1 END,id").fetchall():
                if row["kind"] == "complex" and sum(r["scope"] == row["scope"] for r in hour) >= self.cfg["max_session_calls_per_hour"]:
                    continue
                if row["kind"] == "complex" and (sum(r["kind"] == "complex" for r in hour) >= self.cfg["max_complex_calls_per_hour"]
                        or sum(r["kind"] == "complex" for r in calls) >= self.cfg["max_complex_calls_per_day"]):
                    continue
                if row["kind"] == "fault" and sum(r["kind"] == "fault" for r in calls) >= self.cfg["max_fault_calls_per_day"]:
                    continue
                db.execute("UPDATE jobs SET state='running',started=?,updated=?,attempts=1 WHERE id=?", (now, now, row["id"]))
                return {**dict(row), "started": now, "brief": json.loads(row["brief"])}
        return None

    def finish(self, job_id, result=None, error=None, usage=None):
        usage = usage or {}
        now = self.clock()
        with self.db() as db:
            changed = db.execute("UPDATE jobs SET state=?,finished=?,updated=?,result=?,error=?,input_tokens=?,output_tokens=? WHERE id=? AND state='running'",
                ("failed" if error else "completed", now, now, json.dumps(result, ensure_ascii=False) if result else None,
                 re.sub(r"[^a-zA-Z0-9_]", "", str(error))[:80] if error else None,
                 max(0, int(usage.get("input_tokens", 0))), max(0, int(usage.get("output_tokens", 0))), job_id)).rowcount
            if not changed:
                return False
            recent = db.execute("SELECT state FROM jobs WHERE attempts>0 ORDER BY started DESC LIMIT 3").fetchall()
            if len(recent) == 3 and all(r[0] == "failed" for r in recent):
                db.execute("INSERT OR REPLACE INTO meta VALUES('paused_until',?)", (str(now+900),))
        return True

    def get(self, job_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        row = dict(row)
        row["result"] = json.loads(row["result"]) if row["result"] else None
        return row

    def summary(self):
        now = self.clock()
        with self.db() as db:
            counts = dict(db.execute("SELECT state,count(*) FROM jobs GROUP BY state"))
            calls = db.execute("SELECT count(*),COALESCE(sum(input_tokens+output_tokens),0) FROM jobs WHERE attempts>0 AND started>?", (now-86400,)).fetchone()
            latest = db.execute("SELECT id,kind,state,error,repeats,finished FROM jobs ORDER BY updated DESC LIMIT 5").fetchall()
        return {"revision": REVISION, "enabled": self.cfg["enabled"], "counts": counts,
                "calls_24h": calls[0], "reported_tokens_24h": calls[1], "latest": [dict(r) for r in latest]}


def exception_details(exc):
    """Keep actionable exception leaves, without messages, secrets or full paths."""
    detail = {"error_type": type(exc).__name__ if exc else "HealthCheck", "frames": []}
    if exc is None:
        return detail
    detail["frames"] = [{"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                        for f in traceback.extract_tb(exc.__traceback__)[-6:]]
    for key in ("code", "errno", "winerror"):
        value = getattr(exc, key, None)
        if value is not None:
            detail[key] = re.sub(r"[^A-Za-z0-9_.-]", "", str(value))[:80]
    if isinstance(exc, BaseExceptionGroup):
        detail["causes"] = [exception_details(child) for child in exc.exceptions[:6]]
    return detail


def report_fault(base, component, exc=None, code="runtime_error"):
    """Never include raw exception strings, credentials, prompts or turn tokens."""
    try:
        cfg = settings(base)
        if not cfg["enabled"]:
            return None
        try:
            maintenance=json.loads((project_root(base)/"state/service_maintenance.json").read_text(encoding="utf-8-sig"))
            if time.time()<maintenance.get("until",0) and component in {"legacy_bridge","official_worker","official_supervisor"}:
                return None
        except (OSError,ValueError,TypeError):
            pass
        safe = lambda value: re.sub(r"[^A-Za-z0-9_.-]", "", str(value))[:100]
        if code == "runtime_error" and exc is not None and re.fullmatch(r"FALLBACK_[A-Z_]+", str(exc)):
            code = str(exc)  # Reviewed fixed code, never a provider error body.
        return HelpQueue(base).enqueue({**exception_details(exc), "kind": "fault", "component": safe(component), "code": safe(code)})
    except Exception:
        return None  # Fault reporting must never take down the chat loop.


def worker_healthy(base):
    try:
        value = read_json_cached(project_root(base) / "state/codex_help_worker.json", {})
        return value.get("revision") == REVISION and 0 <= time.time()-value.get("at", 0) < 20
    except (OSError, ValueError, TypeError):
        return False
