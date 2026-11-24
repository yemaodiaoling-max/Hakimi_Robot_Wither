"""QQ space daily post state: one post per New York day, shared by publisher and gate."""
import json
import os
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from hakimi import paths


BASE_DIR = paths.root()
STATE_DIR = BASE_DIR / "state"
QZONE_LOG = STATE_DIR / "qzone_posts.jsonl"
QZONE_TIMEZONE = ZoneInfo("America/New_York")


def daily_status():
    day = datetime.now(QZONE_TIMEZONE).date().isoformat()
    if QZONE_LOG.exists():
        for line in QZONE_LOG.read_text(encoding="utf-8").splitlines()[-60:]:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if str(row.get("timestamp", ""))[:10] == day and row.get("ret") == 0:
                return {"day": day, "state": "published"}
    marker = STATE_DIR / "qzone_daily.json"
    try:
        row = json.loads(marker.read_text(encoding="utf-8"))
        if row.get("day") == day:
            return {"day": day, "state": row.get("state", "unknown")}
    except (OSError, ValueError):
        pass
    return {"day": day, "state": "not_published"}


@contextmanager
def daily_lock():
    with (STATE_DIR / "qzone_daily.lock").open("a+b") as stream:
        stream.write(b"0")
        stream.flush()
        stream.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
