from contextlib import closing
"""Allowlisted, read-only activity snapshot. No credentials or execution controls."""
import argparse
from datetime import datetime, timedelta
import time
import json
from pathlib import Path
import sqlite3
import sys
import re
from zoneinfo import ZoneInfo
from bili_companion.sync.types import SENSITIVE


DATABASE = Path('state/watch/watcher.sqlite3')


def safe_text(value, limit=200):
    value = value if isinstance(value, str) else ''
    if SENSITIVE.search(value):
        return '[敏感内容已隐藏]'
    return re.sub(r'https?://[^\s<>]+', '[链接已隐藏]', value)[:limit]


def _live_danmaku(db, now):
    zone = ZoneInfo('America/New_York')
    today = datetime.fromtimestamp(now, zone).date()
    days = {today - timedelta(days=offset): [] for offset in range(30)}
    start = datetime.combine(min(days), datetime.min.time(), zone).timestamp()
    rows = db.execute("""
        SELECT at, room, text FROM danmaku_attempts
        WHERE account=? AND state='sent' AND at>=? AND at<=?
          AND typeof(text)='text' AND trim(text)<>''
        ORDER BY at DESC, room, text
    """, ('hakimi-bilibili', start, now))
    for stamp, room, text in rows:
        day = datetime.fromtimestamp(stamp, zone).date()
        days[day].append(dict(at=stamp, room=str(room), text=safe_text(text, 120)))
    return dict(timezone=zone.key, days=[
        dict(date=day.isoformat(), messages=messages) for day, messages in days.items()
    ])


