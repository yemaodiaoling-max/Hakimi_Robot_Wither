"""Resolve observed group members locally and send a real QQ mention."""
import difflib,html,json,re,sqlite3,unicodedata
from contextlib import closing
from pathlib import Path
from hakimi.core.runtime_io import read_json_cached

ID=re.compile(r'[1-9][0-9]{0,19}\Z')
AT=re.compile(r'\[CQ:at,qq=([1-9][0-9]{0,19})\]')

def normal(value):
    return ''.join(c for c in unicodedata.normalize('NFKC',html.unescape(str(value))).casefold() if c.isalnum())

def resolve(base,payload,name):
    session=payload.get('session_key','')
    if not session.startswith('group:'):return {'ok':False,'error':'请在群聊中使用'}
    query=normal(name)
    if not query or len(query)>80:return {'ok':False,'error':'请提供成员昵称或编号'}
    members={}
    def add(uid,alias='',at=0):
        uid=str(uid)
        if not ID.fullmatch(uid):return
        at=float(at or 0)
        item=members.setdefault(uid,{'user_id':uid,'nickname':'','aliases':{uid},'at':0})
        alias=str(alias or '').strip()[:80]
        if alias and alias not in {'群友','朋友','SELF','用户'+uid}:
            item['aliases'].add(alias)
            if at>=item['at']:item.update(nickname=alias,at=at)
    for row in payload.get('recent_context_messages',[]):
        if row.get('session_key',session)==session:
            add(row.get('user_id'),row.get('nickname'),row.get('received_at',0))
            for uid in row.get('direct_targets',[]):add(uid)
    path=Path(base)/'state/memory.sqlite3'
    if path.exists():
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as db:
            for uid,nick,at in db.execute("SELECT user_id,nickname,MAX(at) FROM records WHERE session=? AND state!='deleted' GROUP BY user_id,nickname",(session,)):
                add(uid,nick,at)
            if members:
                marks=','.join('?' for _ in members);ids=list(members)
                for uid,nick,at in db.execute("SELECT user_id,nickname,MAX(at) FROM records WHERE session LIKE 'group:%' AND state!='deleted' AND user_id IN ("+marks+") AND nickname!='' GROUP BY user_id,nickname",ids):
                    members[uid]['aliases'].add(nick)
                tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'graph_aliases' in tables:
                    for node,alias in db.execute("SELECT DISTINCT node_id,alias FROM graph_aliases WHERE session LIKE 'group:%' AND node_id IN ("+marks+")",['person:'+uid for uid in ids]):
                        members[node[7:]]['aliases'].add(alias)
                if 'identity_member_links' in tables:
                    for old,uid in db.execute("SELECT old_user,new_user FROM identity_member_links WHERE active=1"):
                        if uid in members:
                            members[uid]['aliases'].add(old)
                            root=Path(base).parent if Path(base).name=='official_test_runtime' else Path(base)
                            bias=read_json_cached(root/'config.json',{}).get('user_bias',{}).get('users',{}).get(old,{})
                            if bias.get('nickname'):members[uid]['aliases'].add(bias['nickname'])
                for uid,key,text in db.execute("SELECT user_id,key,text FROM records WHERE session LIKE 'group:%' AND state!='deleted' AND kind='person' AND user_id IN ("+marks+") ORDER BY at DESC LIMIT 300",ids):
                    if key in {'昵称','名字','称呼','标准昵称','别名','nickname','name'} and len(text.strip())<=24:
                        members[uid]['aliases'].add(text.strip())
                    for alias in re.findall(r'(?:叫我|被叫|称呼(?:我|为)?|昵称(?:是|为)?|标准昵称(?:是|为)?|又叫|别名(?:是|为)?)[：:\s“\"\']*([^\s，。；;！!?？、”\"\']{1,24})',text[:800]):
                        members[uid]['aliases'].add(alias)
    root=Path(base).parent if Path(base).name=='official_test_runtime' else Path(base)
    for config in {Path(base)/'config.json',root/'config.json'}:
        users=read_json_cached(config,{}).get('user_bias',{}).get('users',{})
        for uid,item in members.items():
            bias=users.get(uid,{})
            if bias.get('nickname'):item['aliases'].add(bias['nickname'])
    self_ids={str(row.get('self_id','')) for row in payload.get('recent_context_messages',[])}|{'9000000000000000001'}
    route=Path(base)/'state/official_routes.sqlite3'
    actors=None
    if route.exists():
        with sqlite3.connect(route.resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as db:actors={r[0] for r in db.execute('SELECT local_id FROM actors')}
    ranked=[]
    for uid,item in members.items():
        if uid in self_ids or actors is not None and uid not in actors:continue
        names={normal(a) for a in item['aliases']} - {''}
        score=max((1 if query==n else .9 if min(len(query),len(n))>=2 and (query in n or n in query) else .8*difflib.SequenceMatcher(None,query,n).ratio() for n in names),default=0)
        if score>=.68:ranked.append((score,item))
    ranked.sort(key=lambda row:(-row[0],-row[1]['at'],row[1]['user_id']))
    exact=[item for score,item in ranked if score==1]
    chosen=exact[0] if len(exact)==1 else ranked[0][1] if len(ranked)==1 and ranked[0][0]>=.9 else None
    def brief(item):return {'user_id':item['user_id'],'nickname':item['nickname'] or next((a for a in sorted(item['aliases']) if a!=item['user_id']),item['user_id'])}
    if chosen:return {'ok':True,'member':brief(chosen)}
    return {'ok':False,'error':'请选择成员编号' if ranked else '本群尚未找到这个人','candidates':[brief(item) for _,item in ranked[:6]]}

def known_local_member(base,payload,uid):
    """Exact local ID and same-session observation; never resolve a nickname."""
    uid=str(uid);session=payload.get('session_key','')
    if not ID.fullmatch(uid) or not session.startswith('group:') or uid=='9000000000000000001':return False
    rows=[row for row in payload.get('recent_context_messages',[]) if isinstance(row,dict) and row.get('session_key',session)==session]
    if uid in {str(row.get('self_id','')) for row in rows}:return False
    for row in rows:
        if str(row.get('user_id',''))==uid or uid in [str(value) for value in row.get('direct_targets',[])]:return True
    path=Path(base)/'state/memory.sqlite3'
    if path.is_file():
        try:
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)) as db:
                if db.execute("SELECT 1 FROM records WHERE session=? AND user_id=? AND kind='chat' AND state!='deleted' LIMIT 1",(session,uid)).fetchone():return True
        except sqlite3.Error:pass
    # Actors are global aliases: only a message observed in this exact route
    # proves group membership. Merely finding an actor must never suffice.
    path=Path(base)/'state/official_routes.sqlite3'
    if path.is_file():
        try:
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)) as db:
                record=db.execute("SELECT value FROM settings WHERE key='group_routes'").fetchone()
                routes=json.loads(record[0]) if record else {}
                for route in routes.values():
                    if not isinstance(route,dict) or route.get('local_id')!=session[6:] or not route.get('active',True):continue
                    group=route.get('scope') or route.get('openid')
                    if group and db.execute("SELECT 1 FROM messages WHERE group_openid=? AND user_id=? AND direction='in' LIMIT 1",(group,uid)).fetchone():return True
        except (sqlite3.Error,ValueError,TypeError,AttributeError):pass
    return False


