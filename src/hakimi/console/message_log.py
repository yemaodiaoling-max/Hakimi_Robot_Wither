"""Read recent QQ messages and their recorded scheduler and delivery decisions."""
import json
import math
import re
import sqlite3
import time
from collections import defaultdict, deque, OrderedDict
from threading import RLock
from contextlib import closing
from pathlib import Path

GATES = {
    "ready": "已满足唤醒条件", "insufficient_value": "本次参与评分未达到阈值",
    "no_actionable_input": "本次未发现需要接话的内容", "busy": "当时正在处理其他轮次",
    "waiting_window": "正在监听后续消息", "waiting_batch": "等待合并后续消息",
}
BASES = {
    "contextual_followup": "上下文续聊", "lexical": "话题关键词相关", "addressed_other": "消息指向其他人",
    "awaiting_refinement": "等待进一步判断", "dialogue_cooldown": "对话冷却中",
    "collecting_mention": "正在收集被提及后的消息", "review_empty": "复核未选出内容",
    "collecting_window": "收集监听窗口内的消息", "memory_bridge": "关联已有记忆",
    "ready_for_refinement": "可进一步判断", "image_followup": "图片续聊", "explicit_mention": "明确提及哈基米",
}
OUTCOMES = {"replied":"本轮已生成回复", "no_reply":"本轮决定不回复", "expired":"本轮已过期",
            "fallback_error":"备用回复出错", "review_failed":"本轮复核失败", "superseded":"该消息已由另一轮回复处理"}
TAKEOVERS = {"group_hourly_limit":"主通道触及群每小时限额", "primary_hard_timeout":"主通道超时",
             "primary_stalled":"主通道停滞", "reported:gateway_unavailable":"主通道网关不可用"}

def tail(path, limit):
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            offset = max(0, stream.tell() - 2_000_000)
            stream.seek(offset)
            if offset:
                stream.readline()
            lines = stream.read(2_000_000).splitlines()[-limit:]
    except FileNotFoundError:
        return []
    result = []
    for line in lines:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                result.append(value)
        except (ValueError, UnicodeError):
            continue
    return result

PHASES = {'queued_local':'已进入调度队列','reviewing':'正在复核上下文','context_ready':'上下文已收集完成',
          'published':'已提交生成任务','cancelled':'已取消', 'generation_error':'生成出错','send_error':'发送出错'}
REASONS = {'session_not_allowed':'会话未启用','autonomous_unavailable':'主动参与权限或冷却限制','cold_opportunity_expired':'冷场机会已失效','decision_not_ready':'复核未达到参与条件','window_expired':'监听窗口已过期','participation_score_low':'参与评分不足','participation_probability':'本次随机参与未命中','exploration_expired':'探索机会已失效','exploration_budget':'探索额度不足','image_probability':'本次图片随机参与未命中','empty_selection':'未选出可回复内容','user_reply_limit':'个人回复上限（半小时7轮）','quota':'模型额度不足','auth':'模型鉴权失败',
           'model_error':'模型请求失败','unavailable':'模型不可用'}

REASONS['source_already_answered'] = '这条消息已有回复'

def archived_messages(state, session='', scope='group'):
    """Read preserved chat originals by session; never substitute memory notes."""
    path=state/'memory.sqlite3'
    if not path.is_file():return []
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            columns={r[1] for r in db.execute('PRAGMA table_info(records)')}
            nickname='nickname' if 'nickname' in columns else "'' AS nickname"
            sessions=[session] if session else [r[0] for r in db.execute(
                "SELECT session FROM records WHERE kind='chat' AND state!='deleted' AND session LIKE ? GROUP BY session ORDER BY MAX(at) DESC LIMIT 100", (scope+':%',))]
            result=[]
            for group in sessions:
                for r in db.execute("SELECT origin,user_id,text,at,"+nickname+" FROM records "
                    "WHERE kind='chat' AND session=? AND state!='deleted' ORDER BY at DESC,id DESC LIMIT ?",(group,300 if session else 30)):
                    if r['user_id']=='SELF' or not str(r['origin'] or ''):continue
                    result.append(dict(session_key=group,message_id=r['origin'],user_id=r['user_id'],
                        nickname=r['nickname'],text=r['text'],received_at=r['at'],archived=True))
            return result
    except sqlite3.Error:return []

_TAIL_CACHE = OrderedDict()
_TAIL_LOCK = RLock()


