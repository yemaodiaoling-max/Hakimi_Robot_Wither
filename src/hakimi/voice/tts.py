"""Context-aware voice offers and sending policy with session/turn ownership gates."""
from contextlib import contextmanager
import hashlib,json,os,re,sqlite3,subprocess,time
from pathlib import Path
from hakimi.voice.store import settings
from hakimi.transport.file_delivery import directory

_TEXT_ONLY = re.compile(r'(?:不要|(?<![特区识辨级个分])别(?![人的家处])|不用|禁止|不准|不想|不需要|勿)[^，,。！？!?；;\n]{0,24}(?:语音|朗读|出声|声音|念出来|读出来)|(?:只|请用|还是用)(?:发|用|回)?(?:文字|文本)|(?:还是|只|就)(?:用)?打字|(?:打字|文字)(?:就好|就行)|text only', re.I)

_VOICE_QUOTES = re.compile(r'“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|"[^"\n]*"')
_VOICE_REQUEST = re.compile(
    r'(?:给我|请|帮我|能不能|可以).{0,8}(?:发|回).{0,5}语音'
    r'|(?:^|[，,。：:\s])(?:发|回|来)(?:我)?(?:一条|一段|条|段|个)?语音'
    # The user can choose the output medium without naming any particular action.
    r'|用语音'
    r'|(?:给|替|帮|为)[^，,。！？!?；;\n]{1,16}?(?:发|回)(?:一条|一段|条|段|个)?语音'
    r'|语音(?:回复|说给|读给)|(?:请|帮我|能不能).{0,5}朗读'
    r'|(?:^|[，,。：:\s])(?:朗读|读给我听|念给我听)'
    r'|(?:想(?:要)?|让我|给我)听听?(?:一下)?(?:你的|你|哈基米的)(?:声音|说话)'
    r'|用声音.{0,16}(?:说|讲|回)|read aloud|speak aloud|send (?:a )?voice message', re.I)
_VOICE_REPORTED = re.compile(r'他说|她说|它说|有人说|群友说|转述|原话|例如|比如|举例|讨论|提到|让我|要求我')
_VOICE_DESCRIPTION = re.compile(r'昨天|刚才|之前|刚刚|我经常|我平时|为什么|怎么会')
_VOICE_LABELS = re.compile(r'\[[^]\n]*\]|<[^>\n]*>')
_VOICE_COMMENT = re.compile(r'(?:很|挺|比较|真|太).{0,8}(?:方便|好听|麻烦|奇怪|有趣|好用)')
_VOICE_CAPABILITY = re.compile(
    r'(?:你(?:能|会|可以|支持|是否支持)|会不会|能否|是否支持)'
    r'(?:用)?(?:语音(?:说话|讲话|回复|回答)?|发语音|朗读)'
    r'(?:吗|么|嘛|功能)?[？?。！!\s]*$', re.I)


def _explicit_text(text):
    # Quoted examples and transport-added voice labels cannot authorize speech.
    text=_VOICE_QUOTES.sub(' ',text)
    if _TEXT_ONLY.search(text):return False
    text=_VOICE_LABELS.sub(' ',text)
    if _VOICE_CAPABILITY.search(text):return False
    for match in _VOICE_REQUEST.finditer(text):
        start=max(text.rfind(c,0,match.start()) for c in '\n。！？!?；;')+1
        prefix=text[start:match.start()].rstrip()
        if _VOICE_REPORTED.search(prefix):continue
        boundary=re.match(r'[，,：:]',match.group())
        clause='' if boundary else re.split(r'[，,：:]',prefix)[-1]
        if _VOICE_DESCRIPTION.search(clause):continue
        if match.group()=='用语音' and (
            re.search(r'(?:^|[\s，,。])(?:我|他|她|我们|他们|她们)(?:也|通常|喜欢|习惯|正在|一直|在)?$',clause)
            or re.match(r'(?:输入|识别|转文字)(?:法|功能|软件|系统|时|的|怎么|为什么)',text[match.end():])
        ):continue
        if _VOICE_COMMENT.match(text[match.end():]):continue
        return True
    return False


def explicit(payload,base=None):
    from hakimi.prompt.turn_text import current_text
    return _explicit_text(current_text(payload,directives_only=True,base=base,include_quotes=False))


_VOICE_TOPIC = re.compile(r'语音|音色|声带|麦克风|朗读|念给|读给|开口说|(?:你的|哈基米的)声音|\bvoice\b|read aloud|speak aloud',re.I)
_VOICE_FOLLOWUP = re.compile(r'(?:请|麻烦|那就|那|你)?(?:再(?:来|发|回)(?:一条|一段|条|段)|(?:再|重新)(?:念|读)(?:一遍|一次)?|换个说法再(?:念|读)(?:一次|一遍)?)(?:吧|呀|呢|好吗|好不好)?[。！!？?\s]*$')


