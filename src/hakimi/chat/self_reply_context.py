"""Restore recent delivered SELF replies independently of inbound selection."""
import hashlib
import math
import re
from pathlib import Path

from hakimi.core.context_window import CHAT_ZONE, chronological_rows
from hakimi.core.jsonio import tail_jsonl
from hakimi.core.runtime_io import read_derived_cached
from hakimi.chat.sent_media_context import enrich_sent_rows
from datetime import datetime

MAX_OWN_REPLIES = 5
MAX_AGE_SECONDS = 3600


def _stamp(value):
    return value if type(value) in (int, float) and math.isfinite(value) and 0 < value < 32503680000 else None


def recent_sent_receipts(base, session, cutoff):
    path = Path(base)/'state/sent_messages.jsonl'
    receipts = read_derived_cached(path, lambda p: tail_jsonl(p, count=256, max_bytes=1048576),
                                   'recent-own-replies-v1')
    return [dict(row) for row in receipts if row.get('session_key') == session
            and _stamp(row.get('at')) is not None and 0 <= cutoff-row['at'] <= MAX_AGE_SECONDS]


def with_recent_own_replies(messages, data, base, now):
    """A receipt is context evidence, never a new inbound message or reply target.

    Bound both bytes and rows, reuse unchanged log parsing, and cap evidence at
    the selected input's receipt time so replayed/slow turns cannot see a reply
    delivered after their input. A missing input timestamp falls back to the
    payload creation time; legacy callers without either keep their old view.
    """
    session = data.get('session_key')
    if not isinstance(session, str) or not session:
        return messages
    times = [_stamp(row.get('received_at')) for row in messages if row.get('user_id') != 'SELF']
    cutoff = max((at for at in times if at is not None), default=None)
    cutoff = cutoff if cutoff is not None else _stamp(data.get('created_at'))
    if cutoff is None or _stamp(now) is None:
        return messages
    cutoff = min(cutoff, now)
    references = {str(row.get('reply_to')) for row in messages
                  if row.get('user_id') != 'SELF' and row.get('reply_to')}
    own, unidentified = {}, []
    # Existing daemon SELF entries are also added only after a successful send.
    for row in messages:
        at = _stamp(row.get('received_at'))
        if row.get('user_id') == 'SELF' and row.get('session_key', session) == session and (at is None or at <= cutoff):
            mid = str(row.get('message_id') or '')
            if mid:
                own[mid] = dict(row, message_id=mid)
            else:
                # An absent ID provides no deduplication evidence. Keep each
                # original history row, rather than collapsing them under ''.
                unidentified.append(dict(row))
    existing_ids = set(own)
    eligible = recent_sent_receipts(base, session, cutoff)
    enriched = enrich_sent_rows(eligible, base)
    for raw, row in zip(eligible, enriched):
        at = row['at']
        text = str(row.get('text') or '')
        if not text.strip():
            continue
        mid = str(row.get('message_id') or '')
        if not mid:
            mid = 'sent-' + hashlib.sha256(f'{session}:{at}:{text}'.encode()).hexdigest()[:20]
        existing = own.get(mid)
        if existing and str(existing.get('text') or '').strip():
            # The durable receipt text is deliberately capped at 1500 chars;
            # it must not replace an already selected, complete live message.
            if isinstance(row.get('media'), dict):
                live = re.sub(r'\[CQ:(?:image|file),[^\]]*\]', '', str(existing['text'])).strip()
                raw_text = str(raw.get('text') or '')
                label = text[len(raw_text):].strip() if text.startswith(raw_text) else ''
                if label and label not in live:
                    live = (live + ' ' + label).strip()
                own[mid] = dict(existing, text=live or text, media=row['media'])
            continue
        own[mid] = dict(row, text=text, message_id=mid, user_id='SELF', nickname='哈基米',
                        session_key=session, received_at=at,
                        time=datetime.fromtimestamp(at, CHAT_ZONE).strftime('%H:%M:%S'))
    ordered = chronological_rows(list(own.values()))
    retained = {row['message_id']: row for row in ordered[-MAX_OWN_REPLIES:]}
    retained.update({mid: own[mid] for mid in existing_ids})
    for row in [row for row in ordered if row['message_id'] in references][-3:]:
        retained[row['message_id']] = row
    recent = list(retained.values())
    inbound = [row for row in messages if row.get('user_id') != 'SELF']
    return chronological_rows([*inbound, *recent, *unidentified])
