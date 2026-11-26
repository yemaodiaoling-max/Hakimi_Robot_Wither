"""QQ-visible link parsing and program delegation to the video companion."""
from hakimi import paths
from contextlib import closing
import hashlib
import html
import math
import os
import signal
import sqlite3
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, unquote

from hakimi.core.context_window import select_context
from hakimi.core.runtime_io import read_json_cached

ROOT = paths.base()
LINK = re.compile(r"(?<![A-Za-z0-9_.-])(?:https?://)?(?:(?:www\.|m\.)?bilibili\.com/video/(?:BV[A-Za-z0-9]{10}|av\d+)(?:\?[^\s\"<>\[\]]*)?|b23\.tv/[A-Za-z0-9]+)", re.I)
QQ_SHARE = re.compile(r"(?<![A-Za-z0-9_.-])(?:https?://)?m\.q\.qq\.com/a/s/[A-Za-z0-9]+", re.I)
BARE_BV = re.compile(r"(?<![A-Za-z0-9_])BV[A-Za-z0-9]{10}(?![A-Za-z0-9_])")


def _decode(text):
    for _ in range(3):
        decoded = unquote(html.unescape(text)).replace("\\/", "/")
        if decoded == text:break
        text = decoded
    return text


def _card_values(value):
    """Read actual link fields, never a thumbnail or title containing a BV ID."""
    root = ROOT.parent if ROOT.name == "official_test_runtime" else ROOT
    settings = read_json_cached(root/"video_tools_config.json", {})
    rules = settings.get("qq_miniapp", {})
    url_fields = {"qqdocurl", "qqdoc_url", "jumpurl", "jump_url", "url", "targeturl", "target_url", "href", "link", "path", "weburl"}
    payload_fields = {"data", "json", "payload", "content", "ark_data", "bytesdata", "bytes_data"}
    for fields, name in ((url_fields, "extra_url_fields"), (payload_fields, "extra_payload_fields")):
        fields.update(key.lower() for key in rules.get(name, []) if isinstance(key, str))
    budget = 256
    def walk(item, depth=0, text_ok=False):
        nonlocal budget
        budget -= 1
        if budget < 0 or depth > 8:return
        if isinstance(item, str):
            text = _decode(item[:32000])
            try:
                decoded, _ = json.JSONDecoder().raw_decode(text.lstrip())
            except ValueError:
                if text_ok:yield text
            else:
                yield from walk(decoded, depth+1, text_ok)
        elif isinstance(item, dict):
            if str(item.get("key", "")).strip("#").lower() in url_fields:
                yield from walk(item.get("value"), depth+1, True)
            for key, child in list(item.items())[:64]:
                key = str(key).lower()
                if key in {"bvid", "bv_id"} and re.fullmatch(r"BV[A-Za-z0-9]{10}", str(child)):
                    yield "https://www.bilibili.com/video/"+str(child)
                elif key == "path" and isinstance(child, str):
                    bvid = parse_qs(urlsplit(_decode(child)).query).get("bvid", [""])[0]
                    if re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid):yield "https://www.bilibili.com/video/"+bvid
                    yield from walk(child, depth+1, True)
                elif isinstance(child, (dict, list)) or key in url_fields | payload_fields:
                    yield from walk(child, depth+1, key in url_fields | payload_fields)
        elif isinstance(item, list):
            for child in item[:64]:yield from walk(child, depth+1)
    yield from walk(value, text_ok=True)


def find_url(text):
    structured = isinstance(text, (dict, list))
    if not structured:
        text = str(text)[:32000]
        cq = re.search(r"\[CQ:json,data=", text)
        if cq:
            text = text[cq.end():]
            structured = True
        else:structured = _decode(text).lstrip().startswith(("{", "[", '"')) and not text.startswith("[B站")
    values = _card_values(text) if structured else [_decode(text)]
    qq_share = None
    for value in values:
        match = LINK.search(value)
        if match:return _canonical(match[0])
        share = QQ_SHARE.search(value)
        if share:qq_share = _canonical(share[0])
        if not structured:
            bare = BARE_BV.search(value)
            if bare:return "https://www.bilibili.com/video/"+bare[0]
    if qq_share and structured and not re.search(r"哔哩哔哩|bilibili|1109937557", str(text)[:32000], re.I):return None
    return qq_share


def _canonical(url):
    url = url.rstrip("，。；,;)")
    if not url.startswith("http"):url = "https://"+url
    parsed = urlsplit(url)
    page = parse_qs(parsed.query).get("p", [""])[0]
    return "https://"+parsed.netloc+parsed.path+("?p="+page if page.isdecimal() else "")


