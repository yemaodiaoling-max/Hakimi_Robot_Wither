"""Turn-local search policy, with a replaceable, time-bounded classifier."""
import json
from pathlib import Path
import queue
import re
import threading

from hakimi.core.context_window import select_context
from hakimi.core.jsonio import read_json

_JUDGES = threading.BoundedSemaphore(2)


def current_text(payload,*,directives_only=False,base=None,include_quotes=True):
    rows,target=select_context(payload)
    session=payload.get('session_key')
    all_rows=[row for row in payload.get('recent_context_messages',[]) if isinstance(row,dict)
              and row.get('session_key',session)==session and row.get('user_id')!='SELF']
    author=target.get('user_id')
    window=payload.get('listening_window') or {}
    ids=window.get('selected_message_ids',window.get('new_message_ids',[]))
    ids={str(value) for value in ids} if isinstance(ids,list) else set()
    pending=payload.get('pending_explicit_question_ids') or []
    pending={str(value) for value in pending} if isinstance(pending,list) else set()
    if ids:
        current=[row for row in all_rows if str(row.get('message_id','')) in ids|pending
                 and (row.get('user_id')==author if author else row is target)]
    else:
        anchor=next((i for i,row in enumerate(rows) if row is target),len(rows))
        current=[row for row in rows[anchor:] if row.get('user_id')!='SELF'
                 and row.get('user_id')==target.get('user_id')]
        current=[row for row in all_rows if row in current or
                 str(row.get('message_id','')) in pending and author and row.get('user_id')==author]
    seen={str(row.get('message_id')) for row in current}
    reference=str(target.get('reply_to') or '')
    for _ in range(3 if include_quotes else 0):
        quoted=next((row for row in all_rows if str(row.get('message_id'))==reference),None)
        if not quoted or reference in seen:break
        if not directives_only or author and quoted.get('user_id')==author:current.insert(0,quoted)
        seen.add(reference);reference=str(quoted.get('reply_to') or '')
    pieces=[]
    for row in current:
        text=str(row.get('text',''))[:12000]
        if 'voice.read message_id=' in text:
            from hakimi.voice.store import policy_text
            from hakimi import paths
            transcript=policy_text(base or paths.base(),session,str(row.get('message_id','')))
            text+='\n'+(transcript or '禁止联网：此语音原文暂不可用')
        pieces.append(text)
    text='\n'.join(pieces)
    return text if directives_only else text[-8000:]


def network_allowed(payload,base=None):
    if payload.get('allow_network') is False or payload.get('network_allowed') is False:
        return False
    text=current_text(payload,directives_only=True,base=base)
    forbidden=(r'(?:不要|别|不许|禁止|无需|不用|不必|不准|不需要|不得|不能|勿).{0,5}(?:联网|上网|搜|查|检索|访问网页)',
               r'(?:不联网|不上网)(?:回答|也|直接|，|,|。|$)',
               r'(?:只|仅)(?:用|根据|凭).{0,8}(?:已有知识|现有知识|本地资料|下面材料|我贴的|我给的|我提供的)',
               r'(?:仅|只|就)?(?:用|凭|根据)(?:你的|已有的|已有|现有)?记忆(?:回答|作答|就行|即可)?|离线(?:回答|作答|处理|模式)',
               r"\b(?:do not|don't|without|no)\s+(?:use\s+)?(?:web|internet|browsing|search)|\boffline only\b")
    return not any(re.search(pattern,text,re.I) for pattern in forbidden)


def explicit_search(text):
    return bool(re.search(r'搜一下|搜索一下|(?:请|帮我|帮忙)(?:搜索|检索)|(?:^|[，,\s])搜(?!索)|搜搜|查一下|查查|帮(?:我|忙)?.{0,4}(?:搜|查)|上网查|联网查|(?:^|[，,：:\s])(?:请)?搜(?:下|\s)|\b(?:search|look up|browse)\b',text,re.I))


def local_decision(text):
    explicit=re.search(r'查一下|查查|搜一下|搜搜|帮.{0,5}(?:搜|查)|联网|核实|查证|辟谣|传闻',text)
    temporal=re.search(r'最新|近期|最近|今天|今日|本周|本月|目前|现在|刚刚|进展|现任',text)
    public=re.search(r'发布|公告|价格|行情|版本|更新|新闻|政策|赛事|比分|量子|研究|突破|论文|上线|模型|天气|球队|股票|汇率',text)
    return bool(explicit or temporal and public)


def compact_query(text):
    text=re.sub(r'\[CQ:[^\]]*\]|<qqbot-at-user[^>]*>|@\S+\s',' ',text)
    text=re.sub(r'^(?:请|麻烦|帮我|帮忙|你能不能|能不能|给我|查一下|搜索一下|搜一下|看看|查查)[，,：:\s]*','',text)
    return re.sub(r'\s+',' ',text).strip()[:240]


def config(base):
    root=Path(base);root=root.parent if root.name=='official_test_runtime' else root
    value=read_json(root/'search_routing.json',{})
    return value if isinstance(value,dict) else {}


def decide(payload,base,*,judge=None,timeout=3):
    if not network_allowed(payload,base):
        return {'search':False,'reason':'network_forbidden','backend':'policy'}
    if payload.get('internal_review') or payload.get('cold_topic_request'):
        return {'search':False,'reason':'background_no_presearch','backend':'policy'}
    text=current_text(payload,base=base)
    if explicit_search(current_text(payload,directives_only=True,base=base)):
        return {'search':True,'reason':'explicit_search','backend':'policy'}
    fallback={'search':local_decision(text),'reason':'local_rule','backend':'local'}
    if not text.strip() or judge is None:return fallback
    if not _JUDGES.acquire(blocking=False):return dict(fallback,reason='judge_busy_local_rule')
    results=queue.Queue(maxsize=1)
    def invoke():
        try:results.put((True,judge(text,base,timeout_seconds=max(.05,float(timeout)-.15))))
        except Exception as exc:results.put((False,type(exc).__name__))
        finally:_JUDGES.release()
    threading.Thread(target=invoke,daemon=True).start()
    try:ok,value=results.get(timeout=max(.05,min(5,float(timeout))))
    except queue.Empty:return dict(fallback,reason='judge_timeout_local_rule')
    if not ok:return dict(fallback,reason='judge_unavailable_local_rule')
    if not isinstance(value,dict) or type(value.get('search')) is not bool:
        return dict(fallback,reason='judge_invalid_local_rule')
    return {'search':value['search'],'reason':'classifier','backend':str(value.get('model','configured'))[:80]}
