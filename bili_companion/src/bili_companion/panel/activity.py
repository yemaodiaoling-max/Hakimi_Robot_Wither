"""Read a bounded, explicitly public activity export; never inspect live configuration."""
import json
import math
from pathlib import Path
import re


def read_activity(path):
    path = Path(path)
    if not path.exists():
        return dict(available=False, exported_at='', video_history=[], video_comment_attempts=[], qq_memory_reads=[], video_selections=[], runtime_status={})
    if path.stat().st_size > 4_000_000:
        raise ValueError('activity export too large')
    data = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(data, dict) or data.get('schema_version') not in (1, 2)
            or not isinstance(data.get('video_history'), list)
            or not isinstance(data.get('video_comment_attempts'), list)):
        raise ValueError('invalid activity schema')
    def string(value, limit=8000):
        return value[:limit] if isinstance(value, str) else ''
    def number(value):
        return value if type(value) in (int, float) and math.isfinite(value) else 0
    def bvid(row):
        value = row.get('bvid', '')
        return value if isinstance(value, str) and re.fullmatch(r'BV[0-9A-Za-z]{10}', value) else ''
    videos = []
    for row in data.get('video_history', [])[:500]:
        if not isinstance(row, dict): continue
        coverage = []
        for span in row.get('coverage', [])[:64]:
            if not isinstance(span, dict): continue
            start, end = number(span.get('start')), number(span.get('end'))
            if 0 <= start < end:
                coverage.append(dict(start=start, end=end, modality=string(span.get('modality'), 40)))
        videos.append(dict(bvid=bvid(row), title=string(row.get('title'), 300),
            summary=string(row.get('summary')), status=string(row.get('status'), 40),
            observations=number(row.get('observations')), coverage=coverage,
            updated_at=row.get('updated_at') if type(row.get('updated_at')) in (int, float) else string(row.get('updated_at'), 100)))
    comments = []
    for row in data.get('video_comment_attempts', [])[:500]:
        if not isinstance(row, dict): continue
        at = row.get('at', row.get('created_at', ''))
        comments.append(dict(bvid=bvid(row), text=string(row.get('text')),
            state=string(row.get('state'), 40), visibility=string(row.get('visibility'),40),rpid=number(row.get('rpid')), at=number(at) if type(at) in (int,float) else string(at,100)))
    replies=[dict(bvid=bvid(r),incoming=string(r.get('incoming'),1000),text=string(r.get('text'),120),state=string(r.get('state'),40),at=number(r.get('at')),root=number(r.get('root')),parent=number(r.get('parent')),rpid=number(r.get('rpid'))) for r in data.get('comment_reply_attempts',[])[:100] if isinstance(r,dict)]
    for item,raw in list(zip(comments,[r for r in data.get('video_comment_attempts',[])[:500] if isinstance(r,dict)]))+list(zip(replies,[r for r in data.get('comment_reply_attempts',[])[:100] if isinstance(r,dict)])):
        item.update(code=string(raw.get('code'),80),notice=string(raw.get('notice'),40),review_state=raw.get('review_state') if type(raw.get('review_state')) is int else None)
        for key in ('read_status','anonymous_state','anonymous_code','account_state','account_code'):
            if key in raw:item[key]=string(raw[key],40)
        if 'read_checked_at' in raw:item['read_checked_at']=number(raw['read_checked_at'])
        for key in ('anonymous_review_state','account_review_state'):
            if key in raw:item[key]=raw[key] if type(raw[key]) is int else None
    return dict(available=True, exported_at=number(data.get('exported_at')) if type(data.get('exported_at')) in (int,float) else string(data.get('exported_at'),100),
        video_history=videos, video_comment_attempts=comments,comment_reply_attempts=replies,
        runtime_status={k:v for k,v in data.get('runtime_status',{}).items() if k in {'state','understanding_state','next_observation_at','last_error'}},
        qq_memory_reads=[dict(read_at=number(r.get('read_at')),keyword=string(r.get('keyword'),80),status=string(r.get('status'),40),records=[dict(source_id=string(x.get('source_id'),160),revision=number(x.get('revision')),state=string(x.get('state'),40),summary=string(x.get('summary'),200)) for x in r.get('records',[])[:6] if isinstance(x,dict)]) for r in data.get('qq_memory_reads',[])[:100] if isinstance(r,dict)],
        video_selections=[dict(request_id=string(r.get('request_id'),160),at=number(r.get('at')),bvid=bvid(r),reason=string(r.get('reason'),120),qq_context=[dict(source_id=string(x.get('source_id'),160),revision=number(x.get('revision')),state=string(x.get('state'),40),summary=string(x.get('summary'),200)) for x in r.get('qq_context',[])[:3] if isinstance(x,dict)]) for r in data.get('video_selections',[])[:100] if isinstance(r,dict)])


