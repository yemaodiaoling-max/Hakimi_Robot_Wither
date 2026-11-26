"""QQ attachment references belong to the event's session, never to model-supplied URLs."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

from hakimi.core.context_window import select_context
from hakimi.core.runtime_io import read_json_cached
from hakimi.voice.audio import approved

LIMIT = 64 * 1024 * 1024
TTL = 86400


def register(base, session, mid, attachments):
    markers = []
    for index, item in enumerate(attachments[:8] if isinstance(attachments, list) else []):
        if not isinstance(item, dict):
            continue
        mime = str(item.get('content_type', '')).split(';')[0].lower()
        name = str(item.get('filename', '')).lower()
        if not (mime.startswith('video/') or mime in ('','application/octet-stream') and name.endswith(('.mp4','.mov','.webm','.mkv'))):
            continue
        url = item.get('url') or ''
        if isinstance(url,str) and url.startswith('//'):
            url = 'https:' + url
        if not approved(url) or isinstance(item.get('size'),int) and item['size'] > LIMIT:
            markers.append('[QQ视频附件无法读取]')
            continue
        ref = f'{mid}-{index}'
        now = time.time()
        with closing(sqlite3.connect(Path(base)/'state/qq_video.sqlite3')) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS attachments(session TEXT,ref TEXT,url TEXT,expires REAL,report TEXT,PRIMARY KEY(session,ref))')
            db.execute('DELETE FROM attachments WHERE expires<=?', (now,))
            db.execute('INSERT OR IGNORE INTO attachments VALUES (?,?,?,?,NULL)', (session,ref,url,now+TTL))
        markers.append(f'[QQ视频 video.watch_attachment ref={ref}]')
    return ' '.join(markers)


def watch(base, payload, ref, timeout=90, *, owner=None, is_active=None):
    from hakimi.chat.turn_text import network_allowed
    if not network_allowed(payload,base):
        return {'ok':False,'error':'WEB_DISABLED_FOR_THIS_TURN'}
    rows,target = select_context(payload)
    mids = {str(row.get('message_id')) for row in rows if row.get('user_id')!='SELF'}
    if target.get('reply_to'):
        mids.add(str(target['reply_to']))
    session = payload.get('session_key')
    path = Path(base)/'state/qq_video.sqlite3'
    if not isinstance(ref,str) or ref.rpartition('-')[0] not in mids or not path.is_file():
        return {'ok':False,'error':'video_attachment_not_visible'}
    with closing(sqlite3.connect(path)) as db:
        row = db.execute('SELECT url,report FROM attachments WHERE session=? AND ref=? AND expires>?',(session,ref,time.time())).fetchone()
    if not row:
        return {'ok':False,'error':'video_attachment_expired_or_missing'}
    if is_active and not is_active():
        return {'ok':False,'error':'TURN_ALREADY_TAKEN_OVER'}
    root = Path(base)
    if root.name=='official_test_runtime':
        root=root.parent
    cfg=read_json_cached(root/'video_tools_config.json',{})
    config_path = cfg.get('companion_config','/opt/hakimi/bili_companion/deploy/watcher.json')
    companion = read_json_cached(Path(config_path), {})
    feedback_enabled = bool(companion.get('video_selection_profile'))
    observation_route = json.dumps(['av-detail-v2', companion.get('video_native_vertex_model'),
                                    companion.get('video_hydrogen_profile'),
                                    companion.get('claude_video_profile'), companion.get('hydrogen_route')],
                                   ensure_ascii=False, sort_keys=True)
    from hakimi.tools.bilibili_video import FEEDBACK_REVISION, feedback_input, notes_receipt, receive_frame
    caption = next((item for item in rows if str(item.get('message_id')) == ref.rpartition('-')[0]), None)
    question = feedback_input(payload, caption) if feedback_enabled else None
    trigger = str(payload.get('trigger_id') or payload.get('message_id') or target.get('message_id') or '')
    identity = hashlib.sha256(json.dumps([FEEDBACK_REVISION, session, ref, trigger, question],
                                         ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    try:
        cached = json.loads(row[1]) if row[1] else {}
    except (TypeError, ValueError):
        cached = {}
    observing = cached.get('observations') if cached.get('observer') == observation_route else None
    if not feedback_enabled and cached.get('revision') == 'timeline-v3' and cached.get('observer') == observation_route:
        return cached['report']
    env=os.environ.copy()
    env['PYTHONPATH']=str(cfg.get('companion_source','/opt/hakimi/bili_companion/src'))+os.pathsep+str(Path(__file__).resolve().parents[2])+os.pathsep+env.get('PYTHONPATH','')
    with tempfile.TemporaryDirectory(prefix='qq-video-') as tmp:
        frame = Path(tmp) / 'frame.jpg'
        if feedback_enabled:
            now = time.time()
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('''CREATE TABLE IF NOT EXISTS viewing_requests (
                    request_id TEXT PRIMARY KEY, expires REAL NOT NULL, report TEXT)''')
                db.execute('DELETE FROM viewing_requests WHERE expires <= ?', (now,))
                claim = db.execute('INSERT OR IGNORE INTO viewing_requests VALUES (?,?,NULL)', (identity, now + TTL))
                if not claim.rowcount:
                    previous = db.execute('SELECT report FROM viewing_requests WHERE request_id=?', (identity,)).fetchone()
                    if not previous or not previous[0]:
                        return {'ok':False,'error':'QQ_VIDEO_REQUEST_IN_PROGRESS'}
                    report = json.loads(previous[0])
                    receive_frame(report, frame, base, session, identity, owner)
                    return notes_receipt(report, keep_frame_path=True) or report
        try:
            limit = max(1, min(timeout, 120))
            result=subprocess.run([sys.executable,'-m','hakimi.tools.qq_video_worker'],env=env,
                input=json.dumps(dict(url=row[0],ref=ref,session=session,tmp=tmp,config=config_path,
                    timeout=max(1,limit-2),request_id=identity,question=question,observations=observing)),
                text=True,encoding='utf-8',capture_output=True,timeout=limit)
            report=json.loads(result.stdout)
            if not isinstance(report, dict):
                raise ValueError('invalid_video_report')
        except subprocess.TimeoutExpired:
            checkpoint = Path(tmp) / 'coverage.json'
            try:
                report = json.loads(checkpoint.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                report = {'ok':False,'error':'video_attachment_view_timeout'}
        except (OSError,ValueError):
            report = {'ok':False,'error':'video_attachment_view_failed'}
        report.pop('model_request_ids', None)
        with closing(sqlite3.connect(path)) as db, db:
            if feedback_enabled:
                db.execute('UPDATE viewing_requests SET report=? WHERE request_id=?',
                           (json.dumps(report, ensure_ascii=False), identity))
            if report.get('ok') and not report.get('unfinished_samples'):
                receipt = {'revision':'timeline-v3','observer':observation_route,'report':report}
                if feedback_enabled:
                    raw = {key:value for key,value in report.items()
                           if key not in ('feedback', 'synthesis_status', 'observations_cached')}
                    receipt = dict(revision='timeline-v3', observer=observation_route, observations=raw)
                db.execute('UPDATE attachments SET report=? WHERE session=? AND ref=?',
                           (json.dumps(receipt,ensure_ascii=False),session,ref))
        if is_active and not is_active():
            return {'ok':False,'error':'TURN_ALREADY_TAKEN_OVER'}
        receive_frame(report, frame, base, session, identity, owner)
    return notes_receipt(report, keep_frame_path=True) or report
