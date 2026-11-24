"""Account-bound, evidence-grounded top-level video comments; at most one attempt."""
from dataclasses import dataclass
import math
import re
from difflib import SequenceMatcher

from bili_companion.domain import Scope
from bili_companion.policy.budget import new_york_date


@dataclass(frozen=True)
class VideoCommentCandidate:
    bvid: str
    text: str
    evidence_ids: tuple[str,...]
    expires_at: float
    watch_epoch: int
    session_id: str

    def __post_init__(self):
        if not math.isfinite(self.expires_at) or self.watch_epoch<0:
            raise ValueError('invalid video comment time/epoch')


def normalized(text):
    return ''.join(re.findall(r'[\w]',text.lower()))


AI_CLICHE_SUBSTRINGS = (
    '直接拉满', '拉满了', '一针见血', '拔高到', '的高度', '执念也太深', '执念太深',
    '不得不说', '属实是', '可谓是', '淋漓尽致', '完美诠释', '叹为观止',
    '耳朵都跟着', '耳朵一下就', '耳朵都差点', '勾起我耳朵', '支棱着耳朵',
    '把耳朵往后', '尾巴都要炸毛', '本猫耳朵', '耳朵当场',
)

AI_CLICHE_PATTERNS = (
    re.compile(r'(前一秒|前脚).{1,30}(转头|下一秒|后脚)'),
    re.compile(r'嘴上.{1,25}(手头|身体|视线)'),
    re.compile(r'刚把.{1,25}转头发现'),
)


def ai_cliche_comment(text):
    if not isinstance(text, str):
        return False
    if any(s in text for s in AI_CLICHE_SUBSTRINGS):
        return True
    if any(p.search(text) for p in AI_CLICHE_PATTERNS):
        return True
    if text.count('，') + text.count(',') >= 3 and len(text.strip()) > 42:
        return True
    return False


def specific_comment(text, evidence_text):
    if ai_cliche_comment(text):
        return False
    body=normalized(text)
    if not body or any(x in body for x in ('应该是','大概是','猜测','可能是','全片','整部','看完了')):
        return False
    # Require a concrete shared detail, rather than praise alone.
    generic_phrases=('这个这一段这段片段视频声音真的有意思很棒不错好看喜欢感觉感动温暖很有太棒了让人听着看着',)
    generic={phrase[i:i+2] for phrase in generic_phrases for i in range(len(phrase)-1)}
    detail=normalized(evidence_text)
    return any(body[i:i+2] in detail and body[i:i+2] not in generic
               for i in range(len(body)-1))


def sufficient_observation(video):
    intervals=sorted(video.get('comment_coverage',[]))
    end=total=0.0
    for start,stop in intervals:
        if not (0<=start<stop):continue
        total+=max(0,stop-max(start,end))
        end=max(end,stop)
    duration=video.get('duration')
    if isinstance(duration,(int,float)) and 0<duration<60:
        covered=0.0
        for start,stop in intervals:
            start,stop=max(0,start),min(duration,stop)
            if stop<=start:continue
            if start>covered+.2:return False
            covered=max(covered,stop)
        return covered>=duration-.2
    return video.get('comment_chunks',0)>=2 and total>=60