def export_activity(database=DATABASE, bridge_database=Path('state/bridge/bilibili_bridge.sqlite3'), *,
                    now=None, include_live_danmaku=False):
    now = time.time() if now is None else now
    cutoff = now - 30 * 86400
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        videos = []
        for row in db.execute('SELECT bvid,observations,summary,coverage,status,updated_at '
                              'FROM video_history WHERE observations>0 AND updated_at>=? ORDER BY updated_at DESC LIMIT 100', (cutoff,)):
            item = dict(row)
            try:
                raw = json.loads(item['coverage'])
            except (ValueError, TypeError):
                raw = []
            item['coverage'] = [{k: segment[k] for k in ('start', 'end', 'modality') if k in segment}
                                for segment in raw if isinstance(segment, dict)] if isinstance(raw, list) else []
            videos.append(item)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        live = {}
        if include_live_danmaku:
            live['live_danmaku'] = _live_danmaku(db, now) if 'danmaku_attempts' in tables else None
        runtime = {}
        if 'watcher_state' in tables:
            row = db.execute('SELECT value FROM watcher_state WHERE id=1').fetchone()
            state = json.loads(row[0]) if row else {}
            for key in ('state','understanding_state','next_observation_at'):
                if key in state:
                    runtime[key] = state[key]
            error = state.get('last_error')
            runtime['last_error'] = error if isinstance(error,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}',error) else None
        selections = []
        if 'video_selection_audit' in tables:
            for row in db.execute('SELECT request_id,at,bvid,reason,qq_context_json FROM video_selection_audit WHERE at>=? ORDER BY at DESC LIMIT 100', (cutoff,)):
                selections.append(dict(request_id=row['request_id'],at=row['at'],bvid=row['bvid'],reason=safe_text(row['reason'],120),qq_context=json.loads(row['qq_context_json'])))
        comments = [dict(row) for row in db.execute(
            'SELECT bvid,text,state,at FROM video_comment_attempts WHERE at>=? ORDER BY at DESC LIMIT 100', (cutoff,)
        )] if 'video_comment_attempts' in tables else []
        if 'comment_receipts' in tables:
            receipts={r['bvid']:dict(r) for r in db.execute('SELECT bvid,rpid,visibility,checked_at FROM comment_receipts WHERE at>=?',(cutoff,))}
            for comment in comments:comment.update(receipts.get(comment['bvid'],{}))
        if 'comment_read_checks' in tables:
            columns={r[1] for r in db.execute('PRAGMA table_info(comment_read_checks)')}
            optional=''.join(','+name for name in ('anonymous_review_state','account_review_state') if name in columns)
            checks={r['rpid']:dict(r) for r in db.execute('SELECT rpid,anonymous_state,anonymous_code,account_state,account_code,checked_at AS read_checked_at'+optional+' FROM comment_read_checks WHERE checked_at>=? ORDER BY checked_at DESC LIMIT 200',(cutoff,))}
            for comment in comments:
                check=checks.get(comment.get('rpid'))
                if check:
                    check={k:v for k,v in check.items() if k!='rpid'}
                    a,b=check['anonymous_state'],check['account_state']
                    check['read_status']=('public_confirmed' if a=='matched' else 'account_only_readable' if b=='matched'
                        else 'both_not_read' if a in {'missing','mismatch'} and b in {'missing','mismatch'} else 'check_blocked')
                    comment.update(check)
        replies=[dict(row) for row in db.execute('SELECT bvid,root,parent,incoming,text,at,state,rpid FROM comment_reply_attempts WHERE at>=? ORDER BY at DESC LIMIT 100',(cutoff,))] if 'comment_reply_attempts' in tables else []
        for reply in replies:
            reply['incoming']=safe_text(reply['incoming'],1000)
            reply['text']=safe_text(reply['text'],120)
        if 'comment_delivery_outcomes' in tables:
            outcomes={(r['bvid'],r['parent']):dict(r) for r in db.execute('SELECT bvid,parent,code,notice,review_state FROM comment_delivery_outcomes WHERE at>=?',(cutoff,))}
            for item in comments+replies:
                outcome=outcomes.get((item['bvid'],item.get('parent',0)))
                if outcome:item.update({k:outcome[k] for k in ('code','notice','review_state')})
    reads = []
    bridge = Path(bridge_database)
    if bridge.exists():
        with closing(sqlite3.connect(bridge.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for selection in selections:
                contexts = []
                for ref in selection['qq_context']:
                    current = db.execute('SELECT payload,expires FROM sync_facts WHERE source_id=? AND revision=?', (ref['source_id'],ref['revision'])).fetchone() if 'sync_facts' in tables else None
                    active = current and current['expires'] > now
                    contexts.append(dict(source_id=ref['source_id'],revision=ref['revision'],state='available' if active else 'unavailable',
                        summary=safe_text(json.loads(current['payload']).get('text','')) if active else '该修订已过期、撤回或被更新；当时提供的来源编号仍保留'))
                selection['qq_context'] = contexts
            if 'qq_memory_reads' in tables:
                for row in db.execute('SELECT read_at,keyword,status,records_json FROM qq_memory_reads WHERE read_at>=? ORDER BY id DESC LIMIT 100', (cutoff,)):
                    records = []
                    for record in json.loads(row['records_json'])[:6]:
                        current = db.execute('SELECT payload,expires FROM sync_facts WHERE source_id=? AND revision=?', (record['source_id'],record['revision'])).fetchone() if 'sync_facts' in tables else None
                        state = 'available' if current and current['expires'] > now else 'expired_or_retracted'
                        records.append(dict(source_id=record['source_id'], revision=record['revision'], state=state,
                            summary=safe_text(record['summary']) if state == 'available' else '正文已过期或撤回'))
                    reads.append(dict(read_at=row['read_at'],keyword=safe_text(row['keyword'],80),status=row['status'],records=records))
            elif {'qq_interest_imports','sync_facts'} <= tables:
                # Legacy receive timestamps are genuine, but there is no per-pass audit.
                for row in db.execute('SELECT i.source_id,i.revision,i.read_at,i.keyword,f.payload,f.expires FROM qq_interest_imports i LEFT JOIN sync_facts f ON f.channel=i.channel AND f.source_id=i.source_id AND f.revision=i.revision WHERE i.read_at>=? ORDER BY i.read_at DESC LIMIT 100', (cutoff,)):
                    active = row['payload'] and row['expires'] > now
                    body = json.loads(row['payload']).get('text','') if active else '正文已过期或撤回'
                    reads.append(dict(read_at=row['read_at'],keyword=safe_text(row['keyword'],80),status='legacy_import',records=[dict(source_id=row['source_id'],revision=row['revision'],state='available' if active else 'expired_or_retracted',summary=safe_text(body))]))
    if not bridge.exists():
        for selection in selections:
            selection['qq_context'] = [dict(**r,state='unavailable',summary='来源正文当前不可用；保留当时提供的来源编号') for r in selection['qq_context']]
    return dict(schema_version=2, exported_at=now, **live,
                video_history=videos, video_comment_attempts=comments, comment_reply_attempts=replies, qq_memory_reads=reads, video_selections=selections, runtime_status=runtime)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live-danmaku', action='store_true', help='Include accepted daily livestream messages')
    options = parser.parse_args()
    try:
        output = json.dumps(export_activity(include_live_danmaku=options.live_danmaku), ensure_ascii=False, allow_nan=False)
    except Exception:
        print('activity_export_failed', file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())


