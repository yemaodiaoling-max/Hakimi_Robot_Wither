"""Deterministic consolidation rules (no database, network or model): synonyms, explicit names, sourced answers, discussion episodes."""
import hashlib
import html
import re
from urllib.parse import urlsplit, urlunsplit


SYNONYMS = (('称呼','昵称','名字','叫我'), ('研究','科研','专业','在学','学习方向'),
            ('颅骨','头骨'), ('声学','声音传播'), ('离子阱','囚禁离子'),
            ('量子模拟','量子仿真'), ('足迹','主线章节预告'), ('不向光者','潘塔罗涅','富人'))


def expanded_terms(text, tokenizer):
    tokens = tokenizer(text)
    for group in SYNONYMS:
        if any(word in text for word in group):
            tokens.update(tokenizer(' '.join(group)))
    return tokens


def source_url(value):
    """Reject attachment credentials, non-public addresses, tracking and auth URLs."""
    from hakimi.memory.text import valid_source
    import ipaddress
    value=html.unescape(str(value)).strip().rstrip('。。，,；;）)]}>')
    if not valid_source(value):return ''
    p=urlsplit(value);host=p.hostname.lower()
    if host=='localhost' or '.' not in host or host.endswith(('.local','.internal')):return ''
    try:
        if not ipaddress.ip_address(host).is_global:return ''
    except ValueError:pass
    if any(word in host for word in ('qq.com','qq.com.cn','gtimg','ugcimg')):return ''
    if re.search(r'(?i)(token|rkey|secret|signature|auth|password|fileid)=',p.query):return ''
    return urlunsplit((p.scheme,p.netloc,p.path,p.query,''))


def explicit_person(row):
    """Only a narrow, direct first-person statement; never infer from forwards/jokes."""
    text=html.unescape(str(row.get('text',''))).strip()
    user=str(row.get('user_id',''))
    if not re.fullmatch(r'[1-9][0-9]{0,19}',user) or len(text)>240:return None
    if any(x in text for x in ('[','引用','转发','假如','假设','比如','开玩笑','骗你的','哈哈','？','?','「','“','"')):return None
    text=re.sub(r'^@哈基米\s*','',text)
    explicit=bool(re.match(r'^(?:请)?记住[，,:：\s]*',text))
    text=re.sub(r'^(?:请)?记住[，,:：\s]*','',text)
    match=re.fullmatch(r'(?:以后)?(?:请)?(?:叫我|称呼我)([^，。！？\n]{1,24})[。！]?',text)
    if match:key,value='称呼',match[1].strip()
    else:
        match=re.fullmatch(r'我(?:正在学|在学|正在研究|研究方向是|的专业是)([^，。！？\n]{2,60})[。！]?',text)
        if match:key,value='学习方向',match[1].strip()
        elif explicit:
            match=re.fullmatch(r'我(不喜欢|喜欢)([^，。！？\n]{2,50})[。！]?',text)
            if not match:return None
            key,value='偏好:'+match[2].strip(),match[1]+match[2].strip()
        else:return None
    if re.search(r'密码|地址|手机号|身份证|诊断|病史|性癖|萝莉|正太|自杀|自残',value):return None
    return {'kind':'person','person_id':user,'key':key,'note':value,
            'session_key':row['session_key'],'created_at':row['received_at'],
            'origin_message_id':str(row.get('message_id','')),'evidence':str(row['text'])[:240],
            'verification':'self_report','shared':int(row['session_key'].startswith('group:')),
            'provider':'deterministic'}


def sourced_reply(session, trigger, question, reply, now):
    """Index bounded sourced answers as unverified notes, not as established facts."""
    urls=list(dict.fromkeys(filter(None,(source_url(x) for x in re.findall(r'https://[^\s<>"|]+',reply)))))[:2]
    if not urls or len(reply)>10000:return []
    clean=re.sub(r'https://[^\s<>"|]+','',reply)
    clean=re.sub(r'\[[^\]]*\]\(|[\[\]()*#]',' ',clean).replace('|',' ')
    clean=' '.join(clean.split()).strip(' ：:。')
    if len(clean)<24:return []
    # No invented sentence-boundary truncation presented as a full conclusion.
    excerpt=clean[:280]+('…' if len(clean)>280 else '')
    return [{'kind':'lore','session_key':session,'key':'source-'+hashlib.sha256((url+excerpt).encode()).hexdigest()[:16],
             'note':excerpt,'source':url,'created_at':now,'origin_message_id':str(trigger),
             'evidence':('当时的问题：'+question[:180]+'\n已发送回答节选：'+excerpt)[:520],
             'verification':'answer_unverified','shared':0,'provider':'delivered_reply'} for url in urls]


def discussion_episode(session,trigger,question,reply,now):
    """Remember an actual substantial discussion, without endorsing its conclusions."""
    if not trigger or not 12<=len(question)<=1600 or not 100<=len(reply)<=10000:return None
    if not re.search(r'研究|课题|推导|声学|量子|考据|原文|实验|项目|学习计划',question):return None
    if any(x in question+reply for x in ('密码','身份证','手机号','病史','性癖','[合并','=== 消息')):return None
    q=re.sub(r'https?://\S+','[链接]',question)
    answer=re.sub(r'https?://\S+','[链接]',reply)
    return {'kind':'episode','session_key':session,'key':'discussion-'+hashlib.sha256(q.encode()).hexdigest()[:16],
            'note':'曾讨论：'+q[:110]+'\n当时回复节选（不保证结论正确）：'+answer[:160],
            'evidence':('提问：'+q[:180]+'\n已发送回复：'+answer[:300]),
            'verification':'observed_dialogue','created_at':now,
            'origin_message_id':str(trigger),'provider':'delivered_reply'}