class VideoCommentExecutor:
    def __init__(self,database,memory,*,enabled=False,max_age_seconds=300):
        self.database,self.memory,self.enabled=database,memory,enabled
        self.max_age=min(300,max(1,max_age_seconds))
        with database.connect(write=True) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS video_comment_attempts (
                account TEXT,bvid TEXT,session TEXT,text TEXT,at REAL,day TEXT,state TEXT,
                PRIMARY KEY(account,bvid))''')
            columns={r[1] for r in db.execute('PRAGMA table_info(video_comment_attempts)')}
            if 'uploader_id' not in columns:
                db.execute("ALTER TABLE video_comment_attempts ADD COLUMN uploader_id TEXT DEFAULT ''")
        from .comment_replies import CommentReplies
        CommentReplies.initialize(database)

    def recent_comments(self,account_id):
        with self.database.connect() as db:
            return [r[0][:120] for r in db.execute(
                'SELECT text FROM video_comment_attempts WHERE account=? ORDER BY at DESC LIMIT 10',(account_id,))
                if not ai_cliche_comment(r[0])]

    def dispatch(self,candidate,current_provider,sender,*,account_id,clock):
        if not self.enabled:return 'disabled'
        now=clock()
        if not math.isfinite(now) or now>=candidate.expires_at:return 'expired'
        current=current_provider()
        if not self.active(candidate,current):return 'inactive_video'
        if (not isinstance(candidate.text,str) or not candidate.text.strip() or len(candidate.text)>120
                or any(ord(c)<32 for c in candidate.text) or not candidate.evidence_ids):
            return 'invalid_candidate'
        video=current.get('video') or {}
        if not sufficient_observation(video):return 'insufficient_observation'
        if video.get('interest')!='high':return 'low_interest'
        uploader=str(video.get('uploader_id') or '')
        if not uploader:return 'unknown_uploader'
        details=[]
        scope=Scope(account_id,'video',candidate.bvid)
        for identity in candidate.evidence_ids:
            evidence=self.memory.get(identity,scope)
            if (not evidence or evidence['session_id']!=candidate.session_id
                    or evidence['attribution']!='observation' or evidence['confidence']<.9
                    or evidence['modality'] not in {'audio','video','subtitle','mixed'}
                    or not 0<=now-evidence['observed_at']<=self.max_age
                    or candidate.expires_at>evidence['observed_at']+self.max_age):
                return 'stale_evidence'
            details.append(evidence['text'])
        if not specific_comment(candidate.text,' '.join(details)):return 'generic_comment'
        day=new_york_date(now).strftime('%Y-%m-%d')
        with self.database.connect(write=True) as db:
            if db.execute('SELECT 1 FROM video_comment_attempts WHERE account=? AND bvid=?',
                (account_id,candidate.bvid)).fetchone():return 'duplicate'
            if db.execute('SELECT count(*) FROM video_comment_attempts WHERE account=? AND day=?',
                (account_id,day)).fetchone()[0]>=4:return 'daily_limit'
            if db.execute('SELECT 1 FROM video_comment_attempts WHERE account=? AND at>?',
                (account_id,now-3600)).fetchone():return 'cooldown'
            if db.execute('SELECT count(*) FROM video_comment_attempts WHERE account=? AND day=? AND (uploader_id=? OR uploader_id="")',
                (account_id,day,uploader)).fetchone()[0]>=2:return 'uploader_limit'
            recent=db.execute('SELECT text FROM video_comment_attempts WHERE account=? ORDER BY at DESC LIMIT 20',
                (account_id,)).fetchall()
            if any(SequenceMatcher(None,normalized(candidate.text),normalized(r[0])).ratio()>=.8 for r in recent):
                return 'repeated_comment'
            db.execute("INSERT INTO video_comment_attempts (account,bvid,session,text,at,day,state,uploader_id) VALUES (?,?,?,?,?,?,'dispatching',?)",
                (account_id,candidate.bvid,candidate.session_id,candidate.text,now,day,uploader))
        response=None
        # Fences again after durable intent; no retry, including process death.
        if not self.active(candidate,current_provider()):status='cancelled'
        elif clock()>=candidate.expires_at:status='expired'
        else:
            try:
                response=sender(candidate.bvid,candidate.text,before_post=lambda:
                    self.active(candidate,current_provider()) and clock()<candidate.expires_at)
                status=response.get('status') if isinstance(response,dict) else response.status
                code=response.get('code') if isinstance(response,dict) else getattr(response,'code',None)
                if status=='rejected' and code=='cancelled_before_post':status='cancelled'
                if status not in {'sent','rejected','unknown','cancelled'}:status='unknown'
            except Exception:status='unknown'
        with self.database.connect(write=True) as db:
            db.execute("UPDATE video_comment_attempts SET state=? WHERE account=? AND bvid=? AND state='dispatching'",
                (status,account_id,candidate.bvid))
            rpid=response.get('rpid') if isinstance(response,dict) else getattr(response,'rpid',None)
            oid=response.get('oid') if isinstance(response,dict) else getattr(response,'oid',None)
            if status=='sent' and type(rpid) is int and rpid>0 and type(oid) is int and oid>0:
                db.execute('INSERT OR IGNORE INTO comment_receipts VALUES (?,?,?,?,?,?,?,?)',
                    (account_id,candidate.bvid,oid,rpid,candidate.text,now,'accepted',None))
            if response is not None:
                field=lambda name,default=None:response.get(name,default) if isinstance(response,dict) else getattr(response,name,default)
                db.execute('INSERT OR REPLACE INTO comment_delivery_outcomes VALUES (?,?,?,?,?,?,?)',
                    (account_id,candidate.bvid,0,str(field('code',''))[:80],str(field('notice',''))[:40],field('review_state'),now))
        return status

    @staticmethod
    def active(candidate,current):
        video=current.get('video') or {}
        return (current.get('state')=='WATCHING_VIDEO' and not current.get('live_session')
            and current.get('watch_epoch')==candidate.watch_epoch and video.get('bvid')==candidate.bvid
            and video.get('session_id')==candidate.session_id and video.get('observations',0)>=1)