def _reverse_lines(stream, end):
    """Yield lines newest first without reading beyond the captured file size."""
    buffer = b''
    position = end
    while position:
        size = min(position, 65536)
        position -= size
        stream.seek(position)
        parts = (stream.read(size) + buffer).split(b'\n')
        buffer = parts[0]
        for line in reversed(parts[1:]):
            if line:
                yield line
    if buffer:
        yield buffer


def _trailing_start(stream, end):
    """Find the final line boundary, including lines larger than one block."""
    position = end
    while position:
        size = min(position, 65536)
        position -= size
        stream.seek(position)
        block = stream.read(size)
        boundary = block.rfind(b'\n')
        if boundary >= 0:
            return position + boundary + 1
    return 0


def scoped_tail(path, limit, scope, session=''):
    """Cache complete lines; expose valid unterminated JSON without committing it.

    Keeping an EOF record outside the committed deque lets a later newline or
    partial-record continuation be consumed once, without duplicate messages.
    """
    import os

    key = (str(path), limit, scope, session)

    def matches(line):
        try:
            row = json.loads(line)
            name = row.get('session_key', '')
            if isinstance(name, str) and name.startswith(scope + ':'):
                if not session or name == session:
                    return row
        except (ValueError, UnicodeError, AttributeError):
            pass
        return None

    with _TAIL_LOCK:
        try:
            with path.open('rb') as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                cached = _TAIL_CACHE.get(key)
                unchanged = True
                if cached:
                    start = max(0, cached['offset'] - 64)
                    stream.seek(start)
                    unchanged = stream.read(cached['offset'] - start) == cached['anchor']
                reusable = (
                    cached and unchanged and cached['identity'] == identity
                    and stat.st_size >= cached['size']
                    and (stat.st_size > cached['size'] or stat.st_mtime_ns == cached['mtime'])
                )
                pending = b''
                if reusable:
                    rows = cached['rows']
                    stream.seek(cached['offset'])
                    while stream.tell() < stat.st_size:
                        line = stream.readline(stat.st_size - stream.tell())
                        if not line.endswith(b'\n'):
                            pending = line
                            break
                        row = matches(line)
                        if row is not None:
                            rows.append(row)
                        cached['offset'] = stream.tell()
                else:
                    offset = _trailing_start(stream, stat.st_size)
                    stream.seek(offset)
                    pending = stream.read(stat.st_size - offset)
                    newest = []
                    for line in _reverse_lines(stream, offset):
                        row = matches(line)
                        if row is not None:
                            newest.append(row)
                        if len(newest) >= limit:
                            break
                    rows = deque(reversed(newest), maxlen=limit)
                    cached = {'identity': identity, 'rows': rows, 'offset': offset}
                start = max(0, cached['offset'] - 64)
                stream.seek(start)
                cached.update(
                    size=stat.st_size,
                    mtime=stat.st_mtime_ns,
                    anchor=stream.read(cached['offset'] - start),
                )
                _TAIL_CACHE[key] = cached
                _TAIL_CACHE.move_to_end(key)
                while len(_TAIL_CACHE) > 64:
                    _TAIL_CACHE.popitem(last=False)
                result = list(rows)
                pending_row = matches(pending)
                if pending_row is not None:
                    result.append(pending_row)
                return result[-limit:]
        except FileNotFoundError:
            _TAIL_CACHE.pop(key, None)
            return []


