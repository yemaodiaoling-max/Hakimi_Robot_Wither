"""Cheap daily gate. Writing and drawing are done only by the Codex automation."""
from hakimi import paths
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from hakimi.models.provider_health import availability
from hakimi.features.qzone_state import QZONE_TIMEZONE, daily_status

BASE = paths.root()


def current_health():
    marker=BASE/'state/cloud_host.json'
    if marker.exists() and json.loads(marker.read_text(encoding='utf-8')).get('active'):
        try:
            result=subprocess.run([sys.executable,str(BASE/'maintenance/cloud_activity.py'),'status'],
                capture_output=True,timeout=25)
            if result.returncode==0:return json.loads(result.stdout)['provider_health']
        except (OSError,ValueError,KeyError,subprocess.TimeoutExpired):pass
        return {'mode':'gemini','fast':False,'unverified_cloud':True}
    return availability(BASE)


def check(now=None):
    now = now or datetime.now(QZONE_TIMEZONE)
    now = now.astimezone(QZONE_TIMEZONE)
    if (BASE/'state/diary_mode.json').exists():
        return {'eligible':False,'reason':'replaced_by_private_diary','timezone':str(QZONE_TIMEZONE)}
    status = daily_status()
    # A stable daily jitter survives restarts without another state file.
    minute = 36 + int(hashlib.sha256(status["day"].encode()).hexdigest()[:8], 16) % 9
    due = now.replace(hour=22, minute=minute, second=0, microsecond=0)
    wait = max(0, round((due-now).total_seconds()))
    health = current_health()
    fast = health.get("mode") == "dsh" or (health.get("fast") and health.get("mode") != "gemini")
    within = now.hour == 22 and now.minute >= 30 or now.hour == 23 and now.minute < 20
    reason = "ready" if fast else "gemini_primary"
    if health.get('unverified_cloud'):reason='cloud_health_unavailable'
    if status["state"] != "not_published":reason = status["state"]
    elif not within:reason = "outside_evening_window"
    return {**status, "eligible": bool(fast and within and status["state"] == "not_published"),
            "reason": reason, "wait_seconds": wait, "not_before": due.isoformat(),
            "timezone": str(QZONE_TIMEZONE)}


if __name__ == "__main__":
    print(json.dumps(check(), ensure_ascii=False))
