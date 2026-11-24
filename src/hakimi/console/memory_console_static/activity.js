const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.has('token')) {
  sessionStorage.setItem('memoryConsoleToken', fragment.get('token'));
  history.replaceState(null, '', location.pathname+location.search);
}

const videoLabels={selected:'已选中，尚无观看记录',continuing:'最近记录为观看进行中',finished:'本次观看已结束',sampled:'已抽样观察',skipped:'已跳过',unavailable:'视频不可用'};
const commentLabels={sent:'平台已受理（公开可见性未确认）',unknown:'结果未知',dispatching:'正在发送或等待结果',failed:'发送失败',rejected:'发送被拒绝',cancelled:'已取消发送',expired:'发送已过期'};
const readLabels={first_import:'首次导入',imported:'导入新内容',no_new_content:'无新内容',delivery_pending:'传输待确认',legacy_import:'历史接收 · 无逐轮审计'};
function element(tag,text,cls){const e=document.createElement(tag);e.textContent=text;if(cls)e.className=cls;return e;}
function time(value){if(!value)return '时间未记录';const d=new Date(typeof value==='number'?value*1000:value);return isNaN(d)?String(value):d.toLocaleString('zh-CN');}
function link(row){if(!row.bvid)return element('strong',row.title||'未选择视频');const a=element('a',row.title?row.title+' · '+row.bvid:row.bvid);a.href='https://www.bilibili.com/video/'+row.bvid;a.target='_blank';a.rel='noopener noreferrer';return a;}
function contextId(row){return 'qq-'+encodeURIComponent(row.source_id)+'-r'+row.revision;}
let initialContextNavigation=true;
function revealContext(hash,navigate=true){const entry=document.getElementById(hash.slice(1));if(!entry)return;for(let parent=entry;parent;parent=parent.parentElement){if(parent.tagName==='DETAILS')parent.open=true;}if(navigate){entry.scrollIntoView({behavior:'smooth',block:'center'});entry.focus({preventScroll:true});}}
function chip(row){const a=element('a','QQ · '+row.source_id.replace(/^qq:/,'')+' r'+row.revision,'chip');a.href='#'+contextId(row);a.addEventListener('click',event=>{event.preventDefault();history.replaceState(null,'',location.pathname+location.search+a.hash);revealContext(a.hash);});return a;}
function memoryEntry(row){const d=document.createElement('details');d.id=contextId(row);d.className='memory-entry';d.tabIndex=-1;d.append(element('summary','QQ · '+row.source_id.replace(/^qq:/,'')+' r'+row.revision+(row.state==='available'?'':' · 正文不可用')),element('div',row.summary||'该修订正文当前不可用。','detail'));const session=row.session||row.source_session;if(session){const a=element('a','查看会话消息');a.href='/messages.html?'+new URLSearchParams({session});d.append(a);}return d;}
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
 const comments = document.getElementById('comments');
 comments.replaceChildren();
 const attempts = [...(data.video_comment_attempts || []), ...(data.comment_reply_attempts || [])];
 attempts.sort((a, b) => b.at - a.at);
 for (const row of attempts) {
   const item = document.createElement('article');
   const confirmed = row.visibility === 'public_confirmed' || row.state === 'public_confirmed';
   const labels = {silent:'决定不回复', generating:'已登记回复意图', not_confirmed:'回查尚未确认',
     account_only_readable:'发布账号读到，匿名未读到', both_not_read:'发布账号与匿名均未读到',
     public_confirmed:'匿名详情已读到', check_blocked:'本次核验受阻'};
   const status = labels[row.read_status] || labels[row.visibility] ||
     (confirmed ? labels.public_confirmed : labels[row.state] || commentLabels[row.state] || '状态未知');
   const kind = row.parent ? '回复' : '顶层评论';
   item.append(link(row), element('p', kind+' · '+status+' · '+time(row.at),'meta'));
   if (row.incoming) item.append(element('div','对方：'+row.incoming,'meta'));
   if (row.text) item.append(element('div',row.text,'detail'));
   const receipt = [];
   if (row.notice) receipt.push(row.notice);
   if (row.code && row.code !== 'accepted') receipt.push('返回码 '+row.code);
   if (Number.isInteger(row.review_state)) receipt.push('提交时状态 '+row.review_state);
   if (Number.isInteger(row.account_review_state)) receipt.push('本人回查状态 '+row.account_review_state);
   if (row.read_checked_at) receipt.push('核验于 '+time(row.read_checked_at));
   if (row.anonymous_code) receipt.push('匿名查询 '+row.anonymous_code);
   if (row.account_code) receipt.push('登录查询 '+row.account_code);
   if (receipt.length) item.append(element('div',receipt.join(' · '),'meta'));
   if (/^BV[a-zA-Z0-9]{10}$/.test(row.bvid) && Number.isSafeInteger(row.rpid) && row.rpid > 0) {
     const locator = element('a','定位评论 '+row.rpid);
     locator.href = 'https://www.bilibili.com/video/'+row.bvid+'/#reply'+row.rpid;
     locator.target = '_blank';
     locator.rel = 'noopener noreferrer';
     item.append(locator);
   }
   comments.append(item);
 }
 if (!comments.children.length) comments.append(element('p','暂无评论尝试。','empty'));
 for(const id of openContextIds)revealContext('#'+id,false);
 if(location.hash&&initialContextNavigation)revealContext(location.hash,true);
 initialContextNavigation=false;
}
async function load(){const updated=document.getElementById('updated');try{const r=await fetch('/api/activity',{cache:'no-store',headers:{Authorization:'Bearer '+(sessionStorage.getItem('memoryConsoleToken')||'')}});if(!r.ok)throw Error();const {data}=await r.json();const stamp=typeof data.exported_at==='number'?data.exported_at*1000:Date.parse(data.exported_at);updated.textContent=data.available?'最近同步 '+time(data.exported_at)+(Date.now()-stamp>180000?' · 已超过 3 分钟，记录可能不是最新。':''):(data.reason==='module_disabled'?'视频机器人模块未启用，可以在「设置」里开启。':'尚未同步活动记录。');render(data);}catch(e){updated.textContent='活动记录暂时无法读取，请稍后刷新。';}}
document.getElementById('refresh').addEventListener('click',load);load();setInterval(load,60000);