def recent_messages(base, session='', scope=None):
    if session and not re.fullmatch(r'(?:group|private):[1-9][0-9]{0,19}',session):
        raise ValueError('invalid session')
    scope = scope or (session.split(':', 1)[0] if session else 'group')
    if scope not in ('group', 'private') or (session and not session.startswith(scope+':')):
        raise ValueError('invalid scope')
    state = Path(base) / "state"
    incoming = scoped_tail(state/"chat_history.jsonl", 300, scope, session)
    if session:incoming=[r for r in incoming if r.get('session_key')==session]
    originals={(r.get('session_key'),str(r.get('message_id'))):r for r in archived_messages(state,session,scope)}
    originals.update({(r.get('session_key'),str(r.get('message_id'))):r for r in incoming})
    from hakimi.voice.store import recent_status as voice_status
    speech_rows=voice_status(base,session)
    from hakimi.voice.store import console_previews
    voice_previews=console_previews(base,session)
    speech_status={(r['session'],str(r['mid'])):r for r in speech_rows}
    speech_rows=[r for r in speech_rows if r.get('session','').startswith(scope+':') and (not session or r.get('session')==session)]
    for r in speech_rows:
        originals.setdefault((r['session'],str(r['mid'])),dict(session_key=r['session'],message_id=r['mid'],user_id=r['sender'],nickname=r['nickname'] or '语音',received_at=r['at'],text='[语音消息；原文仅本会话可读]'))
    for key,row in originals.items():
        if key[0]==session and key[1] in voice_previews:
            row=dict(row,text=str(row.get('text',''))+'\n[自动转写，可能有误] '+voice_previews[key[1]])
            originals[key]=row
    incoming=sorted(originals.values(),key=lambda r:r.get('received_at',0),reverse=True)
    if session:incoming=incoming[:300]
    names = {}
    if scope == 'private':
        for row in reversed(incoming):
            key=row.get('session_key','')
            if key.startswith('private:'):
                names[key]=str(row.get('nickname') or ('私聊 '+key.split(':',1)[1]))
    queued = scoped_tail(state/"outbox.jsonl", 1200, scope, session)
    sent = scoped_tail(state/"sent_messages.jsonl", 2000, scope, session)
    if scope == 'group':
        from hakimi.console.group_catalog import group_metadata
        keys={r.get('session_key') for r in [*incoming,*queued,*sent] if r.get('session_key')}
        names.update({key:value['label'] for key,value in group_metadata(base,keys).items()})
    events = tail(state/"turn_events.jsonl", 1200)
    lifecycle = tail(state/'message_lifecycle.jsonl',2500)
    phases = defaultdict(list)
    for r in lifecycle:
        for mid in r.get('message_ids',[]):phases[r.get('session_key'),str(mid)].append(r)
    active={}
    for path in [state/'turn_state.json',*(state/'turn_lanes').glob('*.json')]:
        try:
            r=json.loads(path.read_text());payload=r.get('payload',{})
            if r.get('status') in ('queued','primary_active','fallback_active'):
                active[payload.get('session_key'),r.get('trigger_id')]=r
        except (OSError,ValueError):pass
    decisions = {}
    for r in tail(state/"conversation_decisions.jsonl", 2000):
        decisions[(r.get("session_key"), str(r.get("last_message_id")))] = r
    def turn(r):
        return r.get("session_key"), r.get("trigger_id")
    boxes = {turn(r):r for r in queued}
    runs = {turn(r):r for r in events}
    receipts = {turn(r):r for r in sent}
    # The trial's outbox "model" is the configured budget route. Only the
    # successful turn's final inference request identifies the reply model.
    from hakimi.models.vertex_route import root_for
    models = {}
    for audit in tail(root_for(base)/'state/task_model_routing.jsonl', 4000):
        requests = audit.get('actual_requests')
        if (not audit.get('error') and audit.get('session_key') and audit.get('trigger_id')
                and isinstance(requests, list) and requests and isinstance(requests[-1], dict)):
            name = requests[-1].get('model')
            if isinstance(name, str) and name.strip():
                models[turn(audit)] = name.strip()

    def reply_model(key):
        if not (key in boxes or key in receipts):
            return None
        provider = receipts.get(key, {}).get('provider') or boxes.get(key, {}).get('provider')
        return models.get(key) if provider == 'gemini' else None
    links = defaultdict(list)
    def link(session, message, key):
        if session and message and key[1] and key not in links[session,str(message)]:
            links[session,str(message)].append(key)
    for r in lifecycle:
        for mid in r.get('message_ids',[]):link(r.get('session_key'),mid,turn(r))
    for key,r in active.items():
        payload=r.get('payload',{})
        for mid in payload.get('pending_explicit_question_ids',[]) or [payload.get('reply_target_message_id')]:
            link(key[0],mid,key)
    for r in queued:
        link(r.get("session_key"),r.get("reply_target_id"),turn(r))
    dbpath = state/"official_routes.sqlite3"
    if dbpath.is_file():
        with closing(sqlite3.connect(dbpath.resolve().as_uri()+"?mode=ro",uri=True)) as db:
            ids = list({str(r["message_id"]) for r in incoming if r.get("message_id")})
            routes = dict(db.execute("SELECT trigger,source FROM turns WHERE source IN ("+
                                     ",".join("?" for _ in ids)+")",ids)) if ids else {}
        for key in (boxes.keys() | runs.keys() | receipts.keys()):
            link(key[0],routes.get(key[1]),key)
    receipt_counts=defaultdict(int)
    for r in sent:receipt_counts[turn(r)]+=1
    def explain(key):
        box,run,receipt = boxes.get(key,{}),runs.get(key,{}),receipts.get(key,{})
        outcome=run.get("outcome")
        count=receipt_counts[key];expected=len(box.get('segments',[]))
        status=(f'已发送 {count}/{expected} 条' if receipt and expected>count else '已发送') if receipt else '已入队，尚未匹配发送回执' if box else OUTCOMES.get(outcome,outcome or '')
        if not status and key in active:
            status={'queued':'等待模型领取','primary_active':'主通道正在生成','fallback_active':'备用通道正在生成'}[active[key]['status']]
        if not status:status='已提交任务，尚未记录最终结果'
        wake=box.get("wake_mode") or run.get("wake_mode")
        provider=receipt.get("provider") or box.get("provider") or run.get("provider")
        model = reply_model(key)
        actor = '回复模型：' + model if model else ('处理方：' + provider if provider else '')
        if not model and provider == 'gemini' and (receipt or box):
            actor += '（具体模型未确认）'
        reason=run.get("takeover_reason")
        return " · ".join(x for x in (status,wake,("接管原因："+TAKEOVERS.get(reason,reason)) if reason else "",
                                      actor) if x)
    rows=[]
    for kind,source,stamp in (("received",incoming,"received_at"),("queued",queued[-300:],"created_at"),("sent",sent[-300:],"at")):
        for r in source:
            try:
                at=float(r[stamp])
                if not math.isfinite(at):continue
            except (TypeError,ValueError,KeyError):continue
            row_session=r.get("session_key")
            if not isinstance(row_session,str) or not row_session.startswith(scope+':'):continue
            if session and row_session!=session:continue
            message=(row_session,str(r.get("message_id")))
            notes=[]
            if kind=="received":
                speech=speech_status.get(message)
                if speech:
                    labels={'queued':'等待本地识别','processing':'正在本地识别','ready':'识别完成，等待归档','delivered':'语音处理完成'}
                    label=labels.get(speech['status'],'语音处理中')
                    if speech['error']:label={'SOURCE_EXPIRED':'语音等待超过处理窗口，未识别','ADMISSION_CHANGED':'会话设置已变，未保存转写','VOICE_DISABLED':'语音识别未启用','VOICE_QUEUE_FULL':'语音队列已满，保留附带文字','NO_CLEAR_SPEECH':'未听清语音，未写入文字记忆','AUDIO_PROCESSING_FAILED':'音频处理失败，未写入文字记忆','TRANSIENT_RETRY':'临时故障，等待有界恢复','SOURCE_FORGOTTEN':'原文已删除或过期'}.get(speech['error'],'语音处理未完成')
                    notes.append('语音：'+label+(f" · {speech['duration']:.1f}秒" if speech['duration'] else ''))
                notes.extend(explain(key) for key in links.get(message,[]))
                history=phases.get(message,[])
                if history:
                    steps=[]
                    for event in history[-6:]:
                        label=PHASES.get(event.get('phase'),event.get('phase',''))
                        reason=event.get('reason')
                        if reason:label+='：'+REASONS.get(reason,reason)
                        if event.get('retry_after'):label+=f"，当时需等待约{event['retry_after']}秒"
                        if not steps or steps[-1]!=label:steps.append(label)
                    notes.append('处理过程：'+' → '.join(steps))
                dec=decisions.get(message,{})
                gate,basis=dec.get("gate"),dec.get("basis")
                if gate:
                    prefix='初始调度：' if history or links.get(message) or time.time()-at>30 else '调度判断：'
                    notes.append(prefix+GATES.get(gate,gate)+("（"+BASES.get(basis,basis)+"）" if basis and basis!="none" else ""))
                if not history and not links.get(message) and gate in ('waiting_batch','ready','busy') and time.time()-at>30:
                    prior={box.get('trigger_id') for box in queued
                           if box.get('reply_user_id')==str(r.get('user_id')) and 0<at-box.get('created_at',0)<1800
                           and 0<receipts.get(turn(box),{}).get('at',at)<at}
                    if scope == 'group' and len(prior)>=7:
                        notes.insert(0,'根据当时发送记录推断：已触及个人回复上限（半小时7轮）；旧日志未保存取消事件')
                    else:notes.append('后续状态未记录，不能据此判断仍在等待')
                if not notes:notes.append("当前日志未记录这条消息的处理原因")
            else:
                notes.append(explain(turn(r)))
            body=r.get("text") if kind!="queued" else "\n".join(map(str,r.get("segments",[])))
            rows.append(dict(kind=kind,at=at,user_id=str((r.get("reply_user_id") or row_session.split(":",1)[1]) if kind != "received" and scope == "private" else (r.get("user_id") or r.get("reply_user_id") or "")),message_id=str(r.get("message_id") or ""),session=row_session,session_name=names.get(row_session,''),actor=r.get("nickname") or r.get("user_id") or "",
                             reply_model=reply_model(turn(r)) if kind!='received' else None,
                             text=str(body or "")[:12000],decision="；".join(filter(None,notes))))
    if scope == 'private':
        _attach_relation_people(state, rows)
    return sorted(rows,key=lambda r:r["at"],reverse=True)


