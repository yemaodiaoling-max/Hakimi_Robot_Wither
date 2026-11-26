"""Small delivered-message counters; models classify conflicts, never keywords."""
import json
import math
from pathlib import Path
import re
import time
import html
import unicodedata

from hakimi.core.runtime_io import read_json_cached,atomic_replace

WINDOW=480


def state(base):
    return read_json_cached(Path(base)/'state/interaction_policy.json',{'users':{},'groups':{},'receipts':[]})


def known_ai(user,cfg):
    profile=cfg.get('bot_profile',{})
    return str(user) in {str(uid) for key in ('known_bot_ids','ignored_bot_ids')
                        for uid in cfg.get(key,[])+profile.get(key,[])}


def ignored_text(text,cfg):
    return any(word in str(text) for word in cfg.get('always_ignored_keywords',[]) if isinstance(word,str) and word)


def blocked_outgoing_topic(text):
    # Deterministic text guard; images and contextual abbreviations remain model decisions.
    value=unicodedata.normalize('NFKC',html.unescape(str(text))).casefold()
    value=''.join(c for c in value if c.isalnum())
    return any(word in value for word in ('姆巴佩','mbappe','mbappé','姆总','姆皇'))


def ignored(user,base,cfg=None,now=None,*,bot_cooldown=True,session=''):
    from hakimi.bridge.bot_registry import confirmed,cooling
    if bot_cooldown and cooling(base,user,now):return True
    if session.startswith('private:'):return False
    if confirmed(base,user) or cfg and known_ai(user,cfg):return False
    return state(base).get('users',{}).get(str(user),{}).get('until',0)>(time.time() if now is None else now)


def annotation(payload,kind,base,target_id=None):
    if kind not in (None,'normal','conflict','ai'):raise ValueError('无效互动标记')
    from hakimi.core.context_window import select_context
    rows,target=select_context(payload)
    if target_id is not None:
        target=next((row for row in rows if str(row.get('message_id'))==str(target_id)),target)
    user=str(target.get('user_id',''))
    if not re.fullmatch(r'[1-9][0-9]{0,19}',user):return None
    cfg=read_json_cached(Path(base)/'config.json',{})
    if ignored_text(target.get('text',''),cfg):raise ValueError('目标消息命中硬忽略词')
    from hakimi.bridge.bot_registry import confirmed,cooling
    if cooling(base,user):raise ValueError('机器人十分钟接话冷却中')
    if kind!='ai' and ignored(user,base,cfg,session=payload.get('session_key','')):raise ValueError('该用户处于一小时争吵冷静期')
    return {'user_id':user,'kind':'ai' if confirmed(base,user) or target.get('is_bot') is True or known_ai(user,cfg) else kind or 'normal'}


def delivered(session,receipt,interaction,first,base,now=None,*,text='',provider=None,trigger_id='',message_id=None,sticker_digest=None):
    now=time.time() if now is None else now
    data=state(base)
    from hakimi.web.web_sources import delivered as source_delivered
    source_delivered(base,session,message_id,str(receipt).rsplit(':',1)[0])
    if receipt in data.get('receipts',[]):return
    data['receipts']=(data.get('receipts',[])+[receipt])[-128:]
    groups=data.setdefault('groups',{})
    groups[session]=[at for at in groups.get(session,[]) if 0<=now-at<WINDOW]+[now]
    data['groups']={key:values[-80:] for key,values in groups.items() if values and now-values[-1]<WINDOW}
    users=data.setdefault('users',{})
    if first and interaction and not session.startswith('private:'):
        uid=interaction['user_id']
        previous=users.get(uid,{})
        count=previous.get('rounds',0) if not previous.get('until',0) else 0
        count=count+1 if interaction['kind']=='conflict' else 0
        users[uid]={'rounds':count,'at':now,'until':now+3600 if count>=3 else 0}
        if count>=3:
            with (Path(base)/'state/interaction_events.jsonl').open('a',encoding='utf-8') as stream:
                stream.write(json.dumps({'at':now,'event':'conflict_cooldown','user_id':uid,'session_key':session,'until':now+3600})+'\n')
    if first and interaction:
        from hakimi.bridge.bot_registry import delivered as bot_delivered
        bot_delivered(base,interaction['user_id'],receipt,now)
    data['users']={uid:row for uid,row in users.items() if now-row.get('at',0)<86400 or row.get('until',0)>now}
    path=Path(base)/'state/interaction_policy.json'
    path.parent.mkdir(exist_ok=True)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(data,separators=(',',':')),encoding='utf-8')
    atomic_replace(temp,path)
    log=Path(base)/'state/sent_messages.jsonl'
    if log.exists() and log.stat().st_size>2*1024*1024:
        with log.open('rb') as stream:
            stream.seek(-512*1024,2);tail=stream.read().partition(b'\n')[2]
        temp=log.with_suffix('.tmp');temp.write_bytes(tail);atomic_replace(temp,log)
    plain=re.sub(r'\[CQ:[^\]]*\]','',text).strip()
    if text.startswith('[CQ:file,'):
        try:
            from hakimi.transport.file_delivery import from_segment
            from hakimi.voice.tts import describe as describe_voice
            path,kind=from_segment(text,session,base)
            if kind==3:plain=describe_voice(base,session,path.name) or plain
        except (ValueError,OSError):pass
    row = {'at':now,'session_key':session,'provider':provider,'trigger_id':trigger_id,
           'message_id':message_id,'text':plain[:1500]}
    if '[CQ:' in text:
        from hakimi.transport.sent_media_context import media_descriptor
        media = media_descriptor(text, base, sticker_digest)
        if media:
            row['media'] = media
    with log.open('a',encoding='utf-8') as stream:
        stream.write(json.dumps(row,ensure_ascii=False)+'\n')


def guidance(payload,base,now=None):
    now=time.time() if now is None else now
    session=payload.get('session_key','')
    cfg=read_json_cached(Path(base)/'config.json',{})
    from hakimi.prompt.group_profiles import resolve_profile
    profile,_=resolve_profile(session,Path(base))
    count=sum(0<=now-at<WINDOW for at in state(base).get('groups',{}).get(session,[]))
    study=profile['name']=='study_calm' or session.removeprefix('group:') in {
        str(g) for g in cfg.get('participation_policy',{}).get('study_groups',[])}
    hints=[]
    if session.startswith('group:') and not study and count>6:
        scale=max(.45,math.exp(-.10*(count-6)))
        hints.append(f'本群8分钟内已发{count}条；本轮日常聊天长度偏好约为平时{scale:.0%}，自然缩短并给别人空间；不硬截断，必要的专业论证仍完整。')
    from hakimi.core.context_window import select_context
    target=select_context(payload)[1]
    rounds=state(base).get('users',{}).get(str(target.get('user_id','')),{}).get('rounds',0)
    if rounds and not session.startswith('private:'):hints.append(f'当前对象此前连续争吵{rounds}/3轮；仅确属对你争吵且希望结束拉扯时标conflict，正常分歧不标，AI互动标ai豁免。')
    from hakimi.bridge.bot_registry import guidance as bot_guidance
    bot_note=bot_guidance(base,payload)
    if bot_note:hints.append(bot_note)
    return '\n'.join(hints)