def mention(base,coordinator,token,provider,name,text=''):
    if not coordinator.check_token(token,provider):return {'ok':False,'error':'本轮已结束'}
    state=coordinator.snapshot()
    if state.get('reply_id'):return {'ok':False,'error':'本轮已发送'}
    result=resolve(base,state['payload'],name)
    if not result['ok']:
        cleared=coordinator.select_mention(token,provider,None)
        if not cleared.get('accepted'):return {'ok':False,'error':cleared.get('reason','mention_selection_rejected')}
        return result
    text_clean = str(text or '').strip()
    uid = result['member']['user_id']
    nick = result['member']['nickname']
    if not text_clean:
        selected=coordinator.select_mention(token,provider,uid)
        if not selected.get('accepted'):return {'ok':False,'error':selected.get('reason','mention_selection_rejected')}
        return {
            'ok': True,
            'sent': False,
            'user_id': uid,
            'nickname': nick,
            'hint': f'已选中成员【{nick}】。请只写要发送的正文，程序会在本轮发送时自动提及该成员。'
        }
    from hakimi.bridge.reply_format import text_bubbles
    parts=text_bubbles(text_clean) or [text_clean]
    receipt=coordinator.submit_reply(token,parts,provider,mention_user=uid)
    return {'ok':True,'sent':True,**result['member']} if receipt['accepted'] else {'ok':False,'error':receipt['reason']}

def mention_status(result):
    """Compact lookup/send outcome; no argument, member ID or provider text."""
    if result.get('ok'):return 'queued' if result.get('sent') else 'selected'
    if result.get('candidates'):return 'ambiguous'
    if result.get('error')=='本群尚未找到这个人':return 'not_found'
    return 'rejected'


def wire_text(text,store):
    def replace(match):
        with store.db() as db:row=db.execute('SELECT openid FROM actors WHERE local_id=?',(match[1],)).fetchone()
        if not row:raise ValueError('成员尚无官方发送标识')
        return '<qqbot-at-user id="'+html.escape(row[0],quote=True)+'" />'
    value=AT.sub(replace,text)
    if '[CQ:' in value:raise ValueError('不支持的消息片段')
    return html.unescape(value)
