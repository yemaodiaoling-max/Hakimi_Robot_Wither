"""Shared QQ provider duty and content-only recovery checks on the private Vertex gateway."""
from contextlib import closing
from http.client import HTTPException
import json
from pathlib import Path
import sqlite3
import ssl
import threading
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

def _initial():
    return dict(mode='vertex', epoch=0, reason=None, count=0)


def _state(db):
    row = db.execute('SELECT mode, epoch, reason, count FROM duty WHERE id=1').fetchone()
    return dict(row) if row else _initial()


def snapshot(root):
    path = Path(root) / 'state' / 'hydrogen_duty.sqlite3'
    if not path.exists():
        return _initial()
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        # Another lane may have just created the file and not yet committed its first record.
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='duty'").fetchone() is None:
            return _initial()
        return _state(db)


def record(root, epoch, error_key):
    """Count consecutive equivalent failures across lanes. Old generations cannot change duty."""
    current = snapshot(root)
    if current['epoch'] != epoch or (error_key is None and current['mode'] == 'vertex' and not current['count']):
        return current
    path = Path(root) / 'state' / 'hydrogen_duty.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS duty (id INTEGER PRIMARY KEY, mode TEXT, epoch INTEGER, reason TEXT, count INTEGER)')
        current = _state(db)
        if current['epoch'] == epoch:
            if error_key is None:
                generation = epoch + int(current['mode'] == 'video_hydrogen')
                current = dict(mode='vertex', epoch=generation, reason=None, count=0)
            elif current['mode'] == 'vertex':
                count = current['count'] + 1 if current['reason'] == error_key else 1
                current.update(reason=error_key, count=count)
                if count >= 3:
                    current.update(mode='video_hydrogen', epoch=epoch+1)
            db.execute('INSERT OR REPLACE INTO duty VALUES(1,:mode,:epoch,:reason,:count)', current)
        db.commit()
        return current


def classify_vertex_error(exc, detail):
    """Return an outage signature only for a failed primary provider request."""
    detail = detail or {}
    if detail.get('provider') or detail.get('block_reason') or str(exc) in {'model_tool_timeout', 'vertex_turn_ownership_lost'}:
        return None
    message = str(detail.get('message') or '').lower()
    if 'budget' in message or message in {'daily_request_limit', 'manual_pause'}:
        return None
    status = detail.get('http_status', getattr(exc, 'code', None))
    if status == 403 and (message == 'vertex_official_spend_cap_breached' or
                          message.startswith('spend cap breached for project:')):
        return 'vertex_official_spend_cap_breached'
    if status == 429:
        return 'gateway_busy' if message == 'gateway_busy' else 'vertex_quota'
    if status in (408, 504) or str(exc) in {'model_response_timeout', 'vertex_timeout_before_headers'}:
        return 'vertex_timeout'
    if status in (401, 403) or isinstance(status, int) and 500 <= status <= 599:
        return 'vertex_http_' + str(status)
    if isinstance(exc, TimeoutError) or isinstance(getattr(exc, 'reason', None), TimeoutError):
        return 'vertex_timeout'
    if not isinstance(exc, HTTPError) and isinstance(exc, (URLError, ConnectionError, HTTPException)):
        return 'vertex_transport'
    if str(exc) == 'vertex_empty_response' or (str(exc) == 'vertex_incomplete_stream' and not detail.get('finish_reason')):
        return 'vertex_empty_response'
    return None


def recovery_text(event):
    """Extract the current QQ request without sending the generated persona or memory."""
    from hakimi.core.context_window import select_context

    if event.get('internal_review') or event.get('cold_topic_request'):
        return ''
    rows, target = select_context(event)
    window = event.get('listening_window') or {}
    current_ids = window.get('selected_message_ids', window.get('new_message_ids', []))
    if not current_ids:
        current_ids = list(event.get('pending_explicit_question_ids') or [])
        if target.get('message_id'):
            current_ids.append(target['message_id'])
    current_ids = {str(message_id) for message_id in current_ids}
    return '\n'.join(
        row['text'].strip() for row in rows
        if row.get('user_id') != 'SELF'
        and (str(row.get('message_id')) in current_ids or (not current_ids and row is target))
        and isinstance(row.get('text'), str) and row['text'].strip()
    )[:8000]


def _recovery_probe(root, epoch, env, event):
    from hakimi.models.vertex_route import GATEWAY_URL, SUPPORTED_MODELS, _NoRedirect
    try:
        model = env['AGY_LLM_GATEWAY_MODELS']
        if model not in SUPPORTED_MODELS or env['AGY_LLM_GATEWAY_URL'].rstrip('/') != GATEWAY_URL:
            return
        duty = snapshot(root)
        if duty['mode'] != 'video_hydrogen' or duty['epoch'] != epoch:
            return
        text = recovery_text(event)
        if not text:
            return
        body = {
            'contents': [{'role': 'user', 'parts': [{'text': text}]}],
            'generationConfig': {'maxOutputTokens': 1024, 'thinkingConfig': {'thinkingLevel': 'LOW'}},
        }
        req = Request(GATEWAY_URL + '/v1beta/models/' + model + ':generateContent',
                      data=json.dumps(body).encode(), headers={
                          'Authorization': 'Bearer ' + env['AGY_LLM_GATEWAY_API_KEY'],
                          'Content-Type': 'application/json'})
        tls = ssl.create_default_context(cafile=env['AGY_LLM_GATEWAY_CA_CERT'])
        opener = build_opener(ProxyHandler({}), HTTPSHandler(context=tls), _NoRedirect())
        with opener.open(req, timeout=190 if model == 'gemini-4-carbon' else 55) as response:
            body = response.read(65537)
            if response.status != 200 or len(body) > 65536:
                return
        candidate = json.loads(body)['candidates'][0]
        parts = candidate['content']['parts']
        if candidate.get('finishReason') == 'STOP' and not any('functionCall' in part for part in parts) and any(
            isinstance(part.get('text'), str) and part['text'].strip() and not part.get('thought')
            for part in parts
        ):
            record(root, epoch, None)
    except HTTPError as exc:
        exc.close()
    except (OSError, HTTPException, ValueError, KeyError, IndexError, TypeError, AttributeError, sqlite3.Error):
        # A probe never runs tools, delivers a reply, or claims to repair an unverified outage.
        return


def start_probe(root, epoch, env, event):
    probe = threading.Thread(target=_recovery_probe, args=(root, epoch, dict(env), dict(event)),
                             name='vertex-recovery', daemon=True)
    try:
        probe.start()
    except (OSError, RuntimeError):
        return
    return probe
