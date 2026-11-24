"""Per-group quiet windows and observed topic outcomes, using the memory DB."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import time

from hakimi.bridge.conversation_scheduler import features, similarity
from hakimi.memory.memory_store import store

MODE='冷场主动抛话题'


def settings(cfg):
    return cfg.get('lurker_schedule',{}).get('proactive_topic',{})


class ColdTopics:
    def __init__(self,base):
        self.base=Path(base)
        self.index=store(str(self.base.resolve()))
        self.known_groups=set()
        self.activity={}
        self.index.sync()
        with self.index.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS cold_groups (
                    session TEXT PRIMARY KEY, baseline REAL NOT NULL, last_received REAL NOT NULL DEFAULT 0,
                    last_attempt REAL NOT NULL DEFAULT 0, permission_checked REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS cold_attempts (
                    trigger TEXT PRIMARY KEY, session TEXT NOT NULL, created REAL NOT NULL,
                    sent REAL NOT NULL DEFAULT 0, deadline REAL NOT NULL DEFAULT 0,
                    topic TEXT NOT NULL DEFAULT '', own_message TEXT NOT NULL DEFAULT '',
                    nodes TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS cold_session ON cold_attempts(session,created);
            ''')
            self.watching={row[0] for row in db.execute("SELECT session FROM cold_attempts WHERE status='watching' AND deadline>=?",(time.time(),))}

    def bootstrap(self,cfg,now):
        active={str(g) for g in cfg.get('bot_profile',{}).get('active_groups',[])}
        groups=active or set(cfg.get('group_weights',{}))
        ignored={str(g) for g in cfg.get('bot_profile',{}).get('ignored_groups',[])}
        new=groups-ignored-self.known_groups
        if not new:
            return
        with self.index.db() as db:
            db.executemany('INSERT OR IGNORE INTO cold_groups(session,baseline) VALUES (?,?)',
                           [('group:'+g,now) for g in new])
        self.known_groups.update(new)

    def due(self,cfg,now=None):
        now=time.time() if now is None else now
        policy=settings(cfg)
        if not policy.get('enable',False):
            return None
        self.bootstrap(cfg,now)
        active={str(g) for g in cfg.get('bot_profile',{}).get('active_groups',[])}
        ignored={str(g) for g in cfg.get('bot_profile',{}).get('ignored_groups',[])}
        window=float(policy.get('quiet_window_seconds',72000))
        jitter=float(policy.get('window_jitter_seconds',1800))
        with self.index.db() as db:
            last=db.execute('SELECT max(created) FROM cold_attempts').fetchone()[0] or 0
            if now-last<300:
                return None
            rows=list(db.execute("SELECT c.*,(SELECT max(at) FROM records r WHERE r.kind='chat' AND r.session=c.session) AS indexed_received "
                                 "FROM cold_groups c ORDER BY max(baseline,last_received,last_attempt)"))
            for row in rows:
                gid=row['session'].split(':',1)[1]
                if gid in ignored or active and gid not in active:
                    continue
                base=max(row['baseline'],row['last_received'],row['last_attempt'],row['indexed_received'] or 0,self.activity.get(row['session'],0))
                offset=int(hashlib.sha256((row['session']+str(base)).encode()).hexdigest()[:8],16)/0xffffffff*jitter
                if now-base>=window+offset and now-row['permission_checked']>=600:
                    return row['session']
        return None

    def permission_denied(self,session,now):
        with self.index.db() as db:
            db.execute('UPDATE cold_groups SET permission_checked=? WHERE session=?',(now,session))

    def context(self,session,cfg):
        group=cfg.get('group_weights',{}).get(session.split(':',1)[1],{})
        with self.index.db() as db:
            old=list(db.execute("SELECT topic,status FROM cold_attempts WHERE session=? AND topic!='' ORDER BY created DESC LIMIT 3",(session,)))
            notes=list(db.execute("SELECT text FROM records WHERE session=? AND kind IN ('lore','episode','preference') "
                "AND (expires=0 OR expires>?) ORDER BY at DESC LIMIT 2",(session,time.time())))
        return {'group_name':group.get('name',''),'group_theme':group.get('theme',''),
                'quiet_hours':round(float(settings(cfg).get('quiet_window_seconds',72000))/3600,1),
                'history_hints':[r['text'][:90] for r in notes],
                'recent_attempts':[{'topic':r['topic'][:80],'outcome':'no_response_yet' if r['status']=='watching' else r['status']} for r in old]}

    def offered(self,session,trigger,now):
        with self.index.db() as db:
            db.execute("UPDATE cold_attempts SET status='no_response' WHERE session=? AND status='watching'",(session,))
            db.execute("INSERT OR IGNORE INTO cold_attempts(trigger,session,created,status) VALUES (?,?,?,'queued')",(trigger,session,now))
            db.execute('UPDATE cold_groups SET last_attempt=? WHERE session=?',(now,session))
        self.watching.discard(session)

    def sent(self,session,trigger,text,message_id,cfg,now):
        nodes=self.index.topic_signature(session,text)
        with self.index.db() as db:
            db.execute("UPDATE cold_attempts SET sent=?,deadline=?,topic=?,own_message=?,nodes=?,status='watching' "
                       "WHERE trigger=? AND session=? AND status='queued'",
                       (now,now+float(settings(cfg).get('response_window_seconds',72000)),text[:300],str(message_id or ''),json.dumps(nodes),trigger,session))
            row=db.execute("SELECT 1 FROM cold_attempts WHERE trigger=? AND status='watching'",(trigger,)).fetchone()
        if row:self.watching.add(session)

    def progress(self,snapshot,now):
        with self.index.db() as db:
            db.execute("UPDATE cold_attempts SET status='no_response' WHERE status='watching' AND deadline<?",(now,))
            db.execute("UPDATE cold_attempts SET status='not_delivered' WHERE status='queued' AND created<?",(now-900,))
            if snapshot.get('status')=='completed' and snapshot.get('outcome')!='replied':
                db.execute("UPDATE cold_attempts SET status='declined' WHERE trigger=? AND status='queued'",(snapshot.get('trigger_id',''),))
            self.watching={row[0] for row in db.execute("SELECT session FROM cold_attempts WHERE status='watching' AND deadline>=?",(now,))}

    def on_message(self,session,text,user_id,message_id,cfg,now,reply_to=''):
        if not session.startswith('group:') or not settings(cfg).get('enable',False):
            return
        self.activity[session]=now
        if session not in self.watching:
            return
        with self.index.db() as db:
            row=db.execute("SELECT * FROM cold_attempts WHERE session=? AND status='watching' AND sent<=? AND deadline>=? ORDER BY sent DESC LIMIT 1",
                           (session,now,now)).fetchone()
        if not row:
            return
        body=re.sub(r'^\[引用.*?\]\s*','',text,flags=re.S).strip()
        related=similarity(features(body),features(row['topic']))>=.15
        if not related and len(body)>=6:
            overlap=set(self.index.topic_signature(session,body)) & set(json.loads(row['nodes']))
            related=any(node.startswith('d') for node in overlap)
        explicit=bool(reply_to and reply_to==row['own_message'])
        if (explicit or related or '@哈基米' in body) and re.search('别刷屏|别发了|不要主动发言|别主动发言|别吵|闭嘴',body):
            with self.index.db() as db:
                db.execute("UPDATE cold_attempts SET status='negative' WHERE trigger=? AND status='watching'",(row['trigger'],))
            self.watching.discard(session)
            return
        if not (explicit or related) or not features(body):
            return
        with self.index.db() as db:
            changed=db.execute("UPDATE cold_attempts SET status='success' WHERE trigger=? AND status='watching'",(row['trigger'],)).rowcount
        if not changed:
            return
        self.watching.discard(session)
        note=('一次话题互动观察：哈基米以“'+row['topic'][:100]+'”开场，群友接话“'+body[:120]+'”。仅代表这次有人接话，不据此推断长期偏好。')[:300]
        entry={'kind':'episode','session_key':session,'note':note,'key':'cold:'+row['trigger'],
               'created_at':now,'origin_message_id':str(message_id),'provider':'program'}
        path=self.base/'state/session_memory.jsonl'
        prefix=''
        if path.exists() and path.stat().st_size:
            with path.open('rb') as stream:
                stream.seek(-1,2)
                prefix='' if stream.read(1)==b'\n' else '\n'
        with path.open('a',encoding='utf-8') as stream:
            stream.write(prefix+json.dumps(entry,ensure_ascii=False)+'\n')
        self.index.note(entry)


@lru_cache(maxsize=16)
def cold_store(base):
    return ColdTopics(Path(base))
