"""Bounded operator diagnostics. No chat bodies, credentials or task tokens."""
import json,os,re,threading,time
from pathlib import Path
_LOCK=threading.Lock()

def record(base,payload,phase,*,trigger_id='',reason='',retry_after=0):
    try:
        session=payload.get('session_key','')
        if not re.fullmatch(r'(?:group|private):[1-9][0-9]{0,19}',session):return
        ids=payload.get('pending_explicit_question_ids',[]) or [payload.get('reply_target_message_id','')]
        ids=list(dict.fromkeys(str(i) for i in ids if re.fullmatch(r'-?[0-9]{1,20}',str(i))))[:20]
        if not ids:return
        row=dict(at=time.time(),session_key=session,message_ids=ids,phase=phase,
                 trigger_id=str(trigger_id or payload.get('trigger_id',''))[:100],
                 reason=reason if re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}',str(reason)) else '',
                 retry_after=round(max(0,float(retry_after or 0))))
        path=Path(base)/'state/message_lifecycle.jsonl'
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.is_symlink():return
        with _LOCK,path.open('a+',encoding='utf-8') as f:
            if os.name!='nt':
                import fcntl
                fcntl.flock(f,fcntl.LOCK_EX)
            f.seek(0,2)
            if f.tell()>1_000_000:
                f.seek(0);kept=f.readlines()[-1500:];f.seek(0);f.truncate();f.writelines(kept)
            f.write(json.dumps(row,ensure_ascii=True,separators=(',',':'))+'\n')
    except (OSError,ValueError,TypeError):pass

def entry(base,item,phase,*,reason=''):
    signature=(phase,reason)
    if item.get('_lifecycle')==signature:return
    item['_lifecycle']=signature
    rows=item.get('direct_messages',[])
    record(base,dict(session_key=item['session_key'],pending_explicit_question_ids=[r.get('message_id') for r in rows]),phase,reason=reason)