def miniapp_text(value):
    url = find_url(value)
    return "[B站小程序视频："+url+"]" if url else ""


def _resolve_qq_share(url, *, timeout=6):
    """Only on demand; no browser/cookies, and no fetch during message receipt."""
    if not QQ_SHARE.fullmatch(url):return url
    import urllib.request
    class VideoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            if urlsplit(newurl).hostname in {"m.q.qq.com", "b23.tv", "bilibili.com", "www.bilibili.com", "m.bilibili.com"}:
                return super().redirect_request(request, fp, code, msg, headers, newurl)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.build_opener(VideoRedirect()).open(request, timeout=timeout) as response:
            redirected = find_url(response.geturl())
            if redirected and not QQ_SHARE.fullmatch(redirected):return redirected
            body = response.read(256*1024).decode("utf-8", errors="replace")
        found = LINK.search(_decode(body))
        if found:return _canonical(found[0])
    except OSError as exc:
        raise VideoError("QQ_MINIAPP_LINK_UNAVAILABLE") from exc
    raise VideoError("QQ_MINIAPP_VIDEO_LINK_MISSING")


def normalize_cards(raw):
    def replace(match):
        if "com.tencent.multimsg" in match[0]:return match[0]
        url = find_url(match[0])
        label = "B站小程序视频" if "miniapp" in match[0].lower() else "B站视频"
        return "["+label+"："+url+"]" if url else match[0]
    return re.sub(r"\[CQ:(?:json|xml|markdown),[^\]]+\]", replace, raw)


def candidate(payload):
    rows, target = select_context(payload)
    url = find_url(target.get("text", ""))
    if url:return url, target
    if re.search(r"视频|链接|上面|刚才.*(?:发|看)|看看", str(target.get("text", ""))):
        for row in reversed(rows):
            url = find_url(row.get("text", ""))
            if url:return url, row
    return None, target


FEEDBACK_REVISION = 'qq-contextual-video-notes-v5'


def _feedback_text(value, limit):
    if not isinstance(value, str):
        return ''
    value = value[:5000]
    value = re.sub(r'\[(?:CQ:[^\]]+|QQ视频[^\]]*|B站(?:小程序)?视频[^\]]*)\]', ' ', value)
    value = re.sub(r'(?i)(?:https?://|//|www\.)[^\s<>\]\[“”"\']+', ' ', value)
    for pattern in (LINK, QQ_SHARE, BARE_BV):
        value = pattern.sub(' ', value)
    value = re.sub(
        r'(?i)\b(?:[a-z_]*openid|access[_-]?token|auth[_-]?token|token|authorization|'
        r'rkey|sign(?:ature)?|api[_-]?key|secret|password|cookie)["\']?\s*[:=：]\s*'
        r'(?:bearer\s+)?[^\s,;，；<>\]\}]+', ' ', value)
    value = re.sub(r'(?i)\b[a-f0-9]{32,}\b|\bBearer\s+[^\s,;]+', ' ', value)
    value = ' '.join(value.split())[:limit]
    return value.encode('utf-8')[:limit * 2].decode('utf-8', errors='ignore')


def _qq_excerpt(row, *, quoted=False):
    result = {'speaker': _feedback_text(row.get('speaker'), 60) or 'QQ未标注'}
    if body := _feedback_text(row.get('text'), 120 if quoted else 240):
        result['text'] = body
    if row.get('same_text_as') == '哈基米在本群的已发送回执':
        result['same_text_as'] = '哈基米在本群的已发送回执'
    attachments = row.get('attachments')
    if isinstance(attachments, list):
        item_labels = [_feedback_text(label, 100) for label in attachments[:2]]
        if any(item_labels):
            result['attachments'] = [label for label in item_labels if label]
    quotes = row.get('quotes')
    if not quoted and isinstance(quotes, list):
        result['quotes'] = [_qq_excerpt(item, quoted=True) for item in quotes[:2] if isinstance(item, dict)]
    return result


