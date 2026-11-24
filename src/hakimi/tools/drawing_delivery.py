"""Generate one picture per QQ request and hand it to the existing outbox."""

import json
import os
import re
import sqlite3
import time
from contextlib import closing, contextmanager, suppress
from pathlib import Path

from hakimi.bridge.interaction_policy import blocked_outgoing_topic
from hakimi.core.runtime_io import atomic_replace
from hakimi.tools.drawing_intent import BRIEF_LIMIT, TOOL, allows_generation, request_id, requested
from hakimi.transport.file_delivery import directory, from_segment, resolve

IMAGE_EXTENSION = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}
_PICTURE = re.compile(r'\[CQ:file,name=_drawing_([a-f0-9]{64})\.(?:png|jpg|webp),')


@contextmanager
def receipts(base):
    path = Path(base) / 'state/drawing_receipts.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=3)) as db, db:
        db.execute('''CREATE TABLE IF NOT EXISTS drawings (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, reply_id TEXT,
                    at REAL NOT NULL)''')
        yield db


def _record(base, drawing_id, status, reply_id=None):
    with receipts(base) as db:
        db.execute("""UPDATE drawings SET status=?, reply_id=COALESCE(?,reply_id), at=?
                     WHERE id=? AND status IN ('generating','submitting')""",
                   (status, reply_id, time.time(), drawing_id))


def drawing_in(segments):
    if isinstance(segments, list) and len(segments) == 1 and isinstance(segments[0], str):
        match = _PICTURE.match(segments[0])
        return match[1] if match else None
    return None


def delivery_finished(base, drawing_id, reply_id, status):
    # Delivery can finish immediately after the append, before its caller can
    # save 'queued'. In both orders the transport's outcome is authoritative.
    with receipts(base) as db:
        db.execute("""UPDATE drawings SET status=?, reply_id=?, at=? WHERE id=?
                     AND status IN ('submitting','queued') AND (reply_id IS NULL OR reply_id=?)""",
                   (status, reply_id, time.time(), drawing_id, reply_id))


def onebot_image(base, session, segment):
    """OneBot uses a native picture segment; official QQ consumes the stamped file."""
    if not _PICTURE.match(segment):
        return segment
    path, kind = from_segment(segment, session, base)
    if kind != 1:
        raise ValueError('drawing_file_is_not_an_image')
    return f'[CQ:image,file={path.as_uri()},sub_type=0]'


def _official_route_available(base, session):
    if not (base / 'official_lane_config.json').is_file():
        return True  # Ordinary OneBot sessions use the live websocket in the outbox worker.
    db_path = base / 'state/official_routes.sqlite3'
    kind, local_id = session.split(':', 1)
    try:
        with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (kind + '_routes',)).fetchone()
        routes = json.loads(row[0]) if row else {}
        return any(route.get('active', True) and str(route.get('local_id')) == local_id
                   for route in routes.values() if isinstance(route, dict))
    except (sqlite3.Error, OSError, ValueError, AttributeError):
        return False


def _stage(base, session, drawing_id, image):
    extension = IMAGE_EXTENSION.get(image.mime_type)
    if extension is None:
        raise ValueError('unsupported_drawing_format')
    root = directory(session, base)
    root.mkdir(parents=True, exist_ok=True)
    # Old generated files have outlived every ordinary outbox delivery window.
    # Retain receipts so an old source cannot be charged a second time.
    cutoff = time.time() - 7 * 86400
    for file in root.glob('_drawing_*'):
        with suppress(OSError):
            if not file.is_symlink() and file.is_file() and file.stat().st_mtime < cutoff:
                file.unlink(missing_ok=True)
    path = root / ('_drawing_' + drawing_id + extension)
    temp = path.with_suffix('.tmp')
    if path.is_symlink() or temp.is_symlink():
        raise ValueError('drawing_path_redirected')
    with temp.open('xb') as stream:
        stream.write(image.data)
        stream.flush()
        os.fsync(stream.fileno())
    atomic_replace(temp, path)
    _, kind, stamp = resolve(path.name, session, base)
    if kind != 1:
        raise ValueError('drawing_file_is_not_an_image')
    return f'[CQ:file,name={path.name},stamp={stamp},mode=auto]'


