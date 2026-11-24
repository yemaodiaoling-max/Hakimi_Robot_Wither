"""Select the same small reply window for either model, without extra I/O."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

CHAT_ZONE = ZoneInfo('America/New_York')


def chronological_rows(rows):
    """Use receipt times, recovering clock-only legacy SELF rows in their own day."""
    def receipt(row):
        value = row.get('received_at')
        return value if type(value) in (int, float) and 0 < value < 32503680000 else None
    known = [(i, receipt(row)) for i, row in enumerate(rows) if receipt(row) is not None]
    if not known:
        return rows
    ordered = []
    for i, row in enumerate(rows):
        at = receipt(row)
        if at is None:
            position, reference = min(known, key=lambda item: abs(item[0]-i))
            try:
                clock = datetime.strptime(str(row.get('time', '')), '%H:%M:%S')
                day = datetime.fromtimestamp(reference,CHAT_ZONE).replace(hour=clock.hour, minute=clock.minute,
                                                                         second=clock.second, microsecond=0)
                at = day.timestamp()
                if at-reference > 43200:at = (day-timedelta(days=1)).timestamp()
                elif reference-at > 43200:at = (day+timedelta(days=1)).timestamp()
            except (ValueError, OverflowError, OSError):
                at = reference + (i-position)*.000001
            row = dict(row, received_at=at)
        ordered.append((at, i, row))
    return [row for _, _, row in sorted(ordered, key=lambda item: item[:2])]


def select_context(data, limit=12):
    session = data.get("session_key")
    rows, seen = [], set()
    window = data.get("listening_window")
    source = chronological_rows([row for row in data.get("recent_context_messages", [])
                                 if isinstance(row, dict) and row.get("session_key", session) == session])
    if not (isinstance(window, dict) and isinstance(window.get("selected_message_ids"), list)):
        source = source[-80:]
    for row in source:
        if not isinstance(row, dict) or row.get("session_key", session) != session:
            continue
        mid = str(row.get("message_id", ""))
        if mid and mid in seen:
            continue
        if mid:
            seen.add(mid)
        rows.append(row)
    if not rows:
        return [], {}
    directed = data.get("pending_explicit_question_ids", [])
    directed = [str(mid) for mid in directed[:3]] if isinstance(directed, list) else []
    targets = [str(data.get("reply_target_message_id", ""))] + directed[::-1]
    anchor = next((i for mid in targets if mid for i, row in enumerate(rows)
                   if str(row.get("message_id", "")) == mid), None)
    if anchor is None:
        anchor = next((i for i in range(len(rows)-1, -1, -1) if rows[i].get("user_id") != "SELF"), len(rows)-1)
    target = rows[anchor]
    nearby = sorted(range(len(rows)), key=lambda i: (abs(i-anchor), -i))
    window = data.get("listening_window")
    if isinstance(window, dict) and isinstance(window.get("selected_message_ids"), list):
        chosen = {str(mid) for mid in window["selected_message_ids"]}
        selected = [row for row in rows if str(row.get("message_id", "")) in chosen and row.get("user_id") != "SELF"]
        own = [row for row in rows if row.get("user_id") == "SELF"][-2:]
        ids = {str(row.get("message_id", "")) for row in selected + own}
        return [row for row in rows if str(row.get("message_id", "")) in ids], selected[-1] if selected else {}
    if isinstance(window, dict):
        ids = {str(mid) for mid in window.get("new_message_ids", [])}
        fresh = [i for i, row in enumerate(rows) if str(row.get("message_id", "")) in ids and row.get("user_id") != "SELF"]
        if fresh:
            anchor = fresh[-1]
            selected = set(fresh[-max(1, limit-2):])
            selected.update([i for i, row in enumerate(rows[:anchor]) if row.get("user_id") == "SELF"][-2:])
            for i in sorted(range(len(rows)), key=lambda i:(abs(i-anchor),-i)):
                if len(selected) >= limit:break
                selected.add(i)
            return [rows[i] for i in sorted(selected)], rows[anchor]
    author = str(target.get("user_id", ""))
    own = [i for i in range(len(rows)-1, -1, -1)
           if author and author != "SELF" and str(rows[i].get("user_id", "")) == author]
    selected = {anchor} | {i for i, row in enumerate(rows) if str(row.get("message_id", "")) in directed}
    # First six from each route; overlap is filled by looking farther along both.
    for start, stop in ((0, 6), (6, len(rows))):
        for offset in range(start, stop):
            for route in (nearby, own):
                if offset < len(route) and len(selected) < limit:
                    selected.add(route[offset])
    return [rows[i] for i in sorted(selected)], target