def feedback_input(payload, source=None):
    """Keep the selected turn's visible messages and QQ-attached excerpt distinct."""
    rows, target = select_context(payload)
    session = payload.get('session_key')
    ids = {str(payload[key]) for key in ('trigger_id', 'message_id') if payload.get(key)}
    trigger = next((row for row in reversed(rows) if row.get('user_id') != 'SELF'
                    and str(row.get('message_id')) in ids), target)
    if trigger.get('user_id') == 'SELF' or trigger.get('session_key', session) != session:
        trigger = {}
    question = _feedback_text(trigger.get('text'), 1200)
    request = dict(question=question or '请挑选视频中与这轮对话相关的画面和声音素材。', context=[])
    if not isinstance(session, str) or not session.startswith(('group:', 'private:')) or not trigger:
        return request

    group = session.startswith('group:')
    trigger_id = str(trigger.get('message_id') or '')
    reply_id = str(trigger.get('reply_to') or '')
    source_id = str(source.get('message_id') or '') if source else ''
    if source and source.get('session_key', session) != session:
        source_id = ''
    index = rows.index(trigger)
    earlier = rows[:index]
    at = trigger.get('received_at')
    def recent(row):
        received = row.get('received_at')
        return type(at) not in (int, float) or type(received) not in (int, float) or 0 <= at - received <= 600

    related_ids = {mid for mid in (reply_id, source_id) if mid and mid != trigger_id}
    required = [row for row in rows if row is trigger or str(row.get('message_id')) in related_ids]
    nearby = []
    if group:
        nearby = [row for row in earlier if row not in required and recent(row)]
    else:
        nearby = [row for row in earlier if row not in required and recent(row)
                  and row.get('user_id') in (trigger.get('user_id'), 'SELF')][-2:]
    nearby = nearby[-(12 - len(required)):]
    messages = [row for row in rows if row in required or row in nearby]
    positions = {str(row['message_id']): position for position, row in enumerate(messages, 1)
                 if row.get('message_id')}
    author_labels = {}
    for position, row in enumerate(messages, 1):
        own = row.get('user_id') == 'SELF'
        if own:
            speaker = '哈基米（本群已发送）' if group else '哈基米（本私聊已发送）'
        else:
            author = row.get('user_id') or ('unknown', position)
            number = author_labels.setdefault(author, len(author_labels) + 1)
            nickname = _feedback_text(row.get('nickname'), 40)
            label = f'群友{number}' if group else f'对话者{number}'
            speaker = label + (f'（{nickname}）' if nickname else '')
        item = {'sequence': position, 'speaker': speaker}
        if row is trigger:
            item['current_question'] = True
        elif body := _feedback_text(row.get('text'), 280):
            item['text'] = body
        if source_id and str(row.get('message_id')) == source_id:
            item['video_source'] = True
        if replied := positions.get(str(row.get('reply_to') or '')):
            if replied < position:
                item['reply_to'] = replied
        if len(item) > 2 or str(row.get('message_id')) in related_ids:
            request['context'].append(item)

    excerpt = trigger.get('qq_prior_messages')
    if group and isinstance(excerpt, list):
        request['qq_prior_messages'] = [_qq_excerpt(row) for row in excerpt[-10:] if isinstance(row, dict)]
    # Limit the actual UTF-8 JSON size, with room for the companion's video evidence.
    # Keep the question, the video share and its explicit reply target under pressure.
    required_positions = {index for index, row in enumerate(messages, 1) if row in required}
    optional = [item for item in request['context'] if item['sequence'] not in required_positions]
    while len(json.dumps(request, ensure_ascii=False).encode('utf-8')) > 12 * 1024:
        if optional:
            removed = optional.pop(0)
            request['context'].remove(removed)
            for item in request['context']:
                if item.get('reply_to') == removed['sequence']:
                    item.pop('reply_to')
        else:
            request['qq_prior_messages'].pop(0)
    return request


