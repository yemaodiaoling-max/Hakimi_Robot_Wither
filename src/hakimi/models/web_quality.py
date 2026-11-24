"""Evidence quality is explicit: a retrieved snippet is not a verified fact."""
from datetime import datetime,timezone
from html.parser import HTMLParser
import ipaddress
import re
import time
from urllib.parse import urlsplit

_STOP={'latest','today','current','recent','news','official','november','october','september',
       'what','when','where','please','search','find','site','www','https','com','the','and','for'}


def blocked(title,text):
    title=str(title or '').strip().lower();text=str(text or '')
    if re.fullmatch(r'(?:just a moment[.!…]*|access denied|security check|sign in|登录|安全验证|访问验证)',title):return True
    if re.match(r'sign in\s*[-–:]|google accounts$',title):return True
    return len(text)<4000 and bool(re.search(r'verify (?:that )?you are human|enable javascript and cookies|unusual traffic|checking your browser|sign in to continue|请完成.*验证|访问过于频繁|登录后查看',text,re.I))


def terms(query):
    text=re.sub(r'(?:site|after|before|lang):\S+',' ',query.lower())
    english={word for word in re.findall(r'[a-z][a-z0-9-]{2,}',text) if word not in _STOP}
    text=re.sub(r'最新|最近|现在|今天|近期|帮我|请问|一下|是什么|怎么样|多少|哪些|有没有|更新公告|\d+年|\d+月',' ',text)
    chinese={part[i:i+2] for part in re.findall(r'[\u4e00-\u9fff]+',text) for i in range(len(part)-1)}
    return english|chinese


def relevant(query,title,text):
    needles=terms(query)
    if not needles:return False
    haystack=(str(title)+' '+str(text)).lower()
    english={word for word in needles if re.fullmatch('[a-z][a-z0-9-]+',word)}-{
        'google','gemini','openai','deepseek','microsoft','anthropic','official','latest'}
    lead=(str(title)+' '+str(text)[:500]).lower()
    if english and not any(word in lead for word in english):return False
    hits=sum(word in haystack for word in needles)
    return hits>=min(2,len(needles)) and hits/max(1,len(needles))>=.18


def published(value):
    if not isinstance(value,str):return None
    try:
        at=datetime.fromisoformat(value.strip().replace('Z','+00:00'))
        if at.tzinfo is None:at=at.replace(tzinfo=timezone.utc)
        if at.timestamp()>time.time()+172800 or at.year<1990:return None
        return at.isoformat()
    except (ValueError,OverflowError):return None


class Metadata(HTMLParser):
    def __init__(self):
        super().__init__();self.title=[];self.in_title=False;self.date=None
    def handle_starttag(self,tag,attrs):
        values=dict(attrs)
        if tag=='title':self.in_title=True
        if tag=='meta' and values.get('property',values.get('name','')).lower() in {'article:published_time','datepublished','pubdate'}:
            self.date=published(values.get('content')) or self.date
        if tag=='time' and not self.date:self.date=published(values.get('datetime'))
    def handle_endtag(self,tag):
        if tag=='title':self.in_title=False
    def handle_data(self,data):
        if self.in_title:self.title.append(data)


def period_status(query,date):
    if not date:return 'publication_date_unknown'
    value=datetime.fromisoformat(date)
    match=re.search(r'(20[0-9]{2})年?\s*(1[0-2]|0?[1-9])月',query)
    if match:return 'within_requested_month' if (value.year,value.month)==tuple(map(int,match.groups())) else 'outside_requested_month'
    english=re.search(r'(?i)\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(20[0-9]{2})\b',query)
    if english:
        months='january february march april may june july august september october november december'.split()
        return 'within_requested_month' if (value.year,value.month)==(int(english[2]),months.index(english[1].lower())+1) else 'outside_requested_month'
    return 'date_available_no_exact_requested_period'


def source(item,query='',*,read=False):
    url=str(item.get('url') or '');parsed=urlsplit(url);host=(parsed.hostname or '').lower()
    if parsed.scheme!='https' or parsed.username or parsed.password or parsed.port not in (None,443):return None
    if not host or '.' not in host or host.endswith('.local'):return None
    try:
        if not ipaddress.ip_address(host).is_global:return None
    except ValueError:pass
    if host.startswith(('accounts.','login.','signin.')) or re.search(r'/(?:login|signin|challenge)(?:/|$)',parsed.path):return None
    title=str(item.get('title') or '')[:300]
    text=str(item.get('text') or item.get('content') or item.get('summary') or item.get('snippet') or '')
    if len((title+' '+text).strip())<(40 if read else 10):return None
    if blocked(title,text):return None
    if not parsed.path.strip('/') and not item.get('published_at',item.get('date')):return None
    if query and not relevant(query,title,text):return None
    date=published(item.get('published_at',item.get('date')))
    age=(time.time()-datetime.fromisoformat(date).timestamp())/86400 if date else None
    result={'url':url,'title':title,'text':text[:24000 if read else 1100],
            'published_at':date,'requested_period_status':period_status(query,date),'date_provenance':item.get('date_provenance','provider_reported' if date else 'not_available'),
            'retrieved_at':datetime.now(timezone.utc).isoformat(),
            'evidence_level':'page_read' if read else 'search_snippet',
            'freshness':'unknown' if age is None else 'recent' if age<=90 else 'older',
            'provider':item.get('provider','public_web')}
    if host in {'genshin.hoyoverse.com','www.hoyolab.com','hoyolab.com'}:result['region']='international_server'
    for key in ('authority','rerank_score','region'):
        if key in item:result[key]=item[key]
    return result
