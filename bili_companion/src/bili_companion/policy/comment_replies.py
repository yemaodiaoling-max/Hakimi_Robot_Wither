"""Bounded public reads of durable bot receipts; no manual comment adoption.

Only the first 20 replies under one recent tracked root are read per pass.
Absence never means rejection. Unknown POSTs and model calls are never retried.
"""
from .budget import new_york_date
import urllib.error


class CommentReplies:
    @staticmethod
    def initialize(database):
        with database.connect(write=True) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS comment_receipts (
                account TEXT,bvid TEXT,oid INTEGER,rpid INTEGER,text TEXT,at REAL,
                visibility TEXT,checked_at REAL,PRIMARY KEY(account,rpid))''')
            db.execute('''CREATE TABLE IF NOT EXISTS comment_reply_attempts (
                account TEXT,bvid TEXT,root INTEGER,parent INTEGER,author TEXT,
                incoming TEXT,text TEXT,at REAL,day TEXT,state TEXT,rpid INTEGER,
                request_id TEXT,PRIMARY KEY(account,parent))''')
            db.execute('''CREATE TABLE IF NOT EXISTS comment_delivery_outcomes (
                account TEXT,bvid TEXT,parent INTEGER,code TEXT,notice TEXT,
                review_state INTEGER,at REAL,PRIMARY KEY(account,bvid,parent))''')
            db.execute('''CREATE TABLE IF NOT EXISTS comment_read_checks (
                account TEXT,rpid INTEGER,anonymous_state TEXT,anonymous_code TEXT,
                account_state TEXT,account_code TEXT,checked_at REAL,
                PRIMARY KEY(account,rpid))''')
            columns={row[1] for row in db.execute('PRAGMA table_info(comment_read_checks)')}
            for name in ('anonymous_review_state','account_review_state'):
                if name not in columns:db.execute('ALTER TABLE comment_read_checks ADD COLUMN '+name+' INTEGER')

    def __init__(self,database,platform,engine,account,*,clock,enabled=False):
        self.initialize(database)
        self.database,self.platform,self.engine,self.account=database,platform,engine,account
        self.clock,self.enabled=clock,enabled
        self.next_poll=0

    @staticmethod
    def matches_identity(row,rpid,oid,uid,text,*,root=0,parent=0):
        return (type(row) is dict and row.get('rpid')==rpid and row.get('oid')==oid
            and row.get('root')==root and row.get('parent')==parent
            and str((row.get('member') or {}).get('mid'))==uid
            and (row.get('content') or {}).get('message')==text)

    @staticmethod
    def matches(row,rpid,oid,uid,text,*,root=0,parent=0):
        # Public visibility and all reply/preflight gates retain the strict
        # moderation-state requirement. Owner readability is a separate fact.
        return (CommentReplies.matches_identity(row,rpid,oid,uid,text,root=root,parent=parent)
            and row.get('state') in (None,0) and row.get('invisible') in (None,0,False))

    def read_side(self,receipt,uid,*,authenticated=False):
        try:
            args=(receipt['oid'],receipt['rpid'])
            thread=(self.platform.comment_thread(*args,authenticated=True) if authenticated
                else self.platform.comment_thread(*args))
            match=self.matches_identity if authenticated else self.matches
            matched=match(thread['root'],receipt['rpid'],receipt['oid'],uid,receipt['text'])
            return ('matched' if matched else 'mismatch'),'0',thread
        except Exception as exc:
            code=getattr(exc,'business_code',None)
            if type(code) is int:
                return ('missing' if code==12006 else 'blocked'),str(code),None
            if isinstance(exc,urllib.error.HTTPError):return 'blocked','http_'+str(exc.code),None
            if isinstance(exc,TimeoutError):return 'blocked','timeout',None
            if isinstance(exc,urllib.error.URLError):return 'blocked','network',None
            return 'blocked','response_or_auth',None

    def poll(self,allowed=lambda:True):
        now=self.clock()
        if now<self.next_poll:return 'cooldown'
        self.next_poll=now+600
        with self.database.connect() as db:
            row=db.execute('''SELECT * FROM comment_receipts WHERE account=? AND at>=?
                ORDER BY COALESCE(checked_at,0),at LIMIT 1''',(self.account,now-7*86400)).fetchone()
        if not row:return 'no_tracked_root'
        receipt=dict(row)
        # Advance even on unavailable roots so another receipt gets its turn.
        with self.database.connect(write=True) as db:
            db.execute('UPDATE comment_receipts SET checked_at=? WHERE account=? AND rpid=?',
                (now,self.account,receipt['rpid']))
        try:
            uid=self.platform.authenticated_uid()
        except Exception:
            with self.database.connect(write=True) as db:
                db.execute('''INSERT OR REPLACE INTO comment_read_checks
                    (account,rpid,anonymous_state,anonymous_code,account_state,account_code,checked_at) VALUES (?,?,?,?,?,?,?)''',
                    (self.account,receipt['rpid'],'not_checked','','blocked','identity_unavailable',now))
            return 'read_unavailable'
        anonymous,anonymous_code,thread=self.read_side(receipt,uid)
        account,account_code,account_thread='not_checked','',None
        if anonymous in {'missing','mismatch'}:
            account,account_code,account_thread=self.read_side(receipt,uid,authenticated=True)
        verified=anonymous=='matched'
        visibility=('public_confirmed' if verified else 'account_only_readable' if account=='matched'
            else 'both_not_read' if anonymous in {'missing','mismatch'} and account in {'missing','mismatch'}
            else 'check_blocked')
        with self.database.connect(write=True) as db:
            def review_state(value):
                state=(value.get('root') or {}).get('state') if type(value) is dict else None
                return state if type(state) is int else None
            db.execute('''INSERT OR REPLACE INTO comment_read_checks
                (account,rpid,anonymous_state,anonymous_code,account_state,account_code,checked_at,
                 anonymous_review_state,account_review_state) VALUES (?,?,?,?,?,?,?,?,?)''',
                (self.account,receipt['rpid'],anonymous,anonymous_code,account,account_code,now,
                 review_state(thread),review_state(account_thread)))
            # A blocked read never erases the last conclusive observation.
            if visibility!='check_blocked':
                db.execute('UPDATE comment_receipts SET visibility=?,checked_at=? WHERE account=? AND rpid=?',
                    (visibility,now,self.account,receipt['rpid']))
            if verified:
                for sent in db.execute("SELECT * FROM comment_reply_attempts WHERE account=? AND root=? AND state='sent'",
                    (self.account,receipt['rpid'])).fetchall():
                    if any(self.matches(r,sent['rpid'],receipt['oid'],uid,sent['text'],root=receipt['rpid'],parent=sent['parent']) for r in thread['replies']):
                        db.execute("UPDATE comment_reply_attempts SET state='public_confirmed' WHERE account=? AND parent=?",(self.account,sent['parent']))
        if not verified or not self.enabled or not allowed():return 'verified' if verified else 'read_unavailable' if visibility=='check_blocked' else 'not_confirmed'
        # Only direct responses to a receipt generated by this process, never
        # siblings replying to other people. One response per person/root ever.
        for incoming in thread['replies']:
            if type(incoming) is not dict:continue
            member=incoming.get('member') or {}; author=str(member.get('mid',''))
            text=(incoming.get('content') or {}).get('message','');parent=incoming.get('rpid')
            stamp=incoming.get('ctime')
            if (type(parent) is not int or parent<=0 or incoming.get('oid')!=receipt['oid']
                or incoming.get('root')!=receipt['rpid'] or incoming.get('parent')!=receipt['rpid']
                or not author.isdecimal() or author==uid or type(text) is not str or not 1<=len(text)<=1000
                or any(x in str(member.get('uname','')).lower() for x in ('bot','机器人','自动回复'))
                or type(stamp) not in (int,float) or not receipt['at']-60<=stamp<=now+60 or now-stamp>86400):continue
            day=new_york_date(now).strftime('%Y-%m-%d')
            with self.database.connect(write=True) as db:
                if db.execute('SELECT 1 FROM comment_reply_attempts WHERE account=? AND (parent=? OR (root=? AND author=?))',
                    (self.account,parent,receipt['rpid'],author)).fetchone():continue
                if db.execute('SELECT count(*) FROM comment_reply_attempts WHERE account=? AND day=?',(self.account,day)).fetchone()[0]>=4:return 'daily_limit'
                if db.execute('SELECT 1 FROM comment_reply_attempts WHERE account=? AND at>?',(self.account,now-3600)).fetchone():return 'reply_cooldown'
                db.execute('INSERT INTO comment_reply_attempts VALUES (?,?,?,?,?,?,?, ?,?,?,NULL,?)',
                    (self.account,receipt['bvid'],receipt['rpid'],parent,author,text,'',now,day,'generating',''))
            status,answer,request_id='silent','',''
            result=None
            try:
                answer,request_id=self.engine.reply_to_comment(receipt['text'],text)
                if answer and allowed():
                    # Revalidate ownership and target immediately before POST.
                    def preflight():
                        if (not allowed() or self.clock()-stamp>86400
                            or self.platform.authenticated_uid()!=uid):return False
                        fresh=self.platform.comment_thread(receipt['oid'],receipt['rpid'])
                        return (self.matches(fresh['root'],receipt['rpid'],receipt['oid'],uid,receipt['text'])
                            and any(self.matches(r,parent,receipt['oid'],author,text,root=receipt['rpid'],parent=receipt['rpid']) for r in fresh['replies']))
                    result=self.platform.send_comment_reply(receipt['oid'],receipt['rpid'],parent,answer,before_post=preflight)
                    status=result.status
                    sent_rpid=result.rpid
                else:sent_rpid=None
            except Exception:status,sent_rpid='unknown',None
            with self.database.connect(write=True) as db:
                db.execute('UPDATE comment_reply_attempts SET text=?,state=?,rpid=?,request_id=? WHERE account=? AND parent=?',
                    (answer,status,sent_rpid,request_id,self.account,parent))
                if result is not None:
                    db.execute('INSERT OR REPLACE INTO comment_delivery_outcomes VALUES (?,?,?,?,?,?,?)',
                        (self.account,receipt['bvid'],parent,result.code,result.notice,result.review_state,now))
            return status
        return 'no_eligible_reply'