def receive_frame(report, output, base, session, request_id, owner=None):
    """Promote a validated worker file to the current session and turn."""
    feedback = report.get('feedback')
    shot = feedback.get('screenshot') if isinstance(feedback, dict) else None
    if not isinstance(shot, dict):
        return
    feedback.pop('screenshot', None)
    at = shot.get('timestamp')
    fact = next((item for item in report.get('evidence', []) if isinstance(item, dict)
                 and item.get('id') == shot.get('evidence_id')), {})
    start, end = fact.get('media_start'), fact.get('media_end')
    if (not isinstance(session, str) or not re.fullmatch(r'(?:group|private):[1-9][0-9]*', session)
            or not re.fullmatch(r'[a-f0-9]{64}', request_id) or shot.get('id') != 'F1'
            or type(at) not in (int, float) or not math.isfinite(at) or at < 0
            or fact.get('modality') not in ('video', 'mixed') or type(start) not in (int, float)
            or type(end) not in (int, float) or not start - .001 <= at <= end + .001
            or shot.get('evidence_id') not in feedback.get('evidence_ids', [])):
        return
    from hakimi.chat.image_permissions import VIDEO_FRAME_TTL, register_video_frame
    from PIL import Image
    root = Path(base).resolve() / 'state' / 'img_cache'
    try:
        if root.resolve() != root or root.is_symlink():
            return
        root.mkdir(parents=True, exist_ok=True)
        path = root / ('img_' + session.replace(':', '_') + '_video_' + request_id + '.jpg')
        if output.is_file() and not output.is_symlink():
            if not 16 <= output.stat().st_size <= 384 * 1024:
                return
            with Image.open(output) as image:
                if image.format != 'JPEG' or not 1 <= min(image.size) <= max(image.size) <= 640:
                    return
                image.load()
            with tempfile.NamedTemporaryFile(dir=root, suffix='.tmp', delete=False) as stream:
                staging = Path(stream.name)
                try:
                    stream.write(output.read_bytes())
                    stream.close()
                    staging.replace(path)
                finally:
                    staging.unlink(missing_ok=True)
        if path.is_symlink() or not path.is_file() or not 0 <= time.time() - path.stat().st_mtime < VIDEO_FRAME_TTL:
            return
        register_video_frame(session, owner or 'video:' + request_id, path, Path(base).resolve())
        feedback['screenshot'] = {'id': 'F1', 'timestamp': at, 'evidence_id': shot['evidence_id']}
        report['_frame_path'] = str(path)
    except (OSError, ValueError, sqlite3.Error, Image.DecompressionBombError):
        return


class VideoError(RuntimeError):
    pass


def prepare(payload, base=ROOT, *, timeout_seconds=180, is_active=None, source=None, owner=None):
    """Program delegation to the companion's actual A/V viewing, never another QQ model."""
    from hakimi.chat.turn_text import network_allowed
    if not network_allowed(payload,base):return None
    deadline = time.monotonic()+min(180,float(timeout_seconds))
    url, source = (find_url(source.get('text', '')), source) if source is not None else candidate(payload)
    if not url:
        return None
    if deadline-time.monotonic() < 2:
        return {"url":url,"error":"QQ_VIDEO_TIMEOUT"}
    if is_active is not None and not is_active():
        return {"url":url,"error":"TURN_ALREADY_TAKEN_OVER"}
    try:
        url = _resolve_qq_share(url,timeout=min(6,max(.1,deadline-time.monotonic()-1)))
    except VideoError as exc:
        return {"url":url,"error":"QQ_VIDEO_TIMEOUT" if time.monotonic() >= deadline else str(exc)}
    root = Path(base).resolve()
    root = root.parent if root.name == "official_test_runtime" else root
    settings = read_json_cached(root/"video_tools_config.json", {})
    config = settings.get("companion_config", "/opt/hakimi/bili_companion/deploy/watcher.json")
    companion_source = settings.get("companion_source", "/opt/hakimi/bili_companion/src")
    feedback = feedback_input(payload, source)
    identity = json.dumps([FEEDBACK_REVISION,payload["session_key"],str(source.get("message_id") or ""),
        str(payload.get("trigger_id") or payload.get("message_id") or payload.get("turn_id") or ""),url,feedback],ensure_ascii=False)
    request_id = hashlib.sha256(identity.encode()).hexdigest()
    timeout = deadline-time.monotonic()
    if timeout < 2:
        return {"url":url,"error":"QQ_VIDEO_TIMEOUT"}
    command = [sys.executable,"-m","bili_companion.runtime.qq_request","--config",str(config),
               "--request-id",request_id,"--url",url,"--timeout",str(max(1,timeout-1)), '--feedback-stdin']
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = str(companion_source)+(os.pathsep+child_env["PYTHONPATH"]
        if child_env.get("PYTHONPATH") else "")
    options = {"creationflags":getattr(subprocess,"CREATE_NO_WINDOW",0),"env":child_env}
    if os.name != "nt":
        options["start_new_session"] = True
    with tempfile.TemporaryDirectory(prefix='qq-video-result-') as directory:
        frame = Path(directory) / 'frame.jpg'
        command += ['--screenshot-path', str(frame)]
        report = _view_process(command, options, feedback, timeout, is_active, config, request_id, url)
        receive_frame(report, frame, base, payload['session_key'], request_id, owner)
        return report