def _voice_media(media):
    name=str(media.get('name','')) if isinstance(media,dict) else ''
    return isinstance(media,dict) and media.get('kind')=='file' and name.startswith('_voice_') and name.endswith('.silk')


def _voice_offer(payload,base,*,for_send=False):
    """Return (offer mode, optional sending allowed) without changing state."""
    if isinstance(payload.get('internal_review'),dict) or payload.get('internal_review') or payload.get('cold_topic_request') or payload.get('is_proactive'):
        return None,False
    cfg=settings(base) if base else {}
    if not cfg.get('enabled') or not cfg.get('tts_enabled'):return None,False
    from hakimi.core.context_window import select_context
    from hakimi.prompt.turn_text import current_text
    from hakimi.voice.store import preview
    rows,target=select_context(payload)
    session=payload.get('session_key')
    if not session or not target or target.get('session_key',session)!=session or target.get('user_id')=='SELF':return None,False
    current=current_text(payload,directives_only=True,base=base,include_quotes=False)
    # search_policy's synthetic network-only fallback is not a user voice veto.
    current=current.replace('禁止联网：此语音原文暂不可用','')
    if _explicit_text(current):return 'requested',True
    current=_VOICE_LABELS.sub(' ',_VOICE_QUOTES.sub(' ',current))
    if _TEXT_ONLY.search(current):return None,False
    followup=bool(_VOICE_FOLLOWUP.fullmatch(re.sub(r'@[^\s]+\s*','',current).strip()))
    reference=str(target.get('reply_to') or '')
    target_index=next((i for i,row in enumerate(rows) if row is target),len(rows)-1)
    offered=sendable=False
    native=[];continuations=[]
    for index,row in enumerate(rows):
        if row.get('session_key',session)!=session:continue
        raw=str(row.get('text',''))
        attachment=bool(row.get('voice_ref') or row.get('_voice_attachments') or row.get('voice_attachments') or 'voice.read message_id=' in raw)
        if _VOICE_TOPIC.search(_VOICE_LABELS.sub(' ',raw)) or row.get('user_id')=='SELF' and _voice_media(row.get('media')):
            offered=sendable=True
        if attachment:
            offered=True
            if row.get('message_id'):native.append(row)
        if followup and (0<=target_index-index<=2 or reference and str(row.get('message_id',''))==reference):
            continuations.append(row)
    seen={}
    def readable(mid):
        if mid not in seen:seen[mid]=preview(base,session,mid).get('text') if base else None
        return bool(seen[mid])
    # Only an actual native transcript or acknowledged SELF file can resolve an
    # ambiguous "another one". A fabricated display label is not permission.
    if followup:
        own_ids=set()
        if base and (reference or any(row.get('user_id')=='SELF' for row in continuations)):
            from hakimi.core.jsonio import tail_jsonl
            from hakimi.core.runtime_io import read_derived_cached
            path=Path(base)/'state/sent_messages.jsonl'
            receipts=read_derived_cached(path,lambda p:tail_jsonl(p,count=64,max_bytes=262144),'voice-followup-receipts-v1')
            for receipt in receipts:
                media=receipt.get('media') or {}
                if receipt.get('session_key')==session and _voice_media(media):
                    own_ids.add(str(receipt.get('message_id','')))
        if reference in own_ids:return 'requested',True
        for row in continuations:
            mid=str(row.get('message_id') or '')
            media=row.get('media') or {}
            own=row.get('user_id')=='SELF' and (mid in own_ids or _voice_media(media))
            if own or row in native and mid and readable(mid):return 'requested',True
    # A cold direct quote can identify exactly one scoped native transcript.
    if reference and base and (not offered or followup) and (Path(base)/'state/voice.sqlite3').is_file():
        if readable(reference):
            if followup:return 'requested',True
            offered=sendable=True
    # Failed/untranscribed attachments may advertise the tool but do not newly
    # authorize autonomous speech about audio the bot has not understood.
    if for_send and not sendable:
        sendable=any(readable(str(row['message_id'])) for row in native[-3:])
    return ('context' if offered else None),sendable


def offer_mode(payload,base):
    """Expose voice only for a current request or visible voice-related context."""
    return _voice_offer(payload,base)[0]


