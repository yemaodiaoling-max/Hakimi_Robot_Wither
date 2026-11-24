"""Single-process watcher with interruptible capture and fenced model results."""
import argparse
import asyncio
import calendar
from dataclasses import replace
from datetime import timedelta
import json
import re
from pathlib import Path
import signal
import threading
import time

from bili_companion.domain import Scope
from bili_companion.cognition.live import BudgetPaused
from bili_companion.memory import MemoryStore
from bili_companion.policy.budget import AUTONOMOUS_VIDEO_ONLY, BudgetLedger, new_york_date
from bili_companion.policy.danmaku import ActionExecutor
from bili_companion.policy.video_comments import VideoCommentExecutor, sufficient_observation
from bili_companion.runtime.storage import Database
from bili_companion.runtime.retention import prune_activity, retained_observation_count
from bili_companion.runtime.diversity import candidates, exploration_required, topic

STALE=object()


def error_backoff(consecutive_errors):
    return min(300,5*2**min(6,max(0,consecutive_errors-1)))


def error_details(exc):
    details={'error_type':type(exc).__name__}
    from bili_companion.cognition.gateway import CognitionUnavailable
    if isinstance(exc,CognitionUnavailable):
        code=getattr(exc,'cause_code',exc.code)
        if type(code) is str and re.fullmatch('[a-z0-9_]{1,80}',code):details['error_code']=code
        provider_code=getattr(exc,'provider_code',None)
        if type(provider_code) is str and re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,79}',provider_code):
            details['provider_code']=provider_code
        status=getattr(exc,'http_status',None)
        if type(status) is int:details['http_status']=status
    code=getattr(exc,'code','') if type(exc).__name__=='PlatformError' else ''
    if code and re.fullmatch('[a-z_]{1,64}',str(code)):details['error_code']=code
    if type(exc).__name__=='MediaError' and str(exc) in {
        'already_started','cancelled','capture_failed','capture_or_probe_failed','capture_timeout',
        'invalid_interval','invalid_live_recorder','invalid_media_duration','live_start_failed',
        'missing_audio_video','probe_failed'}:details['error_code']=str(exc)
    return details


