import asyncio
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from bili_companion.cognition.live import BudgetPaused
from bili_companion.domain import Evidence,Observation,Candidate
from bili_companion.media.capture import MediaChunk
from bili_companion.runtime.watcher import Watcher,error_backoff,error_details


class FakePlatform:
    def __init__(self):self.sent=[]; self.fail_send=False;self.comments=[]
    def live_status(self,room):return dict(is_live=False,checked_at=1_800_000_000)
    def get_recommendations(self,limit=6):return [dict(bvid='BVsynthetic1',title='仅用于测试的标题',duration=90,category='科学')]
    def resolve_video(self,bvid):return SimpleNamespace(identity=bvid,session_id='vod-session',duration=90)
    def resolve_live(self,room):return SimpleNamespace(identity=room,session_id='live-session',duration=None)
    def send_danmaku(self,room,text):
        self.sent.append((room,text))
        if self.fail_send:raise TimeoutError()
        return SimpleNamespace(status='sent')
    def send_video_comment(self,bvid,text,*,before_post=None):
        if before_post is not None and not before_post():return SimpleNamespace(status='rejected',code='cancelled_before_post')
        self.comments.append((bvid,text))
        if self.fail_send:raise TimeoutError()
        return SimpleNamespace(status='sent')


class FakeCollector:
    def capture(self,target,output_dir,duration=30,offset=0,cancel_event=None):
        output=Path(output_dir)/('synthetic-'+uuid.uuid4().hex+'.mp4')
        output.parent.mkdir(parents=True,exist_ok=True); output.write_bytes(b'synthetic')
        return MediaChunk(output,offset,offset+duration,1_799_999_999,'audio',target.identity,target.session_id,True,False)


class FakeRecorder:
    def __init__(self,target,output_dir,**kwargs):self.target=target;self.output_dir=output_dir;self.skipped=[];self.closed=False
    def start(self):return self
    def next_clip(self,timeout=45):return FakeCollector().capture(self.target,self.output_dir)
    def stop(self):self.closed=True


class FakeEngine:
    def __init__(self):
        self.started=threading.Event();self.release=None;self.budget=False
        self.selection_calls=0;self.next_action='continue';self.comment=False;self.empty=False
        self.history=[]
    def select_video(self,items,*,recent_video_history=()):
        self.selection_calls+=1;self.history=recent_video_history
        return SimpleNamespace(bvid=items[0]['bvid'],reason='符合人设的候选',request_ids=())
    def observe(self,chunk,scope,watch_epoch,allow_decision=True,**kwargs):
        self.started.set()
        if self.release:self.release.wait(5)
        if self.budget:raise BudgetPaused()
        evidence=Evidence(uuid.uuid4().hex,scope,chunk.session_id,'实际音频测试依据',1_799_999_999,
            chunk.start,chunk.end,'audio','observation')
        candidate=Candidate(scope.identity,chunk.session_id,'听到啦',(evidence.id,),1_800_000_050,'same-key',watch_epoch) if allow_decision and scope.kind=='live_room' else None
        from bili_companion.policy.video_comments import VideoCommentCandidate
        comment=VideoCommentCandidate(scope.identity,'这一小段的音频测试依据很清晰',(evidence.id,),1_800_000_050,watch_epoch,chunk.session_id) if self.comment and scope.kind=='video' else None
        return SimpleNamespace(status='observed',observation=Observation(()) if self.empty else Observation((evidence,)),candidate=candidate,
            interest='high',next_action=self.next_action,summary='实际音频测试依据',summary_evidence_ids=(evidence.id,),comment_candidate=comment,
            comment_status='ready' if comment else 'silent')


class WatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_entry_reaches_step_with_background_monitors(self):
        stepped = asyncio.Event()

        async def stop_after_step():
            stepped.set()
            self.watcher.stop_event.set()
            return 'selecting'

        self.watcher.step = stop_after_step
        await asyncio.wait_for(self.watcher.run(), timeout=4)
        self.assertTrue(stepped.is_set())

    async def asyncSetUp(self):
        root=Path(__file__).resolve().parents[1]/'state'/'test_tmp';root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root)
        self.root=Path(self.temp.name)
        self.platform=FakePlatform();self.engine=FakeEngine()
        self.now=1_800_000_000
        self.config=dict(account_id='hakimi-bilibili',priority_room='8729095131',state_dir=str(self.root/'watch'),
            native_database=str(self.root/'native.sqlite3'),send_enabled=True)
        self.watcher=Watcher(self.config,self.platform,FakeCollector(),self.engine,recorder_factory=FakeRecorder,
            clock=lambda:self.now)

    async def asyncTearDown(self):
        if self.engine.release:self.engine.release.set()
        if self.watcher.retired:await asyncio.gather(*self.watcher.retired,return_exceptions=True)
        await self.watcher.close_recorder()
        self.temp.cleanup()

    def live(self,value,at=1_800_000_000):
        return self.watcher.apply_status(dict(is_live=value,live_session_id='live-session',checked_at=at))

    async def test_public_chat_is_optional_and_source_failure_keeps_media_observing(self):
        self.live(True)
        original = self.engine.observe
        captured = []
        def observe(*args, **kwargs):
            captured.append(kwargs.get('live_context'))
            return original(*args, **kwargs)
        self.engine.observe = observe
        self.platform.recent_live_danmaku = lambda room: [dict(text='节奏好听', observed_at=self.now-5)]
        self.assertEqual(await self.watcher.step(), 'observed')
        self.assertEqual(captured[-1][0]['text'], '节奏好听')
        def unavailable(room):raise RuntimeError('upstream private diagnostic')
        self.platform.recent_live_danmaku = unavailable
        self.now += 300
        self.assertEqual(await self.watcher.step(), 'observed')
        self.assertIsNone(captured[-1])

    async def test_live_observation_sees_own_recent_danmaku(self):
        self.live(True)
        with self.watcher.executor.database.connect(write=True) as db:
            db.execute("INSERT INTO danmaku_attempts VALUES (?,?,?,?,?,?,?)",
                (self.watcher.account, self.watcher.room, 'older-session', 'k1', '上次那句', self.now-600, 'sent'))
            db.execute("INSERT INTO danmaku_attempts VALUES (?,?,?,?,?,?,?)",
                (self.watcher.account, self.watcher.room, 'older-session', 'k2', '被拒的那句', self.now-500, 'rejected'))
        original = self.engine.observe
        captured = []
        def observe(*args, **kwargs):
            captured.append(kwargs.get('own_recent_danmaku'))
            return original(*args, **kwargs)
        self.engine.observe = observe
        self.assertEqual(await self.watcher.step(), 'observed')
        self.assertEqual([item['text'] for item in captured[-1]], ['上次那句'])

    async def test_real_fake_loop_observation_preemption_double_offline_resume(self):
        self.assertEqual(await self.watcher.step(),'selecting')
        self.assertEqual(await self.watcher.step(),'observed')
        self.assertEqual(self.watcher.state.snapshot()['video']['position'],30)
        live=self.live(True)
        self.assertIsNone(live['video']);self.assertEqual(live['bookmark']['position'],30)
        self.assertEqual(await self.watcher.step(),'observed')
        self.assertEqual(len(self.platform.sent),1)
        self.live(False,1_800_000_001)
        self.live(None,1_800_000_002)
        self.assertIsNotNone(self.live(False,1_800_000_003)['live_session'])
        resumed=self.live(False,1_800_000_004)
        self.assertIsNone(resumed['live_session']);self.assertEqual(resumed['video']['position'],30)
        self.assertEqual(await self.watcher.step(),'waiting_video_budget')
        self.assertEqual(self.watcher.state.snapshot()['video']['position'],30)

    async def test_old_model_result_cannot_write_native_evidence_or_send(self):
        await self.watcher.step()
        self.engine.release=threading.Event()
        task=asyncio.create_task(self.watcher.step())
        for _ in range(100):
            if self.engine.started.is_set():break
            await asyncio.sleep(.01)
        self.live(True)
        self.assertEqual(await asyncio.wait_for(task,2),'stale_observation')
        self.engine.release.set()
        await asyncio.gather(*self.watcher.retired,return_exceptions=True)
        with self.watcher.memory.database.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM evidence').fetchone()[0],0)
        self.assertEqual(self.platform.sent,[])
        self.assertEqual(self.watcher.state.snapshot()['bookmark']['position'],0)

    async def test_unknown_transport_is_at_most_once_even_with_same_candidate(self):
        self.platform.fail_send=True
        self.live(True)
        await self.watcher.step()
        with self.watcher.memory.database.connect() as db:
            evidence_id=db.execute('SELECT id FROM evidence').fetchone()[0]
        s=self.watcher.state.snapshot()
        candidate=Candidate('8729095131','live-session','听到啦',(evidence_id,),1_800_000_050,'same-key',s['watch_epoch'])
        self.assertEqual(self.watcher.executor.dispatch(candidate,self.watcher.state.snapshot,
            self.platform.send_danmaku,account_id='hakimi-bilibili',now=1_800_000_000),'duplicate')
        self.assertEqual(len(self.platform.sent),1)
        with self.watcher.database.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM danmaku_attempts').fetchone()[0],'unknown')

    async def test_video_budget_deferral_allows_priority_live_to_start(self):
        await self.watcher.step()
        self.engine.budget=True
        self.assertEqual(await self.watcher.step(),'budget_deferred')
        self.assertGreater(self.watcher.state.snapshot()['next_observation_at'],self.now)
        self.assertEqual(self.live(True)['state'],'WATCHING_PRIORITY_LIVE')
        with self.watcher.memory.database.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM evidence').fetchone()[0],0)
        self.assertEqual(self.platform.sent,[])
        self.engine.budget=False
        self.assertEqual(await self.watcher.step(),'observed')

    async def test_pacing_spreads_fewer_affordable_video_rounds_over_remaining_day(self):
        profile=dict(today=0,monthly=0,kind_today=0,kind_month=0,daily_limit=2,monthly_limit=60,
            daily_soft=1,monthly_pool=25,average=.01,live_daily_reserve=.99,live_guaranteed_hours=2)
        with patch.object(self.watcher,'cost_profile',return_value=profile):
            self.watcher.schedule_observation(False)
            first=self.watcher.state.snapshot()['next_observation_at']
            profile.update(kind_today=.98,today=.98)
            self.watcher.schedule_observation(False)
            self.assertGreater(self.watcher.state.snapshot()['next_observation_at'],first)

    async def test_epoch_mismatch_rejects_dispatch_before_any_external_send(self):
        self.live(True)
        await self.watcher.step()
        with self.watcher.database.connect() as db:
            row=db.execute('SELECT * FROM danmaku_attempts').fetchone()
        state=self.watcher.state.snapshot()
        candidate=Candidate('8729095131','live-session','旧候选',('unused',),1_800_000_050,'different',state['watch_epoch']-1)
        result=self.watcher.executor.dispatch(candidate,self.watcher.state.snapshot,self.platform.send_danmaku,
            account_id='hakimi-bilibili',now=1_800_000_000)
        self.assertEqual(result,'inactive_session');self.assertEqual(len(self.platform.sent),1)

    async def test_executor_rejects_fresh_low_confidence_or_inferred_evidence(self):
        self.live(True)
        s=self.watcher.state.snapshot()
        from bili_companion.domain import Scope
        scope=Scope('hakimi-bilibili','live_room','8729095131')
        for index,attribution,confidence in ((1,'inference',1),(2,'observation',.79)):
            evidence=Evidence(str(index),scope,'live-session','依据',1_799_999_999,0,4,'audio',attribution,confidence)
            self.watcher.memory.append(evidence)
            candidate=Candidate('8729095131','live-session','候选',(evidence.id,),1_800_000_050,str(index),s['watch_epoch'])
            result=self.watcher.executor.dispatch(candidate,self.watcher.state.snapshot,self.platform.send_danmaku,
                account_id='hakimi-bilibili',now=1_800_000_000)
            self.assertEqual(result,'stale_evidence')
        self.assertEqual(self.platform.sent,[])

    async def test_dynamic_pacing_uses_monthly_daily_soft_envelope(self):
        from bili_companion.policy.budget import BudgetLedger
        ledger=BudgetLedger(self.watcher.database,monthly='10',daily='1')
        ledger.reserve('synthetic','.30',1_800_000_000);ledger.settle('synthetic','.30')
        profile=self.watcher.cost_profile()
        self.assertLessEqual(profile['daily_soft'],1)
        self.assertFalse(self.watcher.observation_due(self.watcher.state.snapshot()))
        self.watcher.schedule_observation(True)
        self.assertGreater(self.watcher.state.snapshot()['next_observation_at'],1_800_000_090)

    async def test_video_pool_does_not_consume_live_daily_allocation(self):
        from bili_companion.policy.budget import BudgetLedger
        ledger=BudgetLedger(self.watcher.database,monthly='10',daily='1')
        ledger.reserve('vod-cost','.17',1_800_000_000);ledger.settle('vod-cost','.17')
        self.watcher.record_calls(SimpleNamespace(request_ids=('vod-cost',)),'video')
        state=self.watcher.state.snapshot()
        self.assertFalse(self.watcher.observation_due(state))
        self.live(True)
        self.assertTrue(self.watcher.observation_due(self.watcher.state.snapshot()))

    async def test_live_guarantee_assigns_every_remaining_dollar_to_video(self):
        from bili_companion.policy.budget import BudgetLedger
        from bili_companion.policy.budget import new_york_date
        import calendar
        self.config.update(monthly_usd='40',daily_usd='4',live_daily_reserve_usd='.66',live_guaranteed_hours=2)
        BudgetLedger(self.watcher.database,monthly='40',daily='4')
        days=calendar.monthrange(new_york_date(self.now).year,new_york_date(self.now).month)[1]
        video=self.watcher.cost_profile(False)
        self.assertAlmostEqual(video['daily_soft'],40/days-.66)
        self.assertAlmostEqual(video['monthly_pool'],40-.66*days)
        self.assertEqual(self.watcher.cost_profile(True)['monthly_pool'],40)
        self.watcher.schedule_observation(True)
        # Two hours, rather than a full day, funds the protected live samples.
        self.assertEqual(self.watcher.state.snapshot()['next_observation_at'],self.now+90)

    async def test_video_cannot_spend_live_guarantee_or_future_month_reserve(self):
        from bili_companion.policy.budget import BudgetLedger
        self.config.update(monthly_usd='40',daily_usd='4',live_daily_reserve_usd='.66')
        ledger=BudgetLedger(self.watcher.database,monthly='40',daily='4')
        ledger.reserve('vod','.68',self.now);ledger.settle('vod','.68')
        self.watcher.record_calls(SimpleNamespace(request_ids=('vod',)),'video')
        self.assertFalse(self.watcher.observation_due(self.watcher.state.snapshot()))
        self.assertFalse(self.watcher.selection_round_affordable(self.watcher.state.snapshot()))
        self.live(True)
        self.assertTrue(self.watcher.observation_due(self.watcher.state.snapshot()))
        with self.watcher.database.connect(write=True) as db:
            db.execute("UPDATE budget SET day='earlier-day',actual=21000000 WHERE id='vod'")
        self.live(False);self.live(False)
        self.assertFalse(self.watcher.observation_due(self.watcher.state.snapshot()))
        # Priority live may use spare video money but never bypass the total cap.
        self.live(True)
        self.assertTrue(self.watcher.observation_due(self.watcher.state.snapshot()))
        with self.watcher.database.connect(write=True) as db:db.execute("UPDATE budget SET actual=40000000 WHERE id='vod'")
        self.assertFalse(self.watcher.observation_due(self.watcher.state.snapshot()))

    async def test_live_overspend_still_protects_future_days(self):
        from bili_companion.policy.budget import BudgetLedger,new_york_date
        import calendar
        self.config.update(monthly_usd='40',daily_usd='4',live_daily_reserve_usd='.66')
        ledger=BudgetLedger(self.watcher.database,monthly='40',daily='4')
        ledger.reserve('live-history','.01',self.now);ledger.settle('live-history','.01')
        self.watcher.record_calls(SimpleNamespace(request_ids=('live-history',)),'live')
        with self.watcher.database.connect(write=True) as db:
            db.execute("UPDATE budget SET actual=20000000,day='earlier-day' WHERE id='live-history'")
        local=new_york_date(self.now)
        days=calendar.monthrange(local.year,local.month)[1]
        protected=max(.66*days,20+.66*(days-local.day+1))
        self.assertAlmostEqual(self.watcher.cost_profile(False)['monthly_pool'],40-protected)

    async def test_video_request_caps_and_unknown_liabilities_protect_live(self):
        from bili_companion.policy.budget import BudgetLedger
        self.config.update(monthly_usd='40',daily_usd='4',live_daily_reserve_usd='.66')
        self.engine.request_limit=.03
        ledger=BudgetLedger(self.watcher.database,monthly='40',daily='4')
        pool=self.watcher.cost_profile(False)['daily_soft']
        ledger.reserve('unclassified',str(pool-.04),self.now)
        # Unknown ownership is conservatively charged to video, including reservations.
        self.assertAlmostEqual(self.watcher.cost_profile(False)['kind_today'],pool-.04,places=5)
        self.assertTrue(self.watcher.observation_due(self.watcher.state.snapshot()))
        self.assertFalse(self.watcher.selection_round_affordable(self.watcher.state.snapshot()))
        with self.watcher.database.connect(write=True) as db:
            db.execute("UPDATE budget SET reserved=? WHERE id='unclassified'",(int((pool-.02)*1000000),))
        self.assertFalse(self.watcher.observation_due(self.watcher.state.snapshot()))
        self.live(True)
        self.assertTrue(self.watcher.observation_due(self.watcher.state.snapshot()))

    async def test_old_unsettled_liabilities_remain_in_current_pools(self):
        from bili_companion.policy.budget import BudgetLedger
        ledger=BudgetLedger(self.watcher.database,monthly='10',daily='1')
        ledger.reserve('old-unknown','.12',self.now)
        with self.watcher.database.connect(write=True) as db:
            db.execute("UPDATE budget SET day='2000-01-01',month='2000-01',state='unknown'")
        profile=self.watcher.cost_profile(False)
        self.assertEqual(profile['today'],0)
        self.assertEqual(profile['kind_today'],0)
        for key in ('monthly','kind_month'):
            self.assertAlmostEqual(profile[key],.12)

    async def test_incremental_live_and_reply_reserves_preserve_video_pool(self):
        from bili_companion.policy.budget import BudgetLedger
        self.config.update(monthly_usd='40',daily_usd='4',live_daily_reserve_usd='.66')
        BudgetLedger(self.watcher.database,monthly='60',daily='6')
        original=self.watcher.cost_profile(False)
        self.config.update(monthly_usd='60',daily_usd='6',live_daily_reserve_usd='.99',
            reply_daily_reserve_usd='.15',live_observation_seconds=60)
        increased=self.watcher.cost_profile(False)
        self.assertGreater(increased['daily_soft'],original['daily_soft'])
        self.assertGreater(increased['monthly_pool'],original['monthly_pool'])
        self.assertEqual(self.watcher.cost_profile(True)['live_daily_reserve'],.99)

    async def test_live_preemption_cancels_inflight_video_capture(self):
        await self.watcher.step()
        started=threading.Event()
        class InterruptCollector:
            def capture(inner,target,output_dir,**kwargs):
                started.set()
                if kwargs['cancel_event'].wait(3):raise RuntimeError('cancelled')
                raise AssertionError('capture was not interrupted')
        self.watcher.collector=InterruptCollector()
        task=asyncio.create_task(self.watcher.step())
        for _ in range(100):
            if started.is_set():break
            await asyncio.sleep(.01)
        self.live(True)
        self.assertEqual(await asyncio.wait_for(task,2),'stale_capture')
        self.assertTrue(self.watcher.cancel_capture.is_set())
        self.assertEqual(self.watcher.state.snapshot()['bookmark']['position'],0)

    async def test_selection_then_first_observation_and_budget_wait_never_advances(self):
        await self.watcher.step()
        self.assertEqual(self.engine.selection_calls,1)
        self.assertEqual(self.watcher.state.snapshot()['video']['observations'],0)
        self.watcher.state.change(lambda s:s.update(next_observation_at=self.now+500))
        original=self.watcher.collector.capture
        def forbidden(*args,**kwargs):raise AssertionError('budget wait captured media')
        self.watcher.collector.capture=forbidden
        self.assertEqual(await self.watcher.step(),'waiting_video_budget')

        self.assertEqual(self.watcher.state.snapshot()['video']['position'],0)
        self.assertEqual(self.engine.selection_calls,1)
        self.watcher.collector.capture=original
        self.watcher.state.change(lambda s:s.update(next_observation_at=0))
        self.assertEqual(await self.watcher.step(),'observed')
        self.assertEqual(self.watcher.state.snapshot()['video']['observations'],1)

    async def test_budget_wait_before_selection_replaces_stale_unavailable_status(self):
        self.watcher.observation_due=lambda _:True
        self.watcher.selection_round_affordable=lambda _:False
        self.watcher.state.change(lambda s:s.update(understanding_state='UNAVAILABLE'))
        self.assertEqual(await self.watcher.step(),'waiting_video_budget')
        self.assertEqual(self.watcher.state.snapshot()['understanding_state'],'WAITING_VIDEO_BUDGET')
        self.assertEqual(self.engine.selection_calls,0)

    async def test_skip_comments_after_memory_commit_and_exports_observed_history(self):
        self.engine.next_action='skip';self.engine.comment=True
        self.platform.resolve_video=lambda bvid:SimpleNamespace(identity=bvid,session_id='vod-session',duration=20,uploader_id='123')
        self.watcher.config['comments_enabled']=True;self.watcher.comments.enabled=True
        await self.watcher.step()
        self.assertEqual(await self.watcher.step(),'observed')
        self.assertIsNone(self.watcher.state.snapshot()['video'])
        self.assertEqual(len(self.platform.comments),1)
        with self.watcher.database.connect() as db:
            row=db.execute('SELECT * FROM video_history').fetchone()
            self.assertEqual((row['observations'],row['status']),(1,'skipped'))
        with self.watcher.memory.database.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM notes').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT text FROM evidence').fetchone()[0],'实际音频测试依据')
        self.watcher.state.change(lambda s:s.update(next_observation_at=0))
        self.platform.get_recommendations=lambda limit=6:[dict(bvid='BVsynthetic2',title='新候选',duration=90,category='科学')]
        await self.watcher.step()
        self.assertEqual(self.engine.selection_calls,2)
        self.assertEqual(self.engine.history[0]['bvid'],'BVsynthetic1')
        self.assertEqual(self.engine.history[0]['coverage'][0]['modality'],'audio')
        self.assertNotIn('符合人设',str(self.engine.history))

    async def test_interested_video_cap_is_sampled_not_finished(self):
        self.platform.resolve_video=lambda bvid:SimpleNamespace(identity=bvid,session_id='vod-session',duration=999)
        await self.watcher.step()
        for _ in range(4):
            self.watcher.state.change(lambda s:s.update(next_observation_at=0))
            self.assertEqual(await self.watcher.step(),'observed')
        self.assertIsNone(self.watcher.state.snapshot()['video'])
        with self.watcher.database.connect() as db:
            row=db.execute('SELECT * FROM video_history').fetchone()
            self.assertEqual((row['observations'],row['status']),(4,'sampled'))

    async def test_empty_observations_have_bounded_persistent_failure_and_no_progress(self):
        self.engine.empty=True
        await self.watcher.step()
        for index in range(3):
            self.watcher.state.change(lambda s:s.update(next_observation_at=0))
            self.assertEqual(await self.watcher.step(),'error')
            current=self.watcher.state.snapshot()['video']
            if index<2:self.assertEqual((current['position'],current['observations']),(0,0))
        self.assertIsNone(self.watcher.state.snapshot()['video'])
        with self.watcher.database.connect() as db:
            row=db.execute('SELECT * FROM video_history').fetchone()
            self.assertEqual((row['observations'],row['status']),(0,'unavailable'))
        with self.watcher.memory.database.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM evidence').fetchone()[0],0)

    async def test_unknown_selection_fee_belongs_to_video_pool(self):
        from bili_companion.policy.budget import BudgetLedger
        ledger=BudgetLedger(self.watcher.database,monthly='10',daily='1')
        ledger.reserve('bili-model-video-selection-unknown','.17',self.now)
        ledger.ambiguous('bili-model-video-selection-unknown')
        self.assertEqual(self.watcher.cost_profile(False)['kind_today'],.17)
        self.assertFalse(self.watcher.selection_round_affordable(self.watcher.state.snapshot()))

    def video_comment(self,bvid,**kwargs):
        from bili_companion.domain import Scope
        from bili_companion.policy.video_comments import VideoCommentCandidate
        self.watcher.state.change(lambda s:s.update(state='WATCHING_VIDEO',live_session=None,
            video=dict(bvid=bvid,session_id='video-session',observations=2,interest='high',uploader_id=bvid,comment_chunks=2,comment_coverage=[[0,30],[30,60]])))
        identity=uuid.uuid4().hex
        evidence=Evidence(identity,Scope('hakimi-bilibili','video',bvid),'video-session','钢琴演奏'+bvid,
            self.now-1,0,4,'audio','observation')
        self.watcher.memory.append(evidence)
        values=dict(bvid=bvid,text='这一段的钢琴演奏'+bvid,evidence_ids=(identity,),expires_at=self.now+50,
            watch_epoch=self.watcher.state.snapshot()['watch_epoch'],session_id='video-session')
        values.update(kwargs)
        return VideoCommentCandidate(**values)

    async def test_video_comments_unknown_not_retried_and_four_attempt_limit(self):
        self.watcher.comments.enabled=True;self.platform.fail_send=True
        candidate=self.video_comment('BVsynthetic1')
        def send(candidate):
            return self.watcher.comments.dispatch(candidate,self.watcher.state.snapshot,
                self.platform.send_video_comment,account_id=self.watcher.account,clock=lambda:self.now)
        self.assertEqual(send(candidate),'unknown')
        self.assertEqual(send(candidate),'duplicate')
        self.platform.fail_send=False
        self.assertEqual(send(self.video_comment('BVsynthetic2')),'cooldown')
        for index,label in enumerate(('鼓声清晰','镜头摇移','小提琴悠扬'),2):
            self.now+=3600
            candidate=self.video_comment('BVsynthetic'+str(index),text=label+'，这一小段钢琴有层次。')
            self.assertEqual(send(candidate),'sent')
        self.now+=3600
        self.assertEqual(send(self.video_comment('BVsynthetic5')),'daily_limit')
        self.assertEqual(len(self.platform.comments),4)

    async def test_video_comment_uploader_limits_and_legacy_unknowns(self):
        from bili_companion.policy.budget import new_york_date
        self.watcher.comments.enabled=True
        def send(bvid,text='这一段的钢琴演奏有层次。',uploader='123'):
            candidate=self.video_comment(bvid,text=text)
            self.watcher.state.change(lambda s:s['video'].update(uploader_id=uploader))
            return self.watcher.comments.dispatch(candidate,self.watcher.state.snapshot,
                self.platform.send_video_comment,account_id=self.watcher.account,clock=lambda:self.now)
        self.assertEqual(send('BVunknown',uploader=''),'unknown_uploader')
        self.assertEqual(send('BVfirst'),'sent')
        self.now+=3599
        self.assertEqual(send('BVcooldown',text='钢琴的节奏这里有变化。'),'cooldown')
        self.now+=1
        self.assertEqual(send('BVsecond',text='钢琴的节奏这里有变化。'),'sent')
        self.now+=3600
        self.assertEqual(send('BVthird',text='钢琴渐强很有推进感。'),'uploader_limit')
        # Legacy attempts retain their quota despite unknown uploader metadata.
        with self.watcher.database.connect(write=True) as db:
            db.execute('UPDATE video_comment_attempts SET uploader_id=""')
        self.assertEqual(send('BVlegacy',text='钢琴渐强很有推进感。',uploader='999'),'uploader_limit')

    async def test_video_comment_requires_observation_and_cancels_during_preflight(self):
        self.watcher.comments.enabled=True
        candidate=self.video_comment('BVsynthetic1')
        self.watcher.state.change(lambda s:s['video'].update(observations=0))
        sender=lambda *args,**kwargs:self.fail('unobserved video sent')
        self.assertEqual(self.watcher.comments.dispatch(candidate,self.watcher.state.snapshot,sender,
            account_id=self.watcher.account,clock=lambda:self.now),'inactive_video')
        self.watcher.state.change(lambda s:s['video'].update(observations=1))
        def preflight(bvid,text,*,before_post):
            self.live(True)
            self.assertFalse(before_post())
            return SimpleNamespace(status='rejected',code='cancelled_before_post')
        self.assertEqual(self.watcher.comments.dispatch(candidate,self.watcher.state.snapshot,preflight,
            account_id=self.watcher.account,clock=lambda:self.now),'cancelled')
        self.assertEqual(self.platform.comments,[])


class WatcherBackoffTests(unittest.TestCase):
    def test_error_backoff_is_exponential_bounded_and_resets_with_count(self):
        self.assertEqual([error_backoff(n) for n in range(1,9)],[5,10,20,40,80,160,300,300])
        self.assertEqual(error_backoff(1000),300)
        self.assertEqual(error_backoff(1),5)

    def test_error_codes_are_controlled_and_never_log_exception_urls(self):
        from bili_companion.media.capture import MediaError
        self.assertEqual(error_details(MediaError('capture_timeout'))['error_code'],'capture_timeout')
        self.assertNotIn('error_code',error_details(MediaError('https://secret.example/?token=x')))
        platform_error=type('PlatformError',(Exception,),{})('not logged')
        platform_error.code='https://secret.example/?token=x'
        self.assertNotIn('error_code',error_details(platform_error))


if __name__=='__main__':unittest.main()