def speech_mode(payload, base):
    """Sending policy: explicit requests bypass only the autonomous cooldown."""
    mode,sendable=_voice_offer(payload,base,for_send=True)
    if mode=='requested':return 'requested'
    if not mode or not sendable:return None
    path=Path(base)/'state/voice_tts.sqlite3'
    if path.is_file():
        db=None
        try:
            db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)
            if db.execute("SELECT 1 FROM receipts WHERE session=? AND at>? LIMIT 1",
                          (payload['session_key'],time.time()-1800)).fetchone():
                return None
        except sqlite3.Error:
            return None  # Unavailable cooldown state cannot enable autonomy.
        finally:
            if db is not None:db.close()
    return 'optional'


def hum_requested(payload,base=None):
    if isinstance(payload.get('internal_review'),dict) or payload.get('internal_review') or payload.get('cold_topic_request') or payload.get('is_proactive'):
        return False
    from hakimi.prompt.turn_text import current_text
    text=current_text(payload,directives_only=True,base=base,include_quotes=False)
    if _TEXT_ONLY.search(text):return False
    if re.search(r'(?:不要|别|不准|不用|禁止|不想).{0,4}(?:唱|哼)|完整|指定|朗读|歌词|《|》',text):return False
    request=re.search(r'(?:^|[，,。：:\s])(?:请|可以|能不能|帮我|给我)?(?:哼|唱)|(?:^|[，,。：:\s])(?:来|给我来)一段你自己的',text)
    return bool(request and re.search(r'原创|自己|哼一段|哼个|哼几句',text))


def offer_hum(payload,base):
    if not hum_requested(payload,base):
        return False
    cfg=settings(base)
    return bool(cfg.get('enabled') and cfg.get('tts_enabled'))


@contextmanager

def receipts(base):
    db=sqlite3.connect(Path(base)/'state/voice_tts.sqlite3',timeout=2)
    db.execute('CREATE TABLE IF NOT EXISTS receipts(session TEXT,filename TEXT,text TEXT,at REAL,PRIMARY KEY(session,filename))')
    columns={r[1] for r in db.execute('PRAGMA table_info(receipts)')}
    if 'kind' not in columns:db.execute("ALTER TABLE receipts ADD COLUMN kind TEXT DEFAULT 'speech'")
    db.execute('CREATE TABLE IF NOT EXISTS attempts(filename TEXT,at REAL,status TEXT)')
    try:yield db;db.commit()
    finally:db.close()

def fallback(base,session,filename):
    path=Path(base)/'state/voice_tts.sqlite3'
    if not path.exists():return None
    with receipts(base) as db:row=db.execute('SELECT text FROM receipts WHERE session=? AND filename=?',(session,filename)).fetchone()
    return row[0] if row else None

def voice_tag(base=None):
    cfg=settings(base) if base else {}
    keys = ('tts_engine','tts_voice','tts_steps')
    if cfg.get('tts_engine') == 'gpt-sovits':
        keys += ('tts_role_revision',)
    return '|'.join(str(cfg.get(key,'')) for key in keys)

def generated_name(session,trigger,text,base=None):
    value=session+'/'+trigger+'/'+text+'/'+voice_tag(base)
    return '_voice_'+hashlib.sha256(value.encode()).hexdigest()[:32]+'.silk'

def prepared_for_turn(base,payload,filename):
    session = payload['session_key']
    trigger = payload.get('trigger_id')
    if not trigger or not filename.startswith('_voice_'):
        return False
    text = fallback(base,session,filename)
    return bool(text and filename == generated_name(session,str(trigger),text,base))

def synthesis_task(base,text,output,*,budget=None,requested=False):
    cfg=settings(base)
    task={'model':cfg['tts_model'],'text':text,'output':output,
          'engine':cfg.get('tts_engine','vits'),'fallback_model':cfg['tts_model'],
          'bitrate':int(cfg.get('tts_bitrate',32000)),'revision':voice_tag(base)}
    local_seconds=max(0.,float(cfg.get('tts_timeout_seconds',35)))
    total=local_seconds
    if task['engine']=='gpt-sovits':
        # The resident role client itself allows 180s for a CPU render. An
        # explicitly requested voice can use its turn for both synthesis stages.
        default_role_seconds=180 if requested else 25
        role_seconds=max(0.,float(cfg.get('tts_role_timeout_seconds',default_role_seconds)))
        if requested:total+=role_seconds
    if budget is not None:total=min(total,max(0.,float(budget)))
    task['timeout_seconds']=total
    if task['engine']=='zipvoice':
        task['zipvoice']={'socket':cfg.get('tts_socket',''),
                          'model':cfg.get('tts_zipvoice_model',''),
                          'pack':cfg.get('tts_voice_pack',''),
                          'voice':cfg.get('tts_voice','auto'),
                          'steps':int(cfg.get('tts_steps',4)),
                          'threads':int(cfg.get('tts_threads',4))}
    elif task['engine']=='gpt-sovits':
        fallback_seconds=min(local_seconds,total/3) if requested else 10.
        task['role'] = {
            'command': cfg.get('tts_role_command','/opt/hakimi/voice/role/say'),
            'timeout': min(role_seconds,max(0.,total-fallback_seconds)),
        }
    return task