def _view_process(command, options, feedback, timeout, is_active, config, request_id, url):
    try:
        process = subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,**options)
        try:
            stdout,_ = process.communicate(input=json.dumps(feedback, ensure_ascii=False).encode('utf-8'), timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(process.pid,signal.SIGTERM)
            else:
                process.terminate()
            try:
                stdout,_ = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    os.killpg(process.pid,signal.SIGKILL)
                else:
                    process.kill()
                stdout,_ = process.communicate()
            try:
                partial = json.loads(stdout)
            except (ValueError,TypeError):
                partial = None
            if not isinstance(partial,dict) or not partial.get('coverage'):
                partial = _saved_viewing_result(config,request_id)
            if not partial:
                return {"url":url,"request_id":request_id,"error":"QQ_VIDEO_TIMEOUT"}
            if is_active is not None and not is_active():
                return {"url":url,"request_id":request_id,"error":"TURN_ALREADY_TAKEN_OVER"}
            return partial
        if process.returncode or len(stdout)>1024*1024:
            raise ValueError("companion_failure")
        report = json.loads(stdout)
        if not isinstance(report,dict):
            raise ValueError("companion_invalid_result")
        if is_active is not None and not is_active():
            return {"url":url,"request_id":request_id,"error":"TURN_ALREADY_TAKEN_OVER"}
        return report
    except (OSError,ValueError):
        return {"url":url,"request_id":request_id,"error":"QQ_VIDEO_SERVICE_UNAVAILABLE"}


def _saved_viewing_result(config,request_id):
    """Completed observations survive a hard deadline; never restart inference."""
    try:
        settings=json.loads(Path(config).read_text(encoding='utf-8'))
        path=Path(settings['state_dir'])/'qq_requests.sqlite3'
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            row=db.execute('SELECT result FROM qq_requests WHERE id=?',(request_id,)).fetchone()
        report=json.loads(row[0]) if row and row[0] else None
        if isinstance(report,dict) and report.get('coverage'):
            return dict(report,status='partial',warning='QQ_VIDEO_TIMEOUT')
    except (OSError,ValueError,KeyError,sqlite3.Error):pass
    return None


def notes_receipt(report, *, keep_frame_path=False):
    """Expose the contextual notes and optional frame, while keeping raw observations in the audit."""
    feedback = report.get('feedback')
    if not isinstance(feedback, dict) or not isinstance(feedback.get('video_notes'), str):
        return None
    result = {key: report[key] for key in ('ok', 'status', 'title', 'duration', 'coverage_status',
                                          'error', 'warning') if key in report}
    result['feedback'] = {key: feedback[key] for key in ('status', 'video_notes', 'screenshot', 'error')
                          if key in feedback}
    if keep_frame_path and '_frame_path' in report and 'screenshot' in result['feedback']:
        result['_frame_path'] = report['_frame_path']
    return result


def brief(report):
    if not report:
        return None
    compact = notes_receipt(report)
    if compact is not None:
        return compact
    # Older reports and configurations without Sol retain the timeline receipt.
    keys = ("feedback","overview","chapters","overview_evidence_ids","coverage_status","coverage_fraction","unobserved_intervals",
            "view_mode","processed_seconds","elapsed_seconds","synthesis_status","planned_segments",
            "url","request_id","source","funding","status","bvid","part","title","duration",
            "error","warning","error_type","error_detail","error_features","error_stage","http_status","coverage","sampled_intervals","summaries","missing_modalities",
            "memory_saved","limitations","cached","observations_cached")
    result = {key:report[key] for key in keys if key in report}
    result["evidence"] = [{key:item[key] for key in
        ("id","text","media_start","media_end","modality","attribution","confidence") if key in item}
        for item in report.get("evidence",[])]
    return result


def images(report, payload, base):
    frames = selected_frames(['F1'], report, payload.get('session_key'), base)
    if not frames:
        return []
    from hakimi.models.fallback_media import _encode_image
    image = _encode_image(Path(frames[0]['path']), 'F1', '')
    return [image] if image else []


def selected_frames(ids, report, session, base):
    path = (report or {}).get('_frame_path')
    feedback = (report or {}).get('feedback')
    shot = feedback.get('screenshot') if isinstance(feedback, dict) else None
    if 'F1' not in (ids or ()) or not isinstance(path, str) or not shot:
        return []
    from hakimi.chat.image_permissions import allowed_images_for_session
    if path in allowed_images_for_session(session, Path(base).resolve()):
        return [dict(shot, path=path)]
    return []


def inspect_for_primary(payload, base, is_active=None, *, timeout_seconds=180, source=None, owner=None):
    report = prepare(payload,base,is_active=is_active,timeout_seconds=timeout_seconds,source=source,owner=owner)
    result = brief(report)
    if result is not None:
        frames = selected_frames(['F1'], report, payload.get('session_key'), base)
        if frames:
            result['key_frames'] = frames
    return result
