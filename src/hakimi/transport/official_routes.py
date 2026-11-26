"""Durable official-channel routing: openid aliases, group/member mapping, reply windows."""
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from hakimi.chat.official_base import OfficialError, alias


class RouteStore:
    def __init__(self, path, clock=time.time, *, persistent=False):
        self.path, self.clock = Path(path), clock
        self.persistent = persistent
        self._connection = None
        self._db_lock = threading.RLock()
        self._db_depth = 0
        self._next_cleanup = 0
        if self.path.is_symlink() or self.path.resolve() != self.path.absolute():
            raise OfficialError("REDIRECTED_ROUTE_STORE")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages
                  (local_id TEXT PRIMARY KEY, remote_id TEXT NOT NULL, ref_idx TEXT,
                   at REAL NOT NULL, direction TEXT NOT NULL, seq INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS turns (trigger TEXT PRIMARY KEY, source TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (key TEXT PRIMARY KEY, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS actors (local_id TEXT PRIMARY KEY, openid TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS messages_time ON messages(at);
                CREATE INDEX IF NOT EXISTS messages_reference ON messages(ref_idx);
            """)
            if "group_openid" not in {r[1] for r in db.execute("PRAGMA table_info(messages)")}:
                db.execute("ALTER TABLE messages ADD COLUMN group_openid TEXT NOT NULL DEFAULT ''")
            if "user_id" not in {r[1] for r in db.execute("PRAGMA table_info(messages)")}:
                db.execute("ALTER TABLE messages ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE INDEX IF NOT EXISTS messages_group ON messages(group_openid)")
            old = db.execute("SELECT value FROM settings WHERE key='group_openid'").fetchone()
            if old and isinstance(legacy := json.loads(old[0]), str) and legacy:
                db.execute("UPDATE messages SET group_openid=? WHERE group_openid=''", (legacy,))

    @contextmanager
    def db(self):
        if self.persistent:
            with self._db_lock:
                if self._connection is None:
                    self._connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
                    self._connection.row_factory = sqlite3.Row
                self._db_depth += 1
                try:
                    if self._db_depth == 1:
                        with self._connection:
                            yield self._connection
                    else:
                        yield self._connection
                finally:
                    self._db_depth -= 1
            return
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                db.row_factory = sqlite3.Row
                yield db
        finally:
            db.close()

    def close(self):
        with self._db_lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db() as db:
            db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value "
                       "WHERE settings.value<>excluded.value", (key, json.dumps(value)))

    def arm(self):
        code = secrets.token_hex(4)
        self.set("pairing", {"hash": hashlib.sha256(code.encode()).hexdigest(), "expires": self.clock() + 900})
        return code

    def prepare_identity(self, app_id, group_number):
        """Correct an unused setup placeholder; never reassign real account data."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            values = {row[0]: json.loads(row[1]) for row in db.execute("SELECT key,value FROM settings")}
            if values.get("expected_qq_group") not in (None, group_number):
                raise OfficialError("TEST_GROUP_CHANGED_REQUIRES_NEW_BINDING")
            changed = values.get("app_id") not in (None, app_id)
            if changed:
                has_data = any(db.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchone()
                               for table in ("messages", "turns", "attempts", "actors"))
                if (has_data or any(values.get(key) for key in
                        ("group_openid", "self_remote", "member_mapping", "memory_mapping"))):
                    raise OfficialError("APP_ID_CHANGED_REQUIRES_EXPLICIT_NEW_BINDING")
                db.execute("DELETE FROM settings WHERE key='pairing'")
            for key, value in (("app_id", app_id), ("expected_qq_group", group_number)):
                db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def bind(self, data, content):
        match = re.fullmatch(r"\s*/绑定测试群\s+([a-f0-9]{8})\s*", content)
        if not match or self.get("group_openid"):
            return False
        pairing = self.get("pairing", {})
        if (self.clock() >= pairing.get("expires", 0)
                or not secrets.compare_digest(hashlib.sha256(match[1].encode()).hexdigest(), pairing.get("hash", ""))
                or data.get("author", {}).get("member_role") not in {"owner", "admin"}):
            return False
        group = data.get("group_openid")
        if not isinstance(group, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", group):
            return False
        self.set("group_openid", group)
        self.set("pairing", {})
        return True

    def remember(self, remote_id, ref_idx, at, direction="in", group=None, user_id=""):
        local_id = alias("message", remote_id)
        with self.db() as db:
            found = db.execute("SELECT 1 FROM messages WHERE local_id=?", (local_id,)).fetchone()
            if found:
                return local_id, False
            db.execute("INSERT INTO messages(local_id,remote_id,ref_idx,at,direction,seq,group_openid,user_id) VALUES (?,?,?,?,?,0,?,?)",
                       (local_id, remote_id, ref_idx, at, direction, group or self.get("group_openid") or "", str(user_id)))
            now = self.clock()
            if now >= self._next_cleanup:
                db.execute("DELETE FROM messages WHERE at<?", (now - 86400,))
                db.execute("DELETE FROM turns WHERE source NOT IN (SELECT local_id FROM messages)")
                self._next_cleanup = now + 60
        return local_id, True

    def message(self, local_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM messages WHERE local_id=?", (str(local_id),)).fetchone()
            return dict(row) if row else None

    def quote_reference(self, local_id, group):
        """At send time, quote a displaced target. Ignore at most 3 author supplements."""
        with self.db() as db:
            target = db.execute("SELECT rowid,* FROM messages WHERE local_id=? AND group_openid=?",
                                (str(local_id), group)).fetchone()
            if not target or not target['ref_idx']:
                return None
            # Receipt order also handles identical platform timestamps. Four rows
            # suffice: either someone intervened, or the author has added four.
            rows = db.execute("SELECT direction,user_id FROM messages WHERE group_openid=? AND rowid>? "
                              "ORDER BY rowid LIMIT 4", (group, target['rowid'])).fetchall()
            if len(rows) == 4 or any(row['direction'] == 'out' or not target['user_id']
                                     or row['user_id'] != target['user_id'] for row in rows):
                return {'message_id': target['ref_idx']}
            return None

    def ref_local(self, ref_idx, group=None):
        with self.db() as db:
            row = db.execute("SELECT local_id FROM messages WHERE ref_idx=? AND (? IS NULL OR group_openid=?) ORDER BY at DESC LIMIT 1",
                             (ref_idx, group, group)).fetchone()
            return row[0] if row else None

    def attach_turn(self, trigger, source):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO turns VALUES (?,?)", (trigger, source))

    def source(self, trigger):
        with self.db() as db:
            row = db.execute("SELECT messages.* FROM messages JOIN turns ON messages.local_id=turns.source "
                             "WHERE turns.trigger=?", (trigger,)).fetchone()
            return dict(row) if row else None

    def actor(self, openid):
        local = alias("member", openid)
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO actors VALUES (?,?)", (local, openid))
        return local

    def reserve(self, key, source, allow_active=False):
        """Reserve before any network I/O; an uncertain result is never replayed."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM attempts WHERE key=?", (key,)).fetchone():
                raise OfficialError("DUPLICATE_ATTEMPT", ambiguous=True)
            if source:
                row = db.execute("SELECT * FROM messages WHERE local_id=?", (source["local_id"],)).fetchone()
                if not row or row["direction"] != "in" or self.clock() >= row["at"] + 295 or row["seq"] >= 5:
                    if not allow_active:
                        raise OfficialError("REPLY_WINDOW_OR_COUNT_EXHAUSTED")
                    row = None
            else:
                row = None
            if not row and not allow_active:
                raise OfficialError("NO_REPLY_AUTHORITY")
            db.execute("INSERT INTO attempts VALUES (?, 'reserved')", (key,))
            if row:
                seq = row["seq"] + 1
                db.execute("UPDATE messages SET seq=? WHERE local_id=?", (seq, row["local_id"]))
                return {"msg_id": row["remote_id"], "msg_seq": seq}
            return {}