ACTIVITY_PAGE = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>哈基米 · 观看与记忆</title><style nonce="NONCE">
:root{font-family:system-ui,"Microsoft YaHei",sans-serif;color:#253247;background:#f4f6fa}body{max-width:960px;margin:36px auto;padding:0 22px}main{background:white;border:1px solid #e1e6ee;border-radius:20px;padding:30px}h1{font-size:27px;margin:18px 0 8px}h2{font-size:20px;margin-top:30px}p{line-height:1.7;color:#566279}a{color:#245fbe}article{border-top:1px solid #e1e6ee;padding:20px 0;overflow-wrap:anywhere}.detail{white-space:pre-wrap;line-height:1.8}.meta{color:#66758b;font-size:13px;line-height:1.8}.notice{padding:12px 15px;background:#f0f5fd;border-radius:10px;line-height:1.7;font-size:13px}button{font:inherit;padding:9px 16px;border:0;border-radius:8px;background:#245fbe;color:white;cursor:pointer}.toolbar{display:flex;align-items:center;justify-content:space-between;gap:16px}.toolbar p{margin:0}.chips{display:flex;flex-wrap:wrap;gap:7px;align-items:center;margin-top:10px}.chip{display:inline-block;font-size:12px;line-height:1.6;padding:3px 9px;border:1px solid #d9e3f1;border-radius:20px;text-decoration:none;background:#f6f9fe}.chip:hover{background:#e9f1ff}.reason{margin:14px 0 8px;padding-left:12px;border-left:3px solid #bacfec;color:#44556d;font-size:14px}.label{font-size:12px;color:#6b7a90;margin-right:8px}details{border-top:1px solid #e8edf4;padding:12px 0}summary{cursor:pointer;line-height:1.8;color:#52627a;font-size:13px}details[open]>summary{margin-bottom:10px}details:target,.memory-entry:focus{outline:2px solid #b4cdf3;outline-offset:5px;border-radius:6px}.memory-entry{margin:10px 0 0;padding:10px 14px;background:#f7f9fc;border:0;border-radius:10px;scroll-margin-top:20px}.memory-entry summary{font-weight:600}.empty{color:#8692a3;font-size:13px}nav{display:flex;gap:20px;margin:22px 0;font-size:14px}@media(max-width:600px){body{margin:12px auto;padding:0 10px}main{padding:20px}.toolbar{align-items:flex-start}h1{font-size:23px}}
</style><main><a href="/" class="meta">返回接口设置</a><h1>哈基米 · 观看与记忆</h1><p>片段记录是实际观察依据；结束观看不代表完整看完。</p><div class="toolbar"><p id="updated" class="meta" role="status">正在读取…</p><button id="refresh">刷新</button></div><p class="meta">最近30天 · 每类最多100条。长期记忆另行保留，不额外调用模型。</p><nav><a href="#videos-heading">视频</a><a href="#memory-heading">QQ共享记忆</a><a href="#comments-heading">评论</a></nav><div id="runtime" class="notice"></div><h2 id="videos-heading">视频记录</h2><p class="meta">选片理由为模型自述；上下文标签只表示摘要曾提供给模型，不证明它导致了选择。</p><div id="videos"></div><details id="pending"><summary>未形成观看记录的选片</summary><div id="unwatched"></div></details><h2 id="memory-heading">QQ共享记忆</h2><p class="meta">仅展示允许共享的摘记。点击标签查看正文；过期、撤回或更新的修订不展示旧正文。</p><div id="memory"></div><details id="context-library"><summary>选片引用的共享摘记</summary><div id="context-details"></div></details><h2 id="comments-heading">评论尝试</h2><div class="notice">“平台已受理”不代表已公开可见；“结果未知”不能当作成功。</div><div id="comments"></div></main>
<script nonce="NONCE">
const videoLabels={selected:'已选中，尚无观看记录',continuing:'最近记录为观看进行中',finished:'本次观看已结束',sampled:'已抽样观察',skipped:'已跳过',unavailable:'视频不可用'};
const commentReadLabels={public_confirmed:'匿名详情已读到',account_only_readable:'发布账号可读而匿名未读到',both_not_read:'两侧均未读到',check_blocked:'接口核验受阻'};
const commentLabels={public_confirmed:'匿名详情已读到',silent:'保持沉默',generating:'正在生成回应',sent:'平台已受理（公开可见性未确认）',unknown:'结果未知',dispatching:'正在发送或等待结果',failed:'发送失败',rejected:'发送被拒绝',cancelled:'已取消发送',expired:'发送已过期'};
const readLabels={first_import:'首次导入',imported:'导入新内容',no_new_content:'无新内容',delivery_pending:'传输待确认',legacy_import:'历史接收 · 无逐轮审计'};
function element(tag,text,cls){const e=document.createElement(tag);e.textContent=text;if(cls)e.className=cls;return e;}
function time(value){if(!value)return '时间未记录';const d=new Date(typeof value==='number'?value*1000:value);return isNaN(d)?String(value):d.toLocaleString('zh-CN');}
function link(row){if(!row.bvid)return element('strong',row.title||'未选择视频');const a=element('a',row.title?row.title+' · '+row.bvid:row.bvid);a.href='https://www.bilibili.com/video/'+row.bvid;a.target='_blank';a.rel='noopener noreferrer';return a;}
function contextId(row){return 'qq-'+encodeURIComponent(row.source_id)+'-r'+row.revision;}
let initialContextNavigation=true;
function revealContext(hash,navigate=true){const entry=document.getElementById(hash.slice(1));if(!entry)return;for(let parent=entry;parent;parent=parent.parentElement){if(parent.tagName==='DETAILS')parent.open=true;}if(navigate){entry.scrollIntoView({behavior:'smooth',block:'center'});entry.focus({preventScroll:true});}}
function chip(row){const a=element('a','QQ · '+row.source_id.replace(/^qq:/,'')+' r'+row.revision,'chip');a.href='#'+contextId(row);a.addEventListener('click',event=>{event.preventDefault();history.replaceState(null,'',a.hash);revealContext(a.hash);});return a;}
function memoryEntry(row){const d=document.createElement('details');d.id=contextId(row);d.className='memory-entry';d.tabIndex=-1;d.append(element('summary','QQ · '+row.source_id.replace(/^qq:/,'')+' r'+row.revision+(row.state==='available'?'':' · 正文不可用')),element('div',row.summary||'该修订正文当前不可用。','detail'));return d;}
function selectionDetails(item,selection){if(!selection){item.append(element('p','未记录选片理由','empty'));return;}item.append(element('p','模型自述 · '+selection.reason,'reason'));const contexts=element('div','','chips');contexts.append(element('span','选片上下文','label'));for(const ref of selection.qq_context||[])contexts.append(chip(ref));if(!selection.qq_context.length)contexts.append(element('span','未提供QQ摘记','empty'));item.append(contexts);const trace=document.createElement('details');trace.append(element('summary','选择于 '+time(selection.at)),element('div','调用编号：'+selection.request_id,'meta'));item.append(trace);}
function render(data){
 const state=data.runtime_status||{},future=state.next_observation_at*1000>Date.now();
 const understanding=state.understanding_state==='WAITING_VIDEO_BUDGET'?(future?'预算节奏等待':'等待视频预算恢复'):({PAUSED_BUDGET:'预算暂停',REDUCED_BUDGET_PACING:'预算节奏等待',SAMPLED:'已完成观察',UNAVAILABLE:'观察不可用'}[state.understanding_state]||state.understanding_state||'未知');
 const runtime=document.getElementById('runtime');runtime.replaceChildren(element('div','最后记录：'+(state.state||'未知')+' · '+understanding),element('div','观察安排：'+(state.understanding_state==='WAITING_VIDEO_BUDGET'&&!future?'预算恢复后重新安排':time(state.next_observation_at))+(state.last_error?' · 错误类型：'+state.last_error:''),'meta'),element('div','计划与记录不保证当前正在观看；同步时间不是实际观察时间。','meta'));
 const openContextIds=new Set(Array.from(document.querySelectorAll('.memory-entry[open]')).map(e=>e.id));
 const reads=data.qq_memory_reads||[],selections=data.video_selections||[],latest=new Map(),contextRows=new Map();
 for(const row of selections){if(row.bvid&&(!latest.has(row.bvid)||row.at>latest.get(row.bvid).at))latest.set(row.bvid,row);for(const ref of row.qq_context||[])contextRows.set(contextId(ref),ref);}
 const memory=document.getElementById('memory'),library=document.getElementById('context-details');memory.replaceChildren();library.replaceChildren();
 const readIds=new Set();
 for(const row of reads){const d=document.createElement('details');d.append(element('summary',time(row.read_at)+' · '+(readLabels[row.status]||'状态未知')+' · 检索词 '+(row.keyword||'无')+' · '+row.records.length+' 条'));for(const ref of row.records){const id=contextId(ref);if(!readIds.has(id)){d.append(memoryEntry(ref));readIds.add(id);}else d.append(chip(ref));}memory.append(d);}
 for(const [id,ref] of contextRows){if(!readIds.has(id))library.append(memoryEntry(ref));}
 document.getElementById('context-library').hidden=!library.children.length;
 if(!reads.length)memory.append(element('p','暂无逐轮读取审计；旧记录的读取情况未知。','empty'));
 const videos=document.getElementById('videos'),unwatched=document.getElementById('unwatched'),videoIds=new Set();videos.replaceChildren();unwatched.replaceChildren();
 for(const row of data.video_history||[]){videoIds.add(row.bvid);const item=document.createElement('article');item.append(link(row),element('p',(videoLabels[row.status]||'状态未识别')+' · '+time(row.updated_at),'meta'),element('div',row.summary||'暂无观察摘要。','detail'));selectionDetails(item,latest.get(row.bvid));const coverage=document.createElement('details');coverage.append(element('summary','实际观察片段 · '+row.coverage.length+' 段'),element('div',row.coverage.length?row.coverage.map(c=>c.start+'–'+c.end+' 秒（'+({audio:'音频',video:'画面',mixed:'音画',subtitle:'字幕',visual:'画面',audiovisual:'音画'}[c.modality]||c.modality||'模态未记录')+'）').join('；'):'未记录','meta'));item.append(coverage);videos.append(item);}
 if(!videos.children.length)videos.append(element('p','暂无实际观看记录。','empty'));
 let pendingCount=0;for(const row of selections){if(row.bvid&&videoIds.has(row.bvid))continue;const item=document.createElement('article');item.append(link(row));selectionDetails(item,row);unwatched.append(item);pendingCount++;}
 document.getElementById('pending').hidden=!pendingCount;
 const comments=document.getElementById('comments');comments.replaceChildren();for(const row of [...(data.video_comment_attempts||[]),...(data.comment_reply_attempts||[])]){const item=document.createElement('article');item.append(link(row),element('p',(commentReadLabels[row.read_status]||commentReadLabels[row.visibility]||commentLabels[row.state]||'未确认状态')+' · '+time(row.at)+(row.read_status?' · 核验 '+time(row.read_checked_at)+' · 匿名 '+row.anonymous_state+'/'+row.anonymous_code+' · 账号 '+row.account_state+'/'+row.account_code:'')+(row.code?' · 业务码 '+row.code:'')+(row.notice?' · '+row.notice:'')+(row.review_state!==null&&row.review_state!==undefined?' · 审核状态 '+row.review_state:''),'meta'),element('div',(row.incoming?'对方回应：'+row.incoming+' → ':'')+(row.text||'评论内容未记录'),'detail'));comments.append(item);}if(!comments.children.length)comments.append(element('p','暂无评论尝试。','empty'));
 for(const id of openContextIds)revealContext('#'+id,false);
 if(location.hash&&initialContextNavigation)revealContext(location.hash,true);
 initialContextNavigation=false;
}
async function load(){const updated=document.getElementById('updated');try{const r=await fetch('/activity-data',{cache:'no-store'});if(!r.ok)throw Error();const data=await r.json();const stamp=typeof data.exported_at==='number'?data.exported_at*1000:Date.parse(data.exported_at);updated.textContent=data.available?'最近同步 '+time(data.exported_at)+(Date.now()-stamp>180000?' · 已超过 3 分钟，记录可能不是最新。':''):'尚未同步活动记录。';render(data);}catch(e){updated.textContent='活动记录暂时无法读取，请稍后刷新。';}}
document.getElementById('refresh').addEventListener('click',load);load();setInterval(load,60000);
</script></html>'''
