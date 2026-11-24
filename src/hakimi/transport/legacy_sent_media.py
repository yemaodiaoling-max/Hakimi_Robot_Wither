"""Recover historical media descriptions without treating queued media as delivered."""
from collections import Counter, defaultdict
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

from hakimi.core.jsonio import tail_jsonl
from hakimi.core.runtime_io import read_derived_cached


_IMAGE = re.compile(r'\[CQ:image,file=file://[^,\]\r\n]+,sub_type=[01]\]')
_FILE = re.compile(r'\[CQ:file,[^\]\r\n]+\]')


def _review_media(state, wanted):
    """Open review windows retain the actual post-transport bubble, including substitutions."""
    db_path = state / 'memory.sqlite3'
    if not db_path.is_file():
        return {}
    sessions = {key[0] for key in wanted}
    found = defaultdict(set)
    try:
        with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.15)) as db:
            for session in sessions:
                rows = db.execute("SELECT replies FROM reply_review_windows WHERE session=? AND replies!='[]' "
                                  "ORDER BY updated_at DESC LIMIT 32", (session,))
                for (raw,) in rows:
                    for item in json.loads(raw):
                        if not isinstance(item, dict) or item.get('speaker') != 'SELF':
                            continue
                        key = (session, str(item.get('message_id', '')))
                        if key not in wanted:
                            continue
                        text = item.get('text', '')
                        # Historical review excerpts were clipped at 240 characters.
                        if isinstance(text, str) and len(text) < 240 and (_IMAGE.fullmatch(text) or _FILE.fullmatch(text)):
                            found[key].add(text)
                        else:
                            found[key].add(None)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return {}
    return {key: next(iter(values)) if len(values) == 1 else None for key, values in found.items()}


def _outbox(path):
    by_turn = {}
    for row in tail_jsonl(path, count=1000, max_bytes=2*1024*1024):
        key = (row.get('session_key'), row.get('trigger_id'))
        by_turn[key] = row if key not in by_turn else None
    return by_turn


def _match_image(acks, batch):
    """Fallback for old reviewed turns. Require one non-substitutable image and a complete batch."""
    if not batch or not isinstance(batch.get('segments'), list):
        return None
    segments = batch['segments']
    if len(acks) != len(segments) or len({str(r['message_id']) for r in acks}) != len(acks):
        return None
    if not all(isinstance(s, str) for s in segments):
        return None
    images = [i for i,s in enumerate(segments) if _IMAGE.fullmatch(s)]
    if len(images) != 1:
        return None
    index = images[0]
    ordered = sorted(acks, key=lambda r:r['at'])
    if len({r['at'] for r in ordered}) != len(ordered):
        return None
    created = batch.get('created_at')
    if type(created) not in (int,float) or ordered[0]['at'] < created:
        return None
    for i,(segment, ack) in enumerate(zip(segments, ordered)):
        if ack.get('provider') != batch.get('provider'):
            return None
        if i == index:
            if ack.get('text') != '':
                return None
            continue
        # Do not infer any batch containing substituted files, mentions, truncation,
        # or another ambiguous non-text bubble. The old receipt kept only 1500 chars.
        if '[CQ:' in segment or not segment.strip() or len(segment.strip()) >= 1500 or ack.get('text') != segment.strip():
            return None
    return (ordered[index]['session_key'], str(ordered[index]['message_id'])), segments[index]


def legacy_media_segments(receipts, base):
    """Return receipt-keyed CQ descriptions. Normal asset authorization still applies."""
    valid = [r for r in receipts if isinstance(r,dict) and r.get('session_key') and r.get('message_id')
             and type(r.get('at')) in (int,float)]
    counts = Counter((r['session_key'], str(r['message_id'])) for r in valid)
    wanted = {(r['session_key'], str(r['message_id'])) for r in valid if r.get('text') == '' and not r.get('media')}
    wanted = {key for key in wanted if counts[key] == 1}
    if not wanted:
        return {}
    state = Path(base) / 'state'
    known = _review_media(state, wanted)
    remaining = wanted - known.keys()
    if remaining:
        turns = {(r['session_key'],r['trigger_id']) for r in valid if r.get('trigger_id') and
                 (r['session_key'],str(r['message_id'])) in remaining}
        grouped = defaultdict(list)
        for r in valid:
            key = (r['session_key'], r.get('trigger_id'))
            if key in turns:
                grouped[key].append(r)
        boxes = read_derived_cached(state/'outbox.jsonl', _outbox, 'legacy-single-image-v1')
        for key, acks in grouped.items():
            match = _match_image(acks, boxes.get(key))
            if match and match[0] in remaining:
                known[match[0]] = match[1]
    return {key:value for key,value in known.items() if value}
