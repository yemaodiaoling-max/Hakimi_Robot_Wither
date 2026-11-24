"""One policy/budget/evidence boundary for primary, fallback and pre-search."""
from datetime import datetime,timezone
from decimal import Decimal
import json
import re
import time
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

from hakimi.models.search_policy import network_allowed,config,current_text,explicit_search
from hakimi.models.search_budget import SearchBudget,BudgetUnavailable
from hakimi.models import web_sources,web_quality
from hakimi.models.web_transport import request

PRICE=Decimal('.00075')


def _paid(kind,args,base,settings,timeout,transport):
    budget=SearchBudget(base,settings)
    reservation=budget.reserve('deepseek_'+kind,PRICE,leave_available=settings.get('_leave_available',0))
    result={}
    try:
        result=transport(kind,args,timeout);return result
    finally:
        bill=result.get('billable')
        budget.settle(reservation,PRICE if bill is True else 0 if bill is False else None)


def _public(url,deadline,transport):
    remaining=deadline-time.monotonic()
    if remaining<.2:return {}
    return transport('public',{'url':url},remaining)


def _official_news(query,deadline,transport):
    if not re.search(r'(?:原神|genshin).*(?:版本|更新|公告|最新|version|update|latest)',query,re.I):return []
    url='https://bbs-api-os.hoyolab.com/community/post/wapi/getNewsList?gids=2&type=1&page_size=5'
    response=_public(url,deadline,transport)
    try:data=json.loads(response.get('body',''))
    except ValueError:return []
    if data.get('retcode')!=0:return []
    result=[]
    for item in (data.get('data') or {}).get('list',[]):
        post=item.get('post',item)
        identity=str(post.get('post_id') or '')
        if not identity.isdecimal():continue
        at=post.get('created_at')
        if isinstance(at,str) and at.isdecimal():at=int(at)
        date=datetime.fromtimestamp(at,timezone.utc).isoformat() if type(at) in (int,float) and 0<at<32503680000 else None
        if not re.search(r'version|update|版本|更新|已知问题',str(post.get('subject','')),re.I):continue
        result.append({'url':'https://www.hoyolab.com/article/'+identity,'title':post.get('subject',''),
            'content':post.get('content',''),'date':date,'date_provenance':'official_post_created_at',
            'region':'international_server','provider':'hoyolab_official'})
    return sorted(result,key=lambda item:item.get('date') or '',reverse=True)


def _rss(query,deadline,transport):
    response=_public('https://www.bing.com/search?'+urlencode({'q':query,'format':'rss'}),deadline,transport)
    try:root=ET.fromstring(response.get('body',''))
    except ET.ParseError:return []
    return [{'title':item.findtext('title') or '', 'url':item.findtext('link') or '',
             'snippet':item.findtext('description') or '', 'provider':'bing_rss'} for item in root.findall('.//item')[:8]]


def run(name,args,timeout,base,payload,*,transport=None):
    try:
        result=_run(name,args,timeout,base,payload,transport=transport)
        if name=='web.read' and result.get('text'):
            text=result['text'];offset=max(0,int(args.get('offset') or 0));limit=max(200,min(6000,int(args.get('limit') or 4000)))
            result=dict(result,text=text[offset:offset+limit],offset=offset,total_chars=len(text),more=offset+limit<len(text))
        return result
    except Exception as exc:
        return {'error':'WEB_UNAVAILABLE','error_type':type(exc).__name__,'status':'not_verified'}


def _run(name,args,timeout,base,payload,*,transport=None):
    transport=transport or request
    if name=='web.sources':return web_sources.lookup(base,payload,args.get('message_id'))
    if name=='web.read':
        cached=web_sources.cached(base,payload,args['url'])
        if cached:return web_sources.record(base,payload,cached)
    if not network_allowed(payload,base):return {'error':'WEB_DISABLED_FOR_THIS_TURN','network_used':False}
    settings=config(base)
    if not explicit_search(current_text(payload,directives_only=True,base=base)):
        settings=dict(settings,_leave_available=settings.get('priority_reserve_usd','0.0015'))
    try:allowance=web_sources.reserve_access(base,payload,max_calls=min(8,int(settings.get('max_web_calls',6))),
        total_seconds=min(60,float(settings.get('total_web_seconds',40))),timeout=min(float(timeout),15))
    except (TimeoutError,ValueError):return {'error':'WEB_TURN_LIMIT','network_used':False}
    deadline=time.monotonic()+allowance
    from hakimi.models import aux_config
    paid_mode = aux_config.search_routing_config(base)['paid_search_mode'] if aux_config.has_config(base) else 'deepseek'
    if name=='web.search':
        query=str(args.get('query') or '').strip()[:128]
        if not query:return {'error':'WEB_EMPTY_QUERY','network_used':False}
        native={}
        if paid_mode == 'deepseek':
            try:native=_paid('search',{'query':query,'count':5,'need_content':True},base,settings,min(8,allowance),transport)
            except (BudgetUnavailable,OSError):pass
        items=(native.get('data') or {}).get('results',[])
        accepted=[]
        for item in items:
            prepared=web_quality.source(dict(item,provider='deepseek_search'),query,read=bool(item.get('content')))
            if prepared:accepted.append(prepared)
        if re.search(r'(?:原神|genshin).*(?:版本|更新|公告|最新|version|update|latest)',query,re.I):
            official=[]
            for item in _official_news(query,deadline,transport):
                prepared=web_quality.source(item,read=True)
                if prepared:prepared['requested_period_status']=web_quality.period_status(query,prepared.get('published_at'))
                if prepared:official.append(prepared)
            accepted=official+accepted
        if not accepted:
            for item in _rss(query,deadline,transport):
                prepared=web_quality.source(item,query)
                if prepared:accepted.append(prepared)
        result=[]
        for item in accepted[:5]:
            stored=web_sources.record(base,payload,item)
            result.append(dict(stored,text=stored.get('text','')[:1300],content_available=stored['evidence_level']=='page_read'))
        return {'query':query,'results':result,'status':'sources_found' if result else 'no_usable_sources',
                'guidance':'来源日期按published_at；search_snippet只作候选，不等于核实。需要细节再web.read。国际服公告不可当作国服。引用来源用[source_id]，如[W1]。'}
    if name=='web.read':
        result={}
        if paid_mode == 'deepseek':
            try:result=_paid('fetch',{'url':args['url']},base,settings,min(8,allowance),transport)
            except (BudgetUnavailable,OSError):pass
        data=result.get('data') or {}
        if data.get('content'):
            item=web_quality.source(dict(data,url=args['url'],provider='deepseek_fetch'),read=True)
        else:
            page=_public(args['url'],deadline,transport)
            if page.get('error'):return {'error':page['error'],'status':'page_not_read'}
            body=page.get('body','')
            from hakimi.models.vertex_web import Text
            metadata=web_quality.Metadata();metadata.feed(body)
            parser=Text();parser.feed(body)
            item=web_quality.source({'url':page.get('url',args['url']),'title':''.join(metadata.title),
                'text':body if page.get('kind')=='text/plain' else ''.join(parser.rows),
                'published_at':metadata.date,'date_provenance':'page_metadata' if metadata.date else 'not_available'},read=True)
        if not item:return {'error':'WEB_BLOCKED_OR_UNUSABLE_PAGE','status':'page_not_read'}
        return web_sources.record(base,payload,item)
    raise ValueError('unknown_web_tool')