class WatchState:
    def __init__(self,database,room):
        self.database=database
        self.lock=threading.RLock()
        with database.connect(write=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS watcher_state (id INTEGER PRIMARY KEY,value TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS watcher_events (at REAL,kind TEXT,details TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS watcher_calls (request_id TEXT PRIMARY KEY,kind TEXT)')
            db.execute('''CREATE TABLE IF NOT EXISTS video_history (
                bvid TEXT PRIMARY KEY,selected_at REAL,updated_at REAL,selection_reason TEXT,
                observations INTEGER DEFAULT 0,interest TEXT DEFAULT 'neutral',decision TEXT DEFAULT '',
                summary TEXT DEFAULT '',coverage TEXT DEFAULT '[]',evidence_ids TEXT DEFAULT '[]',
                status TEXT DEFAULT 'selected')''')
            db.execute('CREATE TABLE IF NOT EXISTS video_selection_hints (bvid TEXT PRIMARY KEY,topic TEXT,source TEXT)')
            db.execute('INSERT OR IGNORE INTO watcher_state VALUES (1,?)',(json.dumps(dict(
                state='BROWSING',room_id=room,live_session=None,video=None,bookmark=None,
                watch_epoch=0,offline_count=0,last_status_at=0,live_position=0,
                paused_day=None,last_error=None,next_observation_at=0,understanding_state='NOT_OBSERVED')),))

    def snapshot(self):
        with self.lock,self.database.connect() as db:
            return json.loads(db.execute('SELECT value FROM watcher_state WHERE id=1').fetchone()[0])

    def change(self,function):
        with self.lock,self.database.connect(write=True) as db:
            state=json.loads(db.execute('SELECT value FROM watcher_state WHERE id=1').fetchone()[0])
            function(state)
            db.execute('UPDATE watcher_state SET value=? WHERE id=1',(json.dumps(state,allow_nan=False),))
            return state

    def log(self,kind,details,now):
        with self.database.connect(write=True) as db:
            db.execute('INSERT INTO watcher_events VALUES (?,?,?)',(now,kind,json.dumps(details,allow_nan=False)))


class Watcher:
    def __init__(self,config,platform,collector,engine,*,recorder_factory=None,clock=time.time):
        self.config,self.platform,self.collector,self.engine=config,platform,collector,engine
        self.clock=clock
        self.account=config['account_id']; self.room=str(config['priority_room'])
        if not str(self.room).isdigit():
            raise ValueError('priority_room must be a numeric Bilibili live room id')
        self.root=Path(config['state_dir']); self.root.mkdir(parents=True,exist_ok=True)
        self.database=Database(self.root/'watcher.sqlite3')
        self.state=WatchState(self.database,self.room)
        self.memory=MemoryStore(Database(config['native_database']))
        self.executor=ActionExecutor(self.database,self.memory,dict(allowed_rooms=[self.room],
            cooldown_seconds=max(90,config.get('cooldown_seconds',90)),hourly_danmaku_limit=min(12,config.get('hourly_danmaku_limit',12)),
            evidence_max_age_seconds=config.get('evidence_max_age_seconds',60),dry_run=not config.get('send_enabled',False)))
        self.comments=VideoCommentExecutor(self.database,self.memory,enabled=config.get('comments_enabled',False))
        from bili_companion.policy.comment_replies import CommentReplies
        self.comment_replies=CommentReplies(self.database,self.platform,self.engine,self.account,
            clock=self.clock,enabled=config.get('comment_replies_enabled',False))
        self.recorder_factory=recorder_factory
        self.recorder=None; self.recorder_epoch=None; self.recorder_offset=0
        self.cancel_capture=threading.Event()
        self.stop_event=asyncio.Event()
        self.wake_event=asyncio.Event()
        self.retired=set()
        self.video_target=None; self.video_target_epoch=None
        self.decision_enabled=True
        self.recorder_skips=0
        self.video_failures={}
        self.next_retention_check=0
        def migrate(s):
            if s.get('runtime_version',0)<2:
                s['runtime_version']=2
                s['next_observation_at']=0  # Once-only pacing migration, never a ledger reset.
                for video in (s.get('video'),s.get('bookmark')):
                    if video:video.setdefault('observations',0)
            if config.get('budget_scope')==AUTONOMOUS_VIDEO_ONLY and s['live_session']:
                s['next_observation_at']=min(s['next_observation_at'],
                    self.clock()+config.get('live_observation_seconds',90))
        self.state.change(migrate)
        if hasattr(self.engine,'epoch_is_current'):self.engine.epoch_is_current=self.epoch_is_current

    def epoch_is_current(self,scope,session,epoch):
        current=self.state.snapshot()
        if current['watch_epoch']!=epoch:return False
        if scope.kind=='live_room':
            return current['state']=='WATCHING_PRIORITY_LIVE' and current['live_session']==session and scope.identity==self.room
        video=current.get('video') or {}
        return current['state']=='WATCHING_VIDEO' and video.get('bvid')==scope.identity and video.get('session_id')==session

    def log(self,kind,**details):
        self.state.log(kind,details,self.clock())

    def _refresh_presence(self):
        try:
            from bili_companion.integrations.autobiography import write_presence_snapshot
            write_presence_snapshot(self.database.path, self.root.parent / 'presence.json', now=self.clock())
        except Exception:
            pass

    def apply_status(self,status):
        previous=self.state.snapshot()['watch_epoch']
        def update(s):
            at=float(status.get('checked_at',self.clock()))
            if at<s['last_status_at']:
                return
            s['last_status_at']=at
            live=status.get('is_live')
            if live is True:
                session=str(status.get('live_session_id') or '')
                if not session:
                    s['offline_count']=0
                    return
                if s['live_session']!=session:
                    if s['video']:
                        s['bookmark'],s['video']=s['video'],None
                    s['live_session']=session; s['watch_epoch']+=1; s['live_position']=0
                    s['next_observation_at']=self.clock()
                s['offline_count']=0
                if s['state']!='PAUSED_BUDGET':
                    s['state']='WATCHING_PRIORITY_LIVE'
            elif live is False and s['live_session']:
                s['offline_count']+=1
                if s['offline_count']>=2:
                    s['live_session']=None; s['offline_count']=0; s['watch_epoch']+=1
                    s['video'],s['bookmark']=s['bookmark'],None
                    s['next_observation_at']=min(s['next_observation_at'],
                        self.clock()+self.config.get('video_observation_seconds',180))
                    if s['state']!='PAUSED_BUDGET':
                        s['state']='WATCHING_VIDEO' if s['video'] else 'BROWSING'
            else:
                s['offline_count']=0
        current=self.state.change(update)
        if current['watch_epoch']!=previous:
            self.cancel_capture.set()
            self.wake_event.set()
            self.log('target_changed',epoch=current['watch_epoch'],state=current['state'])
            self._refresh_presence()
        return current

    async def poll(self):
        try:
            status=await asyncio.to_thread(self.platform.live_status,self.room)
        except Exception as exc:
            status=dict(is_live=None,checked_at=self.clock())
            self.log('status_unknown',error_type=type(exc).__name__)
        return self.apply_status(status)

    async def monitor(self):
        while not self.stop_event.is_set():
            await self.poll()
            try:
                await asyncio.wait_for(self.stop_event.wait(),timeout=self.config.get('monitor_seconds',120))
            except TimeoutError:
                pass

    async def monitor_comments(self):
        while not self.stop_event.is_set():
            try:
                await asyncio.to_thread(self.comment_replies.poll,lambda:
                    self.config.get('comment_replies_enabled',False) and self.config.get('comments_enabled',False)
                    and not self.state.snapshot().get('live_session') and not self.stop_event.is_set())
            except Exception as exc:
                self.log('comment_read_unavailable',error_type=type(exc).__name__)
            try:await asyncio.wait_for(self.stop_event.wait(),timeout=60)
            except TimeoutError:pass

    async def fenced(self,function,*args,epoch,**kwargs):
        cost_kind=(('live' if args[1].kind=='live_room' else 'video') if function==self.engine.observe
            else 'video' if function==getattr(self.engine,'select_video',None) else None)
        task=asyncio.create_task(asyncio.to_thread(function,*args,**kwargs))
        while not task.done():
            if self.state.snapshot()['watch_epoch']!=epoch or self.stop_event.is_set():
                self.retired.add(task)
                def finish(t):
                    self.retired.discard(t)
                    try:
                        result=t.result()
                        if cost_kind:self.record_calls(result,cost_kind)
                        path=getattr(result,'path',None)
                        if path:
                            self.delete_clip(path)
                    except BaseException:
                        pass
                task.add_done_callback(finish)
                return STALE
            await asyncio.sleep(.05)
        result=await task
        if cost_kind:self.record_calls(result,cost_kind)
        if self.state.snapshot()['watch_epoch']!=epoch:
            path=getattr(result,'path',None)
            if path:self.delete_clip(path)
            return STALE
        return result

    def record_calls(self,result,kind):
        with self.database.connect(write=True) as db:
            for request_id in getattr(result,'request_ids',()):
                db.execute('INSERT OR IGNORE INTO watcher_calls VALUES (?,?)',(request_id,kind))
        if hasattr(self.engine,'role_events_for'):
            for event in self.engine.role_events_for(getattr(result,'request_ids',())):
                self.log('model_role',**event)
        child_status=getattr(result,'child_status','not_requested')
        if child_status!='not_requested':
            self.log('child_dispatch',role=getattr(result,'child_role',None),status=child_status)

    def delete_clip(self,path):
        path=Path(path).resolve()
        root=(self.root/'clips').resolve()
        if root in path.parents:
            path.unlink(missing_ok=True)

    async def close_recorder(self):
        if self.recorder:
            recorder,self.recorder=self.recorder,None
            await asyncio.to_thread(recorder.stop)
            self.recorder_epoch=None

    async def choose_video(self,epoch):
        items=await self.fenced(self.platform.get_recommendations,limit=6,epoch=epoch)
        if items is STALE:return
        with self.database.connect() as db:
            seen={r[0] for r in db.execute("SELECT bvid FROM video_history WHERE status IN ('finished','skipped','sampled','unavailable')")}
            rows=db.execute("""SELECT h.summary, s.topic FROM video_history h LEFT JOIN video_selection_hints s
                ON h.bvid=s.bvid WHERE h.observations>0 AND h.summary!='' ORDER BY h.updated_at DESC LIMIT 8""").fetchall()
        labels=[r['topic'] or topic(dict(title=r['summary'])) for r in rows]
        explore=exploration_required(labels)
        popular=[]
        available=[i for i in items or [] if i.get('bvid') not in seen and topic(i)=='other']
        if len(available)<6 and hasattr(self.platform,'get_popular'):
            try:
                page=self.state.snapshot().get('exploration_page',1)
                self.state.change(lambda s:s.update(exploration_page=page%3+1))
                popular=await self.fenced(self.platform.get_popular,limit=20,page=page,epoch=epoch)
            except Exception as exc:
                self.log('exploration_source_unavailable',**error_details(exc))
            if popular is STALE:return
        items=candidates(items or [],popular,seen,explore)
        if explore and len(labels)>=2 and labels[0]==labels[1] and labels[0].startswith('category:'):
            items=[i for i in items if 'category:'+str(i.get('category_id') or i.get('category'))!=labels[0]]
        if not items:
            self.log('recommendations_exhausted')
            self.state.change(lambda s:s.update(next_observation_at=max(s.get('next_observation_at',0),self.clock()+120)))
            return
        choice=await self.fenced(self.engine.select_video,items,recent_video_history=self.recent_video_history(),epoch=epoch)
        if choice is STALE:return
        if choice.bvid is None:
            self.schedule_observation(False)
            self.log('video_selection_quiet')
            return
        item=next((item for item in items if item['bvid']==choice.bvid),None)
        if item is None:raise ValueError('selection outside actual recommendations')
        reason=str(choice.reason)[:240]
        def select(s):
            if s['watch_epoch']==epoch and not s['live_session']:
                s.update(video=dict(bvid=item['bvid'],title=str(item.get('title','') or '')[:120],position=0,duration=item.get('duration') or None,observations=0),
                         state='WATCHING_VIDEO',watch_epoch=epoch+1)
        self.state.change(select)
        with self.database.connect(write=True) as db:
            db.execute('''INSERT INTO video_history(bvid,selected_at,updated_at,selection_reason) VALUES (?,?,?,?)
                ON CONFLICT(bvid) DO UPDATE SET updated_at=excluded.updated_at,selection_reason=excluded.selection_reason''',
                (item['bvid'],self.clock(),self.clock(),reason))
        with self.database.connect(write=True) as db:
            db.execute('INSERT OR REPLACE INTO video_selection_hints VALUES (?,?,?)',
                       (item['bvid'],('category:'+str(item.get('category_id') or item.get('category')))
                        if item['selection_topic']=='other' else item['selection_topic'],item.get('source','recommendations')))
        self.log('video_selected',bvid=item['bvid'],selection_topic=item['selection_topic'],exploration=explore)
        self._refresh_presence()

    def recent_video_history(self):
        with self.database.connect() as db:
            rows=db.execute("SELECT * FROM video_history WHERE observations>0 AND summary!='' ORDER BY updated_at DESC LIMIT 8").fetchall()
        records=[]
        for row in rows:
            coverage=json.loads(row['coverage'])
            if not coverage or not row['summary']:continue
            record=dict(bvid=row['bvid'],interest=row['interest'],summary=row['summary'][:160],coverage=coverage[-4:])
            if len(json.dumps([*records,record],ensure_ascii=False,separators=(',',':')))>1200:continue
            records.append(record)
        return records

    def remember_video(self,s,result,chunk):
        bvid=s['video']['bvid']
        evidence={e.id:e for e in result.observation.evidence}
        summary_ids=tuple(getattr(result,'summary_evidence_ids',()))
        summary=str(getattr(result,'summary',''))[:400] if summary_ids and set(summary_ids)<=set(evidence) else ''
        coverage=[dict(start=e.media_start,end=e.media_end,modality=e.modality) for e in evidence.values()
            if e.modality in {'audio','video','subtitle','mixed'}]
        interest=getattr(result,'interest','neutral')
        if interest not in {'high','neutral','low'}:interest='neutral'
        action=getattr(result,'next_action','continue')
        if action not in {'continue','skip'}:action='continue'
        with self.database.connect(write=True) as db:
            old=db.execute('SELECT * FROM video_history WHERE bvid=?',(bvid,)).fetchone()
            count=(old['observations'] if old else s['video'].get('observations',0))+1
            title=str(s['video'].get('title','') or '').strip()[:80]
            combined_summary='\n'.join(filter(None,((old['summary'] if old else ''),summary)))[-1200:]
            if title and combined_summary and f'《{title}》' not in combined_summary:
                prefix=f'《{title}》'
                combined_summary=prefix+combined_summary[:max(0,1200-len(prefix))]
            combined_coverage=(json.loads(old['coverage']) if old else [])+coverage
            combined_coverage=combined_coverage[-32:]
            all_ids=list(dict.fromkeys((json.loads(old['evidence_ids']) if old else [])+list(evidence)))
            db.execute('''INSERT INTO video_history VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(bvid) DO UPDATE SET updated_at=excluded.updated_at,observations=excluded.observations,
                interest=excluded.interest,decision=excluded.decision,summary=excluded.summary,
                coverage=excluded.coverage,evidence_ids=excluded.evidence_ids,status=excluded.status''',
                (bvid,old['selected_at'] if old else self.clock(),self.clock(),old['selection_reason'] if old else '恢复既有视频，理由不代表已观察',
                 count,interest,action,combined_summary,json.dumps(combined_coverage),json.dumps(all_ids),'continuing'))
        if summary and all_ids:
            scope=Scope(self.account,'video',bvid)
            note_id='video-summary:'+bvid
            with self.memory.database.connect() as db:
                previous=db.execute('SELECT revision FROM notes WHERE id=?',(note_id,)).fetchone()
            self.memory.revise(scope,combined_summary,all_ids,note_id=note_id,expected_revision=previous[0] if previous else 0)
        self.state.change(lambda current:current['video'].update(position=chunk.end,observations=count,session_id=chunk.session_id,failures=0,
                interest=interest,comment_chunks=current['video'].get('comment_chunks',0)+1,
                comment_coverage=[*current['video'].get('comment_coverage',[]),[chunk.start,chunk.end]])
            if current['watch_epoch']==s['watch_epoch'] and current['video'] else None)
        return count,action

    def end_video(self,s,status):
        if self.state.snapshot()['watch_epoch']!=s['watch_epoch']:return
        bvid=s['video']['bvid']
        with self.database.connect(write=True) as db:
            db.execute('''INSERT INTO video_history(bvid,selected_at,updated_at,selection_reason,status) VALUES (?,?,?,?,?)
                ON CONFLICT(bvid) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at''',
                (bvid,self.clock(),self.clock(),'恢复既有视频；状态不代表已观察',status))
        self.log('video_'+status,bvid=bvid)
        self.state.change(lambda current:current.update(video=None,state='BROWSING',watch_epoch=current['watch_epoch']+1)
            if current['watch_epoch']==s['watch_epoch'] else None)
        self._refresh_presence()

    async def clip(self,s):
        epoch=s['watch_epoch']; output=self.root/'clips'
        output.mkdir(exist_ok=True)
        if s['live_session']:
            if self.recorder_epoch!=epoch:
                await self.close_recorder()
                target=await self.fenced(self.platform.resolve_live,self.room,epoch=epoch)
                if target is STALE:return STALE
                if self.recorder_factory is None:raise RuntimeError('continuous recorder required')
                self.recorder=self.recorder_factory(target,output,duration=self.config.get('chunk_seconds',30),max_queue=3)
                self.recorder.start(); self.recorder_epoch=epoch
                self.recorder_offset=s['live_position']
                self.recorder_skips=0
                if self.recorder_offset:
                    self.log('capture_gap',reason='recorder_restart_unknown_wall_gap',
                        wall_time=self.clock(),epoch=epoch,known_coverage=False)
            chunk=await self.fenced(self.recorder.next_clip,timeout=45,epoch=epoch)
            if chunk is STALE:return STALE
            chunk=replace(chunk,start=chunk.start+self.recorder_offset,end=chunk.end+self.recorder_offset)
            def advance(current):
                if current['watch_epoch']==epoch:current['live_position']=max(current['live_position'],chunk.end)
            self.state.change(advance)
            gaps=self.recorder.drain_skipped() if hasattr(self.recorder,'drain_skipped') else self.recorder.skipped[self.recorder_skips:]
            for gap in gaps:
                translated=dict(gap)
                for key in ('start','end'):
                    if key in translated:translated[key]+=self.recorder_offset
                self.log('capture_gap',**translated,epoch=epoch)
            self.recorder_skips=len(self.recorder.skipped)
            return chunk
        await self.close_recorder()
        if self.video_target_epoch!=epoch:
            target=await self.fenced(self.platform.resolve_video,s['video']['bvid'],epoch=epoch)
            if target is STALE:return STALE
            self.video_target,self.video_target_epoch=target,epoch
            self.state.change(lambda current:current['video'].update(uploader_id=getattr(target,'uploader_id',''),
                comment_coverage=[],comment_chunks=0) if current['watch_epoch']==epoch and current['video'] else None)
            target_title=str(getattr(target,'title','') or '')[:120]
            if target_title:
                self.state.change(lambda current:current['video'].update(title=target_title)
                    if current['watch_epoch']==epoch and current['video'] and not current['video'].get('title') else None)
            if target.duration:
                self.state.change(lambda current:current['video'].update(duration=target.duration,session_id=target.session_id)
                    if current['watch_epoch']==epoch and current['video'] else None)
        self.cancel_capture=threading.Event()
        duration=self.config.get('chunk_seconds',30)
        if self.video_target.duration:
            duration=min(duration,max(1,self.video_target.duration-s['video']['position']))
        return await self.fenced(self.collector.capture,self.video_target,output,
            duration=duration,offset=s['video']['position'],
            cancel_event=self.cancel_capture,epoch=epoch)

    async def step(self):
        if self.clock()>=self.next_retention_check:
            result=prune_activity(self.database,self.clock(),scope='watcher')
            self.next_retention_check=(self.clock()+3600 if result['state']=='failed' else result['next_at'])
        s=self.state.snapshot(); epoch=s['watch_epoch']
        if s['state']=='PAUSED_BUDGET':
            # Recover a pause persisted by older versions. Admission checks
            # below still apply before any paid work.
            self.state.change(lambda current:current.update(paused_day=None,state=
                'WATCHING_PRIORITY_LIVE' if current['live_session'] else 'WATCHING_VIDEO' if current['video'] else 'BROWSING'))
            self.decision_enabled=True
            return 'retry_budget'
        if not s['live_session'] and not self.observation_due(s):
            self.state.change(lambda current:current.update(understanding_state='WAITING_VIDEO_BUDGET'))
            return 'waiting_video_budget'
        if not s['live_session'] and not s['video']:
            if not self.selection_round_affordable(s):
                self.state.change(lambda current:current.update(understanding_state='WAITING_VIDEO_BUDGET'))
                return 'waiting_video_budget'
            try:
                await self.choose_video(epoch)
            except BudgetPaused:
                return self.defer_budget(epoch)
            return 'selecting'
        duration=s['video'].get('duration') if s['video'] else None
        if duration and s['video']['position']>=duration-1:
            if duration>s['video']['position']:
                self.log('perception_gap',start=s['video']['position'],end=duration,reason='tail_under_one_second',epoch=epoch)
            self.end_video(s,'finished' if s['video'].get('observations',0)>0 else 'unavailable')
            return 'finished'
        chunk=None;attempted_observation=False
        try:
            chunk=await self.clip(s)
            if chunk is STALE:return 'stale_capture'
            if not self.observation_due(s):
                self.state.change(lambda current:current.update(understanding_state='REDUCED_BUDGET_PACING'))
                self.log('perception_gap',epoch=epoch,start=chunk.start,end=chunk.end,
                    reason='budget_pacing',modality=chunk.modality)
                return 'sample_skipped'
            scope=Scope(self.account,'live_room' if s['live_session'] else 'video',
                self.room if s['live_session'] else s['video']['bvid'])
            if s['video']:
                latest=self.state.snapshot()
                if latest['watch_epoch']!=epoch:return 'stale_observation'
                s=latest
            extra=dict(video_context=dict(bvid=s['video']['bvid'],position=s['video']['position'],
                observed_chunks=s['video'].get('observations',0),
                recent_comments=self.comments.recent_comments(self.account),
                comment_watch_eligible=sufficient_observation(
                    dict(s['video'],comment_chunks=s['video'].get('comment_chunks',0)+1,
                         comment_coverage=[*s['video'].get('comment_coverage',[]),[chunk.start,chunk.end]]))),recent_video_history=self.recent_video_history()) if s['video'] else {}
            if s['live_session'] and hasattr(self.platform, 'recent_live_danmaku'):
                try:
                    chat = await self.fenced(self.platform.recent_live_danmaku, self.room, epoch=epoch)
                    if chat is STALE:return 'stale_observation'
                    extra['live_context'] = chat
                except Exception:
                    # Public history is optional: its failure must not interrupt
                    # media observation or expose upstream response contents.
                    self.log('live_chat_unavailable', epoch=epoch)
            attempted_observation=True
            result=await self.fenced(self.engine.observe,chunk,scope,epoch,
                allow_decision=bool(self.decision_enabled if s['live_session'] else self.config.get('comments_enabled',False)),epoch=epoch,**extra)
            if result is STALE:return 'stale_observation'
            # Fencing and appends are synchronous on the event loop: no target
            # transition can interleave between this check and evidence commit.
            if self.state.snapshot()['watch_epoch']!=epoch:return 'stale_observation'
            if s['live_session']:
                self.log('live_decision',status=getattr(result,'live_decision_status','not_requested'),epoch=epoch)
            if not result.observation.evidence:
                self.log('empty_observation',epoch=epoch,status=result.status)
                raise ValueError('empty observation')
            for evidence in result.observation.evidence:
                if evidence.scope!=scope or evidence.session_id!=chunk.session_id:
                    raise ValueError('model evidence source mismatch')
                self.memory.append(evidence)
            if s['live_session'] and result.summary and result.summary_evidence_ids:
                ids={e.id for e in result.observation.evidence}
                if set(result.summary_evidence_ids)<=ids:
                    self.memory.revise(scope,result.summary,list(result.summary_evidence_ids))
            if s['video']:self.video_failures.pop(s['video']['bvid'],None)
            if result.status=='invalid_observation':raise ValueError('invalid observation')
            count,action=self.remember_video(s,result,chunk) if s['video'] else (0,'continue')
            self.log('observed',epoch=epoch,start=chunk.start,end=chunk.end,
                modality=chunk.modality,evidence_count=len(result.observation.evidence),status=result.status)
            self.state.change(lambda current:current.update(understanding_state='SAMPLED',last_error=None))
            self.schedule_observation(bool(s['live_session']))
            if result.candidate and s['live_session']:
                outcome=await asyncio.to_thread(self.executor.dispatch,result.candidate,self.state.snapshot,
                    self.platform.send_danmaku,account_id=self.account,now=self.clock(),clock=self.clock)
                self.log('danmaku',outcome=outcome,epoch=epoch)
            if s['video']:
                comment_status=getattr(result,'comment_status','silent')
                if comment_status in {'silent','ready','expired','low_confidence','invalid','disabled'}:
                    self.log('video_comment_decision',status=comment_status,epoch=epoch)
                candidate=getattr(result,'comment_candidate',None)
                if candidate:
                    outcome=await asyncio.to_thread(self.comments.dispatch,candidate,self.state.snapshot,
                        self.platform.send_video_comment,account_id=self.account,clock=self.clock)
                    self.log('video_comment',outcome=outcome,epoch=epoch)
                # Dispatch while the observed target is still active; only then
                # apply skip/cap/end, with the same epoch fence as above.
                duration=self.state.snapshot().get('video') or {}
                end=duration.get('duration')
                if action=='skip':self.end_video(s,'skipped')
                elif end and chunk.end>=end-.2:self.end_video(s,'finished')
                elif count>=min(4,max(1,self.config.get('max_video_chunks',4))):self.end_video(s,'sampled')
            self._refresh_presence()
            return 'observed'
        except Exception as exc:
            if isinstance(exc,BudgetPaused):
                return self.defer_budget(epoch)
            if self.state.snapshot()['watch_epoch']!=epoch:return 'stale_capture'
            if attempted_observation:self.schedule_observation(bool(s['live_session']))
            self.video_target_epoch=None
            self.log('watch_error',**error_details(exc),epoch=epoch)
            self.state.change(lambda current:current.update(last_error=type(exc).__name__,understanding_state='UNAVAILABLE'))
            if s['live_session']:await self.close_recorder()
            elif s['video']:
                bvid=s['video']['bvid']
                current=self.state.snapshot()['video'] or {}
                failures=current.get('failures',0)+1
                self.state.change(lambda value:value['video'].update(failures=failures)
                    if value['watch_epoch']==epoch and value['video'] else None)
                if failures>=3:
                    self.end_video(s,'unavailable')
            return 'error'
        finally:
            if chunk is not None and chunk is not STALE:self.delete_clip(chunk.path)

    def cost_profile(self,live=True):
        scoped=self.config.get('budget_scope')==AUTONOMOUS_VIDEO_ONLY
        if scoped and live:
            return None
        now=self.clock()
        local=new_york_date(now)
        day=local.strftime('%Y-%m-%d'); month=local.strftime('%Y-%m')
        days=calendar.monthrange(local.year,local.month)[1]
        monthly_limit=float(self.config.get('monthly_usd','10'))
        daily_limit=min(float(self.config.get('daily_usd','4')),monthly_limit/days)
        hours=float(self.config.get('live_guaranteed_hours',2))
        reserve=float(self.config.get('live_daily_reserve_usd',min(.33*hours,daily_limit/2)))
        reply_reserve=float(self.config.get('reply_daily_reserve_usd',0))
        if not scoped and (not 0<hours<=24 or not 0<reserve<daily_limit
                           or not 0<=reply_reserve<daily_limit-reserve):
            raise ValueError('invalid live budget guarantee')
        with self.database.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='budget'").fetchone():
                return None
            query="""SELECT b.day,b.month,b.state,COALESCE(c.kind,
                CASE WHEN b.id LIKE 'bili-model-video-%' THEN 'video'
                WHEN b.id LIKE 'bili-model-live-%' THEN 'live'
                WHEN b.id LIKE 'bili-model-comment-reply-%' THEN 'reply' ELSE 'video' END) AS kind,
                CASE WHEN b.state='settled' THEN b.actual ELSE b.reserved END AS cost
                FROM budget b LEFT JOIN watcher_calls c ON c.request_id=b.id"""
            if scoped:
                query+=""" LEFT JOIN budget_lanes l ON l.request_id=b.id
                    WHERE COALESCE(l.lane,'legacy') IN ('legacy','autonomous_video')"""
            rows=db.execute(query).fetchall()
            samples=db.execute("SELECT count(*) FROM watcher_events WHERE kind='observed'").fetchone()[0]+retained_observation_count(db)
        today=sum(r['cost'] for r in rows if r['day']==day)/1_000_000
        monthly=sum(r['cost'] for r in rows if r['month']==month or r['state']!='settled')/1_000_000
        total=sum(r['cost'] for r in rows)/1_000_000
        average=max(.0004 if samples>=3 else .0015,total/max(1,samples))
        if scoped:
            daily_available,monthly_available=self.engine.spend_ledger.available(now,lane='autonomous_video')
            monthly_limit=min(monthly_limit,monthly+monthly_available/1_000_000)
            daily_limit=min(daily_limit,today+daily_available/1_000_000,monthly_limit/days)
            return dict(today=today,monthly=monthly,daily_soft=daily_limit,average=average,
                kind_today=today,kind_month=monthly,monthly_pool=monthly_limit,
                daily_limit=daily_limit,monthly_limit=monthly_limit)
        kind='live' if live else 'video'
        kind_today=sum(r['cost'] for r in rows if r['day']==day and r['kind']==kind)/1_000_000
        kind_month=sum(r['cost'] for r in rows if (r['month']==month or r['state']!='settled') and r['kind']==kind)/1_000_000
        live_today=sum(r['cost'] for r in rows if r['day']==day and r['kind']=='live')/1_000_000
        live_month=sum(r['cost'] for r in rows if (r['month']==month or r['state']!='settled') and r['kind']=='live')/1_000_000
        # Video cannot spend today's or future days' protected live allowance.
        # Live remains the priority and can use unspent video money after its guarantee.
        reply_today=sum(r['cost'] for r in rows if r['day']==day and r['kind']=='reply')/1_000_000
        reply_month=sum(r['cost'] for r in rows if (r['month']==month or r['state']!='settled') and r['kind']=='reply')/1_000_000
        protected_reply=max(reply_reserve*days,reply_month+reply_reserve*(days-local.day)+max(0,reply_reserve-reply_today))
        soft=max(0,daily_limit-max(reply_reserve,reply_today))
        if not live:soft=max(0,soft-max(reserve,live_today))
        remaining_live=reserve*(days-local.day)+max(0,reserve-live_today)
        protected_live=max(reserve*days,live_month+remaining_live)
        pool=max(0,monthly_limit-protected_reply-(0 if live else protected_live))
        return dict(today=today,monthly=monthly,daily_soft=soft,average=average,
            kind_today=kind_today,kind_month=kind_month,monthly_pool=pool,
            daily_limit=daily_limit,monthly_limit=monthly_limit,live_daily_reserve=reserve,
            live_guaranteed_hours=hours)

    def observation_due(self,s):
        if self.clock()<s.get('next_observation_at',0):return False
        profile=self.cost_profile(bool(s['live_session']))
        upper=self.video_request_limit('video-observe') if not s['live_session'] else 0
        return profile is None or (profile['today']+upper<profile['daily_limit'] and
            profile['monthly']+upper<profile['monthly_limit'] and
            profile['kind_today']+upper<profile['daily_soft'] and profile['kind_month']+upper<profile['monthly_pool'])

    def selection_round_affordable(self,s):
        profile=self.cost_profile(False)
        if profile is None:return True
        caps = self.video_request_limit('video-selection') + self.video_request_limit('video-observe')
        estimate=max(.002,profile['average']*2,caps)
        return (profile['kind_today']+estimate<=profile['daily_soft'] and
            profile['kind_month']+estimate<=profile['monthly_pool'] and
            profile['today']+estimate<=profile['daily_limit'] and
            profile['monthly']+estimate<=profile['monthly_limit'])

    def video_request_limit(self, purpose):
        if hasattr(self.engine, 'request_limit_for'):
            return float(self.engine.request_limit_for(purpose))
        return float(getattr(self.engine, 'request_limit', 0))

    def defer_budget(self,epoch):
        current=self.state.snapshot()
        if current['watch_epoch']!=epoch:return 'stale_observation'
        self.schedule_observation(bool(current['live_session']))
        # A pending reservation may settle before tomorrow. Reassess the
        # affected target within five minutes, still subject to admission.
        self.state.change(lambda s:s.update(next_observation_at=min(s['next_observation_at'],self.clock()+300),
            understanding_state='WAITING_LIVE_BUDGET' if s['live_session'] else 'WAITING_VIDEO_BUDGET'))
        self.log('budget_deferred',epoch=epoch)
        return 'budget_deferred'

    def schedule_observation(self,live):
        minimum=self.config.get('live_observation_seconds',90) if live else self.config.get('video_observation_seconds',180)
        profile=self.cost_profile(live)
        pacing_window=profile['live_guaranteed_hours']*3600 if live and profile else 86400
        pacing_pool=profile['live_daily_reserve'] if live and profile else (profile['daily_soft'] if profile else 0)
        interval=minimum
        if profile:
            now=self.clock()
            local=new_york_date(now)
            midnight=(local+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
            until_reset=max(1,midnight.timestamp()-now)
            remaining=min(profile['daily_limit']-profile['today'],profile['monthly_limit']-profile['monthly'],
                profile['daily_soft']-profile['kind_today'],profile['monthly_pool']-profile['kind_month'])
            window=until_reset
            if live:
                window=min(window,max(minimum,pacing_window-self.state.snapshot()['live_position']))
            affordable_rounds=max(1,remaining/profile['average'])
            interval=min(until_reset,max(minimum,window/affordable_rounds,
                profile['average']*pacing_window/max(.000001,pacing_pool)))
        self.state.change(lambda current:current.update(next_observation_at=self.clock()+interval))
        self.log('pacing',next_seconds=round(interval,2),live=live)

    async def run(self):
        await self.poll()
        monitor=asyncio.create_task(self.monitor())
        comments_monitor=asyncio.create_task(self.monitor_comments())
        consecutive_errors=0
        try:
            while not self.stop_event.is_set():
                try:
                    status=await self.step()
                except Exception as exc:
                    # Recommendation/selection errors occur before the per-clip
                    # handler; keep the monitor alive and apply the same backoff.
                    self.log('watch_error',**error_details(exc))
                    self.state.change(lambda current:current.update(last_error=type(exc).__name__,understanding_state='UNAVAILABLE'))
                    status='error'
                if status=='error':consecutive_errors+=1
                elif status in {'observed','sample_skipped'}:consecutive_errors=0
                current=self.state.snapshot()
                delay=(error_backoff(consecutive_errors) if status=='error'
                    else 30 if status=='waiting_video_budget'
                    else self.config.get('chunk_seconds',30) if current['video'] and status in {'observed','sample_skipped'}
                    else 1 if status not in {'error','paused_budget','selecting'} else 5)
                waiters=[asyncio.create_task(self.stop_event.wait()),asyncio.create_task(self.wake_event.wait())]
                try:await asyncio.wait(waiters,timeout=delay,return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in waiters:
                        if not task.done():task.cancel()
                    await asyncio.gather(*waiters,return_exceptions=True)
                    self.wake_event.clear()
        finally:
            self.stop_event.set()
            self.cancel_capture.set(); monitor.cancel()
            await asyncio.gather(monitor,comments_monitor,return_exceptions=True)
            await self.close_recorder()


def build(config):
    from bili_companion.platform.bilibili import BilibiliPlatform
    from bili_companion.media.capture import MediaCollector,LiveRecorder
    from bili_companion.cognition.gateway import GatewayClient
    from bili_companion.cognition.live import CognitionEngine
    from bili_companion.persona.loader import PersonaLoader
    from bili_companion.sync import SyncStore
    if (not 1<=config.get('chunk_seconds',30)<=30 or not 30<=config.get('monitor_seconds',120)<=120
            or config.get('live_observation_seconds',90)<60 or config.get('video_observation_seconds',180)<180):
        raise ValueError('invalid monitoring/capture/pacing interval')
    from bili_companion.cognition.profile import ActivatedProfile,load_profile
    from bili_companion.cognition.gateway import HYDROGEN
    from bili_companion.cognition.live import prices
    from bili_companion.cognition.routing import build_route, build_video_overlay, has_video_profiles
    if config.get('hydrogen_route') is not None:
        monthly,daily=ActivatedProfile.budgets(config,monthly_cap=96)
        client=None
        model_options={}
    elif config.get('vertex_model') == HYDROGEN:
        client=GatewayClient.for_hydrogen(config)
        monthly,daily=ActivatedProfile.budgets(config,monthly_cap=96)
        model_options=dict(observer_model=HYDROGEN,decider_model=HYDROGEN,price_resolver=prices,
            single_pass=True,max_output_tokens=1536,request_usd_limit='.04',
            call_provider='vertex' if has_video_profiles(config) else None,
            decision_interval=max(90,config.get('cooldown_seconds',90)))
    elif config.get('provider_profile'):
        profile=load_profile(config['provider_profile'])
        from bili_companion.cognition.openai_provider import OpenAICompatibleClient
        monthly,daily=profile.budgets(config)
        client=OpenAICompatibleClient(profile.base_url,api_key_file=profile.api_key_file,
            input_token_upper_bound=profile.raw['input_token_upper_bound'],media_mode=profile.raw['media_mode'],
            output_limit_parameter=profile.raw['output_limit_parameter'],reasoning_effort=profile.raw.get('reasoning_effort'))
        model_options=dict(observer_model=profile.observer_model,decider_model=profile.decider_model,
            price_resolver=profile.prices,child_roles_enabled=profile.raw['child_roles_enabled'],
            single_pass=profile.raw.get('single_pass',False),max_output_tokens=profile.raw['max_output_tokens'],
            request_usd_limit=profile.raw.get('per_request_usd_limit','.02'))
        if model_options['single_pass']:
            model_options['decision_interval']=max(90,config.get('cooldown_seconds',90))
    else:
        for field in ('gateway_key_file','gateway_ca_file'):
            if not Path(config[field]).is_file():raise ValueError('gateway file unavailable')
        monthly,daily=str(config.get('monthly_usd','10')),str(config.get('daily_usd','1'))
        from decimal import Decimal
        if not 0<Decimal(monthly)<=10 or not 0<Decimal(daily)<=1:
            raise ValueError('unverified deployment budget exceeds authorized limit')
        client=GatewayClient(config['gateway_key_file'],config['gateway_ca_file'])
        model_options={}
    PersonaLoader(config.get('persona_dir')).load()
    db=Database(Path(config['state_dir'])/'watcher.sqlite3')
    memory=MemoryStore(Database(config['native_database']))
    ledger=BudgetLedger(db,monthly=monthly,daily=daily,
        budget_scope=config.get('budget_scope'),lane='other')
    from bili_companion.cognition.video_budget import classify_budget_history
    classify_budget_history(ledger, config['state_dir'])
    budget_lanes={purpose:'autonomous_video' for purpose in (
        'video-selection','video-observe','video-child-expression','video-child-memory')}
    from bili_companion.policy.budget import micros
    ledger.reply_daily=micros(config.get('reply_daily_reserve_usd',0))
    bridge=SyncStore(Database(config['bridge_database']),endpoint='bilibili',
        local_id='hakimi-bilibili',peer_id='hakimi-qq')
    if config.get('hydrogen_route') is not None:
        engine=build_route(config,ledger,memory,persona_loader=PersonaLoader(config.get('persona_dir')),
            bridge_store=bridge,budget_lanes=budget_lanes)
    else:
        engine=CognitionEngine(client,ledger,memory,persona_loader=PersonaLoader(config.get('persona_dir')),
            bridge_store=bridge,budget_lanes=budget_lanes,**model_options)
        engine=build_video_overlay(config,engine)
    return Watcher(config,BilibiliPlatform(config['auth_state_dir']),MediaCollector(),engine,
        recorder_factory=LiveRecorder)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--status',action='store_true')
    args=parser.parse_args(); config=json.loads(Path(args.config).read_text(encoding='utf-8'))
    if args.status:
        print(json.dumps(WatchState(Database(Path(config['state_dir'])/'watcher.sqlite3'),str(config['priority_room'])).snapshot()))
        return
    watcher=build(config)
    async def serve():
        loop=asyncio.get_running_loop()
        for sig in (signal.SIGTERM,signal.SIGINT):
            try:loop.add_signal_handler(sig,watcher.stop_event.set)
            except (NotImplementedError,ValueError):pass
        await watcher.run()
    asyncio.run(serve())


if __name__=='__main__':main()