def draw(base, coordinator, token, provider, arguments, timeout=None):
    from hakimi.tools.drawing import DrawingError, generate, public_settings
    from hakimi.tools.math_render import log_tool_call

    base = Path(base)
    if not coordinator.check_token(token, provider):
        return {'error': 'turn_expired'}
    state = coordinator.snapshot()
    if state.get('owner_token') != token or state.get('reply_id'):
        return {'error': 'turn_expired_or_already_replied'}
    payload = state['payload']
    target = requested(payload)
    if target is None or not allows_generation(payload, target):
        return {'error': 'drawing_not_requested'}
    creative_brief = arguments.get('brief') if isinstance(arguments, dict) and set(arguments) == {'brief'} else None
    if (not isinstance(creative_brief, str) or not 1 <= len(creative_brief.strip()) <= BRIEF_LIMIT
            or creative_brief.strip() == str(target.get('text', '')).strip()
            or re.search(r'\[CQ:|https?://|[\x00-\x1f]', creative_brief, re.I)):
        return {'error': 'invalid_drawing_brief', 'message': '请用一句简短的话写画面的构思。'}
    if blocked_outgoing_topic(str(target.get('text', ''))) or blocked_outgoing_topic(creative_brief):
        return {'error': 'ignored_topic'}
    try:
        config = public_settings(base)
    except DrawingError as exc:
        return {'error': exc.code, 'message': str(exc)}
    if not config.get('enabled') or not config.get('has_api_key'):
        return {'error': 'drawing_unavailable', 'message': '画图服务暂时不可用。'}
    session = payload['session_key']
    if not _official_route_available(base, session):
        return {'error': 'drawing_route_unavailable', 'message': '这个会话现在发不了画。'}
    remaining = min(90, state['deadline'] - time.time() - 2, timeout if timeout is not None else 90)
    if remaining < 1:
        return {'error': 'drawing_deadline', 'message': '这轮来不及画图了。'}
    drawing_id = request_id(session, target)
    try:
        with receipts(base) as db:
            inserted = db.execute("INSERT OR IGNORE INTO drawings VALUES (?,'generating',NULL,?)",
                                  (drawing_id, time.time())).rowcount
            if not inserted:
                status, reply_id = db.execute('SELECT status,reply_id FROM drawings WHERE id=?', (drawing_id,)).fetchone()
                if status in {'queued', 'delivered'}:
                    return {'ok': True, 'status': status, 'reply_id': reply_id}
                return {'error': 'drawing_already_attempted', 'status': status}
        if not coordinator.check_token(token, provider):
            _record(base, drawing_id, 'stale_owner')
            return {'error': 'turn_expired'}
        try:
            image = generate(base, creative_brief.strip(), timeout=remaining)
        except DrawingError as exc:
            _record(base, drawing_id, 'generation_failed')
            log_tool_call(base, TOOL, 'generation_failed', provider=provider, error=exc.code)
            return {'error': exc.code, 'message': str(exc)}
        if not coordinator.check_token(token, provider):
            _record(base, drawing_id, 'stale_owner')
            return {'error': 'turn_expired'}
        try:
            segment = _stage(base, session, drawing_id, image)
        except (OSError, ValueError):
            _record(base, drawing_id, 'image_prepare_failed')
            return {'error': 'drawing_file_error', 'message': '画好了，但这张图没能提交给QQ。'}
        _record(base, drawing_id, 'submitting')
        reply = coordinator.submit_reply(token, [segment], provider, reply_to=str(target['message_id']), generated_drawing=True)
        if not reply.get('accepted'):
            _record(base, drawing_id, 'submit_rejected')
            return {'error': reply.get('reason', 'drawing_submit_rejected'), 'message': '这张图没能提交给QQ。'}
    except (OSError, sqlite3.Error):
        log_tool_call(base, TOOL, 'receipt_or_outbox_error', provider=provider)
        return {'error': 'drawing_submission_unconfirmed', 'message': '这张图的发送结果暂时无法确认。'}

    try:
        _record(base, drawing_id, 'queued', reply['reply_id'])
    except (OSError, sqlite3.Error):
        log_tool_call(base, TOOL, 'receipt_error_after_submit', provider=provider)
    log_tool_call(base, TOOL, 'queued', provider=provider)
    return {'ok': True, 'status': 'queued', 'reply_id': reply['reply_id']}


def failure_text(result):
    """Report known pre-queue failures without making a second send on uncertainty."""
    if result.get('error') in {'drawing_submission_unconfirmed', 'invalid_drawing_brief'}:
        return None
    return result.get('message')