def prepare(base,payload,text,timeout=None):
    import fcntl
    from hakimi.voice.worker import helper_env
    cfg=settings(base)
    if not cfg.get('enabled') or not cfg.get('tts_enabled'):return {'error':'voice_output_disabled'}
    if not isinstance(text,str) or not 1<=len(text.strip())<=80 or re.search(r'\[CQ:|https?://|[\x00-\x08]',text):return {'error':'voice_text_must_be_short_plain_text'}
    text=text.strip();session=payload['session_key'];trigger=str(payload.get('trigger_id',''))
    if not trigger:return {'error':'voice_turn_identity_required'}
    name=generated_name(session,trigger,text,base);root=directory(session,base);root.mkdir(parents=True,exist_ok=True);path=root/name
    lock=Path(base)/'state/voice_tts.lock'
    with lock.open('a') as stream:
        try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'error':'voice_busy_use_text'}
        if path.exists() and fallback(base,session,name)==text:return {'name':name,'cached':True}
        mode=speech_mode(payload,base)
        if not mode:return {'error':'use_text_this_turn'}
        temp=path.with_suffix('.new.silk')
        task=synthesis_task(base,text,str(temp),budget=timeout,requested=mode=='requested')
        budget=task.pop('timeout_seconds')
        if not budget:return {'error':'voice_deadline_use_text'}
        with receipts(base) as db:
            if db.execute('SELECT count(*) FROM attempts WHERE at>?',(time.time()-3600,)).fetchone()[0]>=cfg.get('tts_max_per_hour',20):return {'error':'voice_cpu_budget_use_text'}
        with receipts(base) as db:db.execute("INSERT INTO attempts VALUES (?,?,'running')",(name,time.time()))
        completed=False
        try:
            result=subprocess.run(['/usr/bin/nice','-n','10',cfg['python'],'-m','hakimi.voice.tts_worker'],input=json.dumps(task,ensure_ascii=False).encode(),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=budget,env=helper_env())
            if result.returncode or not temp.exists():return {'error':'voice_generation_failed_use_text'}
            metadata=json.loads(result.stdout)
            os.replace(temp,path)
            with receipts(base) as db:db.execute('INSERT OR REPLACE INTO receipts(session,filename,text,at) VALUES (?,?,?,?)',(session,name,text,time.time()))
            completed=True
            return dict(metadata,name=name)
        except subprocess.TimeoutExpired:return {'error':'voice_generation_timeout_use_text'}
        finally:
            with receipts(base) as db:db.execute("UPDATE attempts SET status=? WHERE filename=? AND status='running'",('complete' if completed else 'failed',name))
            for p in root.glob(temp.stem+'*'):p.unlink(missing_ok=True)

def _log_speech_result(base,claim,provider,hum,result):
    from hakimi.tools.math_render import log_tool_call
    name='voice.hum' if hum else 'voice.speak'
    trigger=str(claim.get('payload',{}).get('trigger_id',''))
    if result.get('error')=='already_replied' and claim.get('_reply_submitted'):return
    fields={'provider':provider,'trigger_id':trigger}
    if not result.get('ok'):
        reason=result.get('error') or (result.get('receipt') or {}).get('reason') or 'voice_submit_rejected'
        fields['error']=reason if isinstance(reason,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}',reason) else 'voice_submit_rejected'
    backend=(result.get('voice') or {}).get('backend')
    if backend in {'gpt-sovits','vits','vits-fallback','zipvoice'}:fields['backend']=backend
    log_tool_call(base,name,'ok' if result.get('ok') else 'error',**fields)


def failed_voice_reply(result, text):
    """Only a known pre-delivery failure can be turned into a text reply."""
    if result.get('error') not in {'use_text_this_turn','voice_busy_use_text',
            'voice_cpu_budget_use_text','voice_deadline_use_text','voice_output_disabled',
            'voice_generation_failed_use_text','voice_generation_timeout_use_text',
            'only_explicit_original_hum_supported','original_hum_not_configured','original_hum_integrity_failed'}:
        return None
    if isinstance(text,str) and 1 <= len(text.strip()) <= 80 and not re.search(r'\[CQ:|https?://|[\x00-\x1f]',text):
        return '这条语音没发出去，我用文字说：' + text.strip()
    return '这条语音没发出去。'


