"""Read the system clock without network access, commands, or mutable state."""

import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone

TIMEZONES = ("local", "UTC", "Asia/Shanghai")
WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def clock_snapshot(timezone_name="local", now=None):
    """Only the current clock is public; `now` is an internal test seam."""
    if not isinstance(timezone_name, str) or timezone_name not in TIMEZONES:
        raise ValueError("timezone 仅支持 local、UTC、Asia/Shanghai")
    now = time.time() if now is None else now
    try:
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ValueError("系统时间无效")
        utc = datetime.fromtimestamp(now, timezone.utc)
        if timezone_name == "local":
            current = utc.astimezone()
        elif timezone_name == "Asia/Shanghai":
            # Current Beijing time is UTC+08:00; no optional tzdata dependency.
            current = utc.astimezone(timezone(timedelta(hours=8)))
        else:
            current = utc
    except (OSError, OverflowError, ValueError) as exc:
        raise ValueError("系统时间无法转换") from exc
    offset = current.strftime("%z")
    offset = offset[:3] + ":" + offset[3:5] + (":" + offset[5:] if len(offset) > 5 else "")
    return {
        "datetime": current.isoformat(timespec="seconds"),
        "date": current.strftime("%Y-%m-%d"),
        "time": current.strftime("%H:%M:%S"),
        "weekday": WEEKDAYS[current.weekday()],
        "timezone": timezone_name,
        "timezone_label": ("本机时区 " + (current.tzname() or "local") if timezone_name == "local" else timezone_name) + " UTC" + offset,
        "utc_offset": offset,
        "unix_seconds": int(now),
        "utc_datetime": utc.isoformat(timespec="seconds"),
        "source": "system_clock",
    }


def wake_time(now=None):
    """A short trusted snapshot shared by Gemini and the zero-tool fallback."""
    snapshot = clock_snapshot(now=now)
    beijing = clock_snapshot("Asia/Shanghai", snapshot["unix_seconds"])
    return (f"{snapshot['datetime']} {snapshot['weekday']}（{snapshot['timezone_label']}；唤醒时快照）；"
            f"北京时间 Asia/Shanghai：{beijing['datetime']} {beijing['weekday']}（UTC+08:00）。"
            "涉及群友作息时先确认其时区，不把本机时间当成北京时间。")


def main():
    parser = argparse.ArgumentParser(description="只读查询当前系统时间，不联网、不设置时间")
    parser.add_argument("--timezone", choices=TIMEZONES, default="local")
    args = parser.parse_args()
    try:
        result = clock_snapshot(args.timezone)
    except ValueError as exc:
        parser.error(str(exc))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