def _attach_relation_people(state, rows):
    """Link private actors only when a group-visible person page exists."""
    candidates = set()
    for row in rows:
        actor = row.get('user_id') if row['kind'] == 'received' else ''
        candidate = actor or row.get('user_id') or row['session'].split(':', 1)[1]
        row['relation_person_id'] = ''
        row['_relation_candidate'] = candidate
        candidates.add(candidate)
    visible = set()
    path = state / 'memory.sqlite3'
    if candidates and path.is_file():
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                ordered = sorted(candidates)
                for start in range(0, len(ordered), 900):
                    batch = ordered[start:start + 900]
                    placeholders = ','.join('?' for _ in batch)
                    query = (
                        "SELECT DISTINCT user_id FROM records WHERE state!='deleted' "
                        "AND session LIKE 'group:%' AND user_id IN (" + placeholders + ")"
                    )
                    visible.update(str(record[0]) for record in db.execute(query, batch))
        except sqlite3.Error:
            pass
    for row in rows:
        candidate = row.pop('_relation_candidate')
        if candidate in visible:
            row['relation_person_id'] = candidate


def message_sessions(base, scope='group'):
    """Console-only directory from preserved originals and registered routes."""
    if scope not in ('group', 'private'):
        raise ValueError('invalid scope')
    state = Path(base) / 'state'
    names = {}
    nickname_times = {}
    sessions = {}
    path = state / 'memory.sqlite3'
    if path.is_file():
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                query = (
                    "SELECT session,MAX(at) FROM records WHERE kind='chat' "
                    "AND state!='deleted' AND session LIKE ? GROUP BY session"
                )
                for key, at in db.execute(query, (scope + ':%',)):
                    sessions[key] = {'session': key, 'last_at': at}
                if scope == 'private':
                    query = (
                        "SELECT nickname,at FROM records WHERE kind='chat' AND state!='deleted' "
                        "AND session=? AND user_id!='SELF' AND nickname!='' "
                        "ORDER BY at DESC,id DESC LIMIT 1"
                    )
                    for key in sessions:
                        row = db.execute(query, (key,)).fetchone()
                        if row:
                            names[key], nickname_times[key] = row
        except sqlite3.Error:
            pass
    sources = (
        ('outbox.jsonl', 1200),
        ('sent_messages.jsonl', 2000),
        ('chat_history.jsonl', 300),
    )
    for filename, limit in sources:
        for record in scoped_tail(state / filename, limit, scope):
            key = record['session_key']
            at = record.get('received_at') or record.get('created_at') or record.get('at') or 0
            entry = sessions.setdefault(key, {'session': key, 'last_at': 0})
            entry['last_at'] = max(entry['last_at'] or 0, at)
            if scope == 'private' and record.get('nickname'):
                if at >= nickname_times.get(key, 0):
                    names[key] = record['nickname']
                    nickname_times[key] = at
    path = state / 'official_routes.sqlite3'
    if path.is_file():
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                row = db.execute(
                    "SELECT value FROM settings WHERE key=?", (scope + '_routes',)
                ).fetchone()
                routes = json.loads(row[0]).values() if row else []
                for route in routes:
                    key = scope + ':' + str(route['local_id'])
                    sessions.setdefault(key, {'session': key, 'last_at': 0})
                    if route.get('name'):
                        names[key] = route['name']
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            pass
    if scope == 'group':
        from hakimi.console.group_catalog import group_metadata
        names.update({key: value['label'] for key, value in group_metadata(base, sessions).items()})
    for key, row in sessions.items():
        fallback = ('私聊 ' if scope == 'private' else '群 ') + key.split(':', 1)[1]
        row['label'] = row['session_name'] = names.get(key) or fallback
    return {'groups': sorted(sessions.values(), key=lambda row: row['last_at'] or 0, reverse=True)}