def speak(base,claim,coordinator,provider,text=None,*,hum=False,timeout=None):
    try:
        result=_speak(base,claim,coordinator,provider,text,hum=hum,timeout=timeout)
    except Exception as error:
        _log_speech_result(base,claim,provider,hum,{'error':'voice_exception_'+type(error).__name__})
        raise
    _log_speech_result(base,claim,provider,hum,result)
    return result


def _speak(base,claim,coordinator,provider,text=None,*,hum=False,timeout=None):
    if not coordinator.check_token(claim['token'],provider):return {'error':'turn_expired'}
    if claim.get('_reply_submitted'):
        return {'error':'already_replied'}
    state=coordinator.snapshot()
    if state.get('owner_token') == claim['token'] and state.get('reply_id'):
        return {'error':'already_replied'}
    now=time.time()
    remaining=claim.get('deadline',now+35)-now
    if timeout is not None:remaining=min(remaining,timeout)
    remaining-=2  # Leave time to hand the finished file to this turn's outbox.
    if remaining<=0:return {'error':'voice_deadline_use_text'}
    result=prepare_hum(base,claim['payload']) if hum else prepare(base,claim['payload'],text,remaining)
    if result.get('error'):return result
    if not coordinator.check_token(claim['token'],provider):return {'error':'turn_expired'}
    receipt=coordinator.submit_reply(claim['token'],[],provider,files=[{'name':result['name']}])
    if receipt.get('accepted'):claim['_reply_submitted']=True
    return {'ok':bool(receipt.get('accepted')),'receipt':receipt,'voice':result}


HUM_SHA='3f9213af7291ceff5b2dda2cc5704e3e85489e3dfdad9cf20b511d3a24cd6739'

def prepare_hum(base,payload):
    cfg=settings(base)
    if not cfg.get('enabled') or not cfg.get('tts_enabled') or not hum_requested(payload,base):return {'error':'only_explicit_original_hum_supported'}
    source=Path(cfg.get('hum_asset',''))
    if not source.is_file() or source.stat().st_size>1_000_000:return {'error':'original_hum_not_configured'}
    data=source.read_bytes()
    if hashlib.sha256(data).hexdigest()!=HUM_SHA:return {'error':'original_hum_integrity_failed'}
    session=payload['session_key'];name='_voice_hum_'+HUM_SHA[:16]+'.silk';path=directory(session,base)/name;path.parent.mkdir(parents=True,exist_ok=True)
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=HUM_SHA:
        temp=path.with_suffix('.tmp');temp.write_bytes(data);os.replace(temp,path)
    with receipts(base) as db:db.execute("INSERT OR REPLACE INTO receipts(session,filename,text,at,kind) VALUES (?,?,?,?,'hum')",(session,name,'这是一小段约7.5秒的原创无词哼唱，不是指定歌曲',time.time()))
    return {'name':name,'duration':7.48,'kind':'original_wordless_hum'}


def describe(base,session,filename):
    if not (Path(base)/'state/voice_tts.sqlite3').exists():return None
    with receipts(base) as db:row=db.execute('SELECT text,kind FROM receipts WHERE session=? AND filename=?',(session,filename)).fetchone()
    if not row:return None
    return '【已送达原创无词哼唱约7.5秒】' if row[1]=='hum' else '【已送达语音，合成朗读】'+row[0]


def cleanup(base):
    path=Path(base)/'state/voice_tts.sqlite3'
    if not path.exists():return
    cutoff=time.time()-max(900,min(86400,float(settings(base).get('tts_cache_seconds',3600))))
    with receipts(base) as db:
        rows=list(db.execute('SELECT session,filename FROM receipts WHERE at<?',(cutoff,)))
        for session,name in rows:
            if re.fullmatch(r'_voice_(?:hum_)?[0-9a-f]{16,32}\.silk',name):(directory(session,base)/name).unlink(missing_ok=True)
        db.execute('DELETE FROM receipts WHERE at<?',(cutoff,))
        db.execute('DELETE FROM attempts WHERE at<?',(time.time()-86400,))
    outgoing=Path(base)/'state/outgoing_files'
    if outgoing.resolve()!=outgoing.absolute():return
    for path in outgoing.glob('*/_voice_*'):
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(outgoing.resolve()) and path.stat().st_mtime<cutoff:
            if re.fullmatch(r'_voice_(?:hum_)?[0-9a-f]{16,32}(?:\.new)?\.(?:silk|wav|pcm)',path.name):path.unlink(missing_ok=True)
