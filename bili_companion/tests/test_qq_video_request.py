"""Offline acceptance for QQ viewing: real engine, bounded fake transport, no network."""
from contextlib import nullcontext
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from bili_companion.cognition.live import CognitionEngine
from bili_companion.domain import Scope
from bili_companion.media.capture import MediaChunk
from bili_companion.memory import MemoryStore
from bili_companion.platform.bilibili import BilibiliPlatform, MediaTarget, PlatformError, _media_url
from bili_companion.policy.budget import BudgetLedger
from bili_companion.runtime.qq_request import QQCallStore, QQVideoRequests, intervals, video_identity, coverage_state, timeline_order
from bili_companion.runtime.storage import Database


class Client:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.empty = False
        self.delay = 0
        self.entered = self.release = None

    def count_tokens(self, model, payload):
        return 100

    def generate(self, model, payload):
        self.calls.append((model,payload))
        if self.entered:
            self.entered.set()
            self.release.wait(5)
        if self.failure:
            raise self.failure
        if self.delay:time.sleep(self.delay)
        data=json.loads(payload['contents'][0]['parts'][-1]['text'])
        if 'segments' in data:
            refs=list(range(len(data['segments'])))
            value=dict(overview='从开头展示钢琴，中段呈现演奏过程，到结尾收束，连续音轨贯穿画面。',
                segment_indices=refs,chapters=[dict(summary='演奏从开始到收尾的连贯发展',segment_indices=refs)])
            return dict(candidates=[dict(finishReason='STOP',content={'parts':[{'text':json.dumps(value)}]})],
                usageMetadata=dict(promptTokenCount=100,candidatesTokenCount=100,totalTokenCount=200))
        events = [] if self.empty else [
            dict(text='镜头中有一架钢琴',start=1,end=3,modality='video',confidence=.9),
            dict(text='听到连续的钢琴音',start=2,end=4,modality='audio',confidence=.9)]
        value = dict(events=events,missing_modalities=[],interesting=bool(events),
            video_feedback=dict(interest='high' if events else 'neutral',next_action='continue',
                summary='钢琴镜头配着演奏声' if events else '',evidence_indices=[0,1] if events else [],
                # Even a spurious model comment request can never dispatch.
                comment=dict(speak=True,text='这个琴声真清亮',evidence_indices=[0] if events else [])))
        return dict(candidates=[dict(finishReason='STOP',content={'parts':[{'text':json.dumps(value)}]})],
            usageMetadata=dict(promptTokenCount=100,candidatesTokenCount=100,totalTokenCount=200))


class Platform:
    def __init__(self, duration=20):
        self.calls = []
        self.duration = duration

    def resolve_video(self,bvid,*,page=1):
        self.calls.append((bvid,page))
        return MediaTarget('https://example.invalid/video',{},bvid,bvid+':part-'+str(page),False,
            duration=self.duration,title='钢琴视频')

    def _json(self,*args):
        return {'code':0,'data':{'bvid':'BV1234567890'}}


class Collector:
    def __init__(self):
        self.paths = []

    def capture(self,target,directory,*,duration,offset,cancel_event=None):
        Path(directory).mkdir(parents=True,exist_ok=True)
        path = Path(directory)/'clip.mp4'
        path.write_bytes(b'actual-test-media-envelope')
        self.paths.append(path)
        return MediaChunk(path,offset,offset+duration,1800000000,'mixed',target.identity,target.session_id)


class QQVideoRequestTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[1]/'state'/'test_tmp'
        temp_root.mkdir(parents=True,exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.root = Path(self.temp.name)
        self.memory = MemoryStore(Database(self.root/'native.sqlite3'))
        self.database = Database(self.root/'qq_requests.sqlite3')
        self.client = Client()
        self.ledger = QQCallStore(self.database)
        self.engine = CognitionEngine(self.client,self.ledger,self.memory,single_pass=True,
            observer_model='gemini-3.8-flash-high',decider_model='gemini-3.8-flash-high',
            price_resolver=lambda *_:self.fail('QQ must not consult prices'),audit_costs=False,
            request_usd_limit='not-a-price')
        self.engine.account_id = 'hakimi-bilibili'
        self.platform, self.collector = Platform(), Collector()
        self.service = QQVideoRequests(self.database,self.memory,self.platform,self.collector,self.engine,
            clock=lambda:1800000000)
        self.url = 'https://www.bilibili.com/video/BV1234567890?p=2'
        self.identity = hashlib.sha256(b'qq-context-and-message').hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def test_exhausted_autonomous_budget_is_untouched_and_native_facts_are_recalled(self):
        autonomous = Database(self.root/'watcher.sqlite3')
        budget = BudgetLedger(autonomous,monthly='.01',daily='.01')
        self.assertTrue(budget.reserve('autonomous-full','.01',1800000000))
        with autonomous.connect(write=True) as db:
            db.execute("INSERT INTO budget_freeze VALUES ('autonomous-freeze',0)")
            db.execute('CREATE TABLE sentinel (state TEXT,epoch INTEGER,position REAL,comment TEXT)')
            db.execute("INSERT INTO sentinel VALUES ('WATCHING_VIDEO',27,45.0,'unchanged')")
        before = budget.entries()
        historical = BudgetLedger(self.database,monthly='.01',daily='.01')
        historical.reserve('old-qq-charge','.01',1800000000)
        with self.database.connect(write=True) as db:
            db.execute("INSERT INTO budget_freeze VALUES ('old-qq-freeze',0)")
        historical_before = historical.entries()
        result = self.service.view(self.identity,self.url)
        self.assertEqual(result['status'],'observed')
        self.assertTrue(result['memory_saved'])
        self.assertEqual(self.platform.calls,[('BV1234567890',2)])
        self.assertEqual(len(self.client.calls),2)
        self.assertIn('inlineData',self.client.calls[0][1]['contents'][0]['parts'][0])
        data = json.loads(self.client.calls[0][1]['contents'][0]['parts'][1]['text'])
        self.assertFalse(data['comments_enabled'])
        self.assertEqual(budget.entries(),before)
        with autonomous.connect() as db:
            self.assertEqual(tuple(db.execute('SELECT * FROM sentinel').fetchone()),('WATCHING_VIDEO',27,45.0,'unchanged'))
            self.assertEqual(db.execute('SELECT count(*) FROM budget_freeze').fetchone()[0],1)
        self.assertEqual(historical.entries(),historical_before)
        self.assertEqual(result['calls'][0]['state'],'completed')
        self.assertNotIn('cost_audit',result)
        self.assertEqual(result['funding'],'qq_on_demand')
        self.assertEqual(result['sampled_intervals'],[dict(start=0,end=20,modality='mixed')])
        self.assertEqual([(c['start'],c['end']) for c in result['coverage']],[(1,3),(2,4)])
        scope = Scope('hakimi-bilibili','video','BV1234567890')
        recall = self.memory.recall(scope,'钢琴',budget=2000)
        self.assertTrue(any('QQ按需观看' in row['text'] for row in recall))
        self.assertTrue(any('镜头中' in row['text'] for row in recall))
        with self.memory.database.connect() as db:
            self.assertEqual(db.execute('SELECT source FROM qq_viewing_sources').fetchone()[0],'qq_on_demand')
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM qq_model_calls').fetchone()[0],2)
        self.assertTrue(all(not p.exists() for p in self.collector.paths))

    def test_repeat_is_cached_and_parameter_change_cannot_reuse_request_id(self):
        first = self.service.view(self.identity,self.url)
        repeat = self.service.view(self.identity,self.url)
        conflict = self.service.view(self.identity,self.url.replace('p=2','p=3'))
        self.assertTrue(repeat['cached'])
        self.assertEqual(repeat['evidence'],first['evidence'])
        self.assertEqual(len(self.client.calls),2)
        self.assertEqual(conflict['error'],'QQ_VIDEO_REQUEST_ID_CONFLICT')
        with self.memory.database.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM evidence').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM notes').fetchone()[0],2)

    def test_failed_call_keeps_lifecycle_without_charge_or_automatic_retry(self):
        self.client.failure = TimeoutError('provider_timeout')
        first = self.service.view(self.identity,self.url)
        repeat = self.service.view(self.identity,self.url)
        self.assertEqual(first['status'],'unavailable')
        self.assertFalse(first['memory_saved'])
        self.assertEqual(first['coverage'],[])
        self.assertEqual(first['calls'][0]['state'],'failed')
        self.assertNotIn('cost_audit',first)
        with self.database.connect() as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='budget'").fetchone())
        self.assertTrue(repeat['cached'])
        self.assertEqual(len(self.client.calls),1)

    def test_crash_after_start_already_has_durable_owner_without_monetary_reservation(self):
        class ProcessGone(BaseException):
            pass
        def interrupted(model,payload):
            with self.database.connect() as db:
                row = db.execute('SELECT owner,state FROM qq_model_calls').fetchone()
                self.assertEqual(row['owner'],self.identity)
                self.assertEqual(row['state'],'started')
            raise ProcessGone()
        self.client.generate = interrupted
        with self.assertRaises(ProcessGone):
            self.service.view(self.identity,self.url)
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM qq_model_calls').fetchone()[0],'started')
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='budget'").fetchone())
        repeat = self.service.view(self.identity,self.url)
        self.assertEqual(repeat['error'],'QQ_VIDEO_REQUEST_IN_PROGRESS')

    def test_valid_observation_without_usage_still_enters_native_memory(self):
        original = self.client.generate
        def without_usage(*args):
            value = original(*args)
            value.pop('usageMetadata')
            return value
        self.client.generate = without_usage
        result = self.service.view(self.identity,self.url)
        self.assertEqual(result['status'],'observed')
        self.assertTrue(result['memory_saved'])
        self.assertEqual(result['calls'][0]['state'],'completed')
        with self.database.connect() as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='budget'").fetchone())

    def test_provider_group_deleted_is_exposed_without_becoming_a_budget_error(self):
        from bili_companion.cognition.gateway import CognitionUnavailable
        from bili_companion.runtime.watcher import error_details
        failure = CognitionUnavailable('provider_http_403')
        failure.http_status = 403
        failure.provider_code = 'GROUP_DELETED'
        self.client.failure = failure
        result = self.service.view(self.identity,self.url)
        self.assertEqual(result['error_detail'],'provider_http_403')
        self.assertEqual(result['http_status'],403)
        self.assertEqual(result['error_features'],['provider_code_group_deleted'])
        self.assertEqual(result['calls'][0]['provider_code'],'GROUP_DELETED')
        self.assertEqual(result['calls'][0]['state'],'failed')
        diagnostic = error_details(self.engine._call_failure(failure,'test','model_call_unsettled'))
        self.assertEqual(diagnostic['error_code'],'provider_http_403')
        self.assertEqual(diagnostic['provider_code'],'GROUP_DELETED')

    def test_empty_facts_never_create_viewed_memory(self):
        self.client.empty = True
        result = self.service.view(self.identity,self.url)
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(result['evidence'],[])
        self.assertFalse(result['memory_saved'])
        with self.memory.database.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM notes').fetchone()[0],0)

    def test_concurrent_claim_prevents_second_media_or_model_call(self):
        self.client.entered,self.client.release = threading.Event(),threading.Event()
        results = []
        worker = threading.Thread(target=lambda:results.append(self.service.view(self.identity,self.url)))
        worker.start()
        try:
            self.assertTrue(self.client.entered.wait(3))
            second = self.service.view(self.identity,self.url)
            self.assertEqual(second['error'],'QQ_VIDEO_REQUEST_IN_PROGRESS')
        finally:
            self.client.release.set()
            worker.join(5)
        self.assertEqual(len(self.client.calls),2)
        self.assertEqual(results[0]['status'],'observed')

    def test_clip_timeout_returns_error_without_viewed_record(self):
        with patch.object(self.collector,'capture',side_effect=TimeoutError('deadline')):
            result = self.service.view(self.identity,self.url)
        self.assertEqual(result['error'],'QQ_VIDEO_TIMEOUT')
        self.assertEqual(len(self.client.calls),0)
        self.assertFalse(result['memory_saved'])

    def test_platform_failure_preserves_only_safe_enumerated_diagnostics(self):
        failure = PlatformError('media_unavailable',features=('http_412','risk_control'))
        with patch.object(self.platform,'resolve_video',side_effect=failure):
            result = self.service.view(self.identity,self.url)
        self.assertEqual(result['error_detail'],'media_unavailable')
        self.assertEqual(result['error_features'],['http_412','risk_control'])
        self.assertEqual(result['bvid'],'BV1234567890')
        self.assertEqual(result['part'],2)
        self.assertEqual(result['model_request_ids'],[])
        self.assertFalse(result['memory_saved'])

    def legacy_format_failure(self):
        with patch.object(self.platform,'resolve_video',side_effect=PlatformError('media_unavailable',features=('format_unavailable',))):
            result=self.service.view(self.identity,self.url)
        result.pop('media_resolver_revision')
        with self.database.connect(write=True) as db:
            db.execute('UPDATE qq_requests SET result=? WHERE id=?',(json.dumps(result),self.identity))
        return result

    def test_upgraded_resolver_recovers_legacy_format_failure_once_then_uses_cache(self):
        self.legacy_format_failure()
        result=self.service.view(self.identity,self.url)
        self.assertEqual(result['status'],'observed')
        self.assertEqual(len(self.client.calls),2)
        self.assertTrue(self.service.view(self.identity,self.url)['cached'])
        self.assertEqual(len(self.client.calls),2)

    def test_still_failing_new_resolver_is_cached_without_blind_retry(self):
        self.legacy_format_failure()
        with patch.object(self.platform,'resolve_video',side_effect=PlatformError('media_unavailable',features=('format_unavailable',))) as resolve:
            first=self.service.view(self.identity,self.url)
            second=self.service.view(self.identity,self.url)
        self.assertEqual(first['status'],'unavailable')
        self.assertTrue(second['cached'])
        self.assertEqual(resolve.call_count,1)
        self.assertEqual(len(self.client.calls),0)

    def test_legacy_failure_with_real_model_call_is_never_replayed(self):
        self.legacy_format_failure()
        with self.database.connect(write=True) as db:
            db.execute("INSERT INTO qq_model_calls VALUES ('attempt',?,'model','observer','observe','failed',1,2,'error',NULL,NULL)",(self.identity,))
        with patch.object(self.platform,'resolve_video') as resolve:
            result=self.service.view(self.identity,self.url)
        self.assertTrue(result['cached'])
        resolve.assert_not_called()

    def test_risk_control_is_not_retried_as_a_format_failure(self):
        result=self.legacy_format_failure();result['error_features'].append('risk_control')
        with self.database.connect(write=True) as db:
            db.execute('UPDATE qq_requests SET result=? WHERE id=?',(json.dumps(result),self.identity))
        with patch.object(self.platform,'resolve_video') as resolve:
            self.assertTrue(self.service.view(self.identity,self.url)['cached'])
        resolve.assert_not_called()

    def test_transient_media_read_retries_with_backoff_before_one_model_pass(self):
        target=self.platform.resolve_video('BV1234567890',page=2)
        with patch.object(self.platform,'resolve_video',side_effect=[PlatformError('network'),
                PlatformError('media_unavailable',features=('network_timeout',)),target]) as resolve,\
                patch('bili_companion.runtime.qq_request.time.sleep') as sleep:
            result=self.service.view(self.identity,self.url)
        self.assertEqual(result['status'],'observed')
        self.assertEqual(resolve.call_count,3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list],[1,3])
        self.assertEqual(len(self.client.calls),2)

    def test_transient_failure_cooldown_and_one_cached_recovery(self):
        with patch.object(self.platform,'resolve_video',side_effect=PlatformError('network')) as resolve,\
                patch('bili_companion.runtime.qq_request.time.sleep'):
            first=self.service.view(self.identity,self.url)
            self.assertEqual(resolve.call_count,3)
            self.assertTrue(self.service.view(self.identity,self.url)['cached'])
            self.assertEqual(resolve.call_count,3)
        self.assertEqual(first['status'],'unavailable')
        self.service.clock=lambda:1800000031
        result=self.service.view(self.identity,self.url)
        self.assertEqual(result['status'],'observed')
        self.assertEqual(result['resolution_recoveries'],1)
        self.assertEqual(len(self.client.calls),2)

    def test_stale_interrupted_request_recovers_only_before_model_and_when_owner_gone(self):
        from bili_companion.runtime.qq_request import VIEW_REVISION
        digest=hashlib.sha256((VIEW_REVISION+'|'+self.url).encode()).hexdigest()
        with self.database.connect(write=True) as db:
            db.execute("INSERT INTO qq_requests VALUES (?,?,'running',?,?,NULL)",
                       (self.identity,digest,1799999700,1799999700))
            db.execute('INSERT INTO qq_request_recovery VALUES (?,0,0,123)',(self.identity,))
        with patch('bili_companion.runtime.qq_request._owner_alive',return_value=True):
            self.assertEqual(self.service.view(self.identity,self.url)['error'],'QQ_VIDEO_REQUEST_INTERRUPTED')
        with patch('bili_companion.runtime.qq_request._owner_alive',return_value=False):
            result=self.service.view(self.identity,self.url)
        self.assertEqual(result['status'],'observed')
        self.assertEqual(result['resolution_recoveries'],1)
        self.assertEqual(len(self.client.calls),2)

    def test_stale_interrupted_request_with_started_model_stays_frozen(self):
        from bili_companion.runtime.qq_request import VIEW_REVISION
        digest=hashlib.sha256((VIEW_REVISION+'|'+self.url).encode()).hexdigest()
        with self.database.connect(write=True) as db:
            db.execute("INSERT INTO qq_requests VALUES (?,?,'running',?,?,NULL)",
                       (self.identity,digest,1799999700,1799999700))
            db.execute("INSERT INTO qq_model_calls VALUES ('attempt',?,'model','observer','observe','started',1,NULL,NULL,NULL,NULL)",(self.identity,))
        with patch.object(self.platform,'resolve_video') as resolve:
            self.assertEqual(self.service.view(self.identity,self.url)['error'],'QQ_VIDEO_REQUEST_INTERRUPTED')
        resolve.assert_not_called()

    def test_permanent_error_ends_current_request_and_next_video_still_runs(self):
        with patch.object(self.platform,'resolve_video',side_effect=PlatformError('media_unavailable',features=('http_403',))) as resolve,\
                patch('bili_companion.runtime.qq_request.time.sleep') as sleep:
            failed=self.service.view(self.identity,self.url)
        self.assertEqual(failed['status'],'unavailable');self.assertEqual(resolve.call_count,1)
        sleep.assert_not_called()
        next_id=hashlib.sha256(b'next-video-request').hexdigest()
        self.assertEqual(self.service.view(next_id,self.url)['status'],'observed')
        self.assertEqual(len(self.client.calls),2)

    def test_transient_recovery_budget_is_finite_even_after_more_cooldowns(self):
        with patch.object(self.platform,'resolve_video',side_effect=PlatformError('network')) as resolve,\
                patch('bili_companion.runtime.qq_request.time.sleep'):
            self.service.view(self.identity,self.url)
            self.service.clock=lambda:1800000031
            self.service.view(self.identity,self.url)
            self.service.clock=lambda:1800000200
            self.assertTrue(self.service.view(self.identity,self.url)['cached'])
        self.assertEqual(resolve.call_count,6)  # 3 bounded reads, once more after cooldown.
        self.assertEqual(self.client.calls,[])

    def test_short_remaining_deadline_does_not_start_another_extractor_attempt(self):
        with patch.object(self.platform,'resolve_video',side_effect=PlatformError('network')) as resolve,\
                patch('bili_companion.runtime.qq_request.time.sleep') as sleep:
            result=self.service.view(self.identity,self.url,timeout_seconds=10)
        self.assertEqual(result['status'],'unavailable');self.assertEqual(resolve.call_count,1)
        sleep.assert_not_called()

    def test_legacy_model_call_ledger_also_blocks_replay(self):
        self.legacy_format_failure()
        with self.database.connect(write=True) as db:
            db.execute('CREATE TABLE qq_request_calls (request_id TEXT PRIMARY KEY,model_request_id TEXT,owner TEXT,usage TEXT)')
            db.execute("INSERT INTO qq_request_calls VALUES ('legacy','actual',?,NULL)",(self.identity,))
        with patch.object(self.platform,'resolve_video') as resolve:
            self.assertTrue(self.service.view(self.identity,self.url)['cached'])
        resolve.assert_not_called()

    def test_extractor_failure_never_exposes_stderr_credentials_or_media_urls(self):
        platform = BilibiliPlatform(self.root/'unused-auth')
        response = type('ProcessResult',(),dict(returncode=1,stdout=b'',
            stderr=b'HTTP Error 412 Precondition Failed; risk control; SESSDATA=PRIVATE https://secret.invalid/media?token=PRIVATE'))()
        with patch.object(platform,'_cookie_file',return_value=nullcontext('synthetic-cookie-file')), \
                patch('bili_companion.platform.bilibili.subprocess.run',return_value=response):
            with self.assertRaises(PlatformError) as failure:
                platform._extract('https://www.bilibili.com/video/BV1234567890')
        self.assertEqual(failure.exception.code,'media_unavailable')
        self.assertEqual(failure.exception.features,('http_412','risk_control'))
        self.assertNotIn('PRIVATE',str(failure.exception))
        self.assertNotIn('secret.invalid',repr(failure.exception.features))

    def test_native_and_expense_databases_cannot_be_the_same(self):
        memory = MemoryStore(self.database)
        with self.assertRaises(ValueError):
            QQVideoRequests(self.database,memory,self.platform,self.collector,self.engine)

    def test_legacy_source_schema_is_migrated_without_losing_existing_records(self):
        with self.memory.database.connect(write=True) as db:
            db.execute('DROP TABLE qq_viewing_sources')
            db.execute('''CREATE TABLE qq_viewing_sources (request_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,bvid TEXT,session TEXT,evidence_ids TEXT NOT NULL,observed REAL NOT NULL)''')
            db.execute("INSERT INTO qq_viewing_sources VALUES ('legacy','qq_on_demand','BV1234567890','part1','[]',1)")
        service = QQVideoRequests(self.database,self.memory,self.platform,self.collector,self.engine,
            clock=lambda:1800000000)
        result = service.view(self.identity,self.url)
        self.assertEqual(result['status'],'observed')
        with self.memory.database.connect() as db:
            self.assertEqual(db.execute("SELECT part FROM qq_viewing_sources WHERE request_id='legacy'").fetchone()[0],1)
            self.assertEqual(db.execute('SELECT part FROM qq_viewing_sources WHERE request_id=?',(self.identity,)).fetchone()[0],2)

    def test_intervals_cover_entire_time_axis_with_bounded_codec_compatible_clips(self):
        for duration in (20,60.2,65,283.91,3000):
            plan=intervals(duration)
            self.assertEqual(plan[0][0],0)
            self.assertTrue(all(1<=length<=30 for _,length in plan))
            state=coverage_state(duration,[dict(start=start,end=start+length) for start,length in plan])
            self.assertEqual(state['coverage_status'],'complete')
            self.assertEqual(state['coverage_fraction'],1)
            self.assertEqual(state['unobserved_intervals'],[])
        order=timeline_order(10)
        self.assertEqual(set(order),set(range(10)))
        self.assertEqual(order[:3],[0,9,4])
        self.assertEqual(video_identity('https://www.bilibili.com/video/av123?p=3',self.platform),('BV1234567890',3))
        with self.assertRaises(ValueError):
            video_identity('https://example.com/video/BV1234567890',self.platform)

    def test_full_video_synthesizes_start_middle_and_ending_and_remembers_overall_note(self):
        self.platform.duration=283.91
        result=self.service.view(self.identity,self.url)
        self.assertEqual(result['status'],'observed')
        self.assertEqual(result['coverage_status'],'complete')
        self.assertEqual(result['coverage_fraction'],1)
        self.assertEqual(result['planned_segments'],10)
        self.assertEqual(len(result['sampled_intervals']),10)
        self.assertEqual(result['unobserved_intervals'],[])
        self.assertIn('结尾',result['overview'])
        self.assertEqual(result['synthesis_status'],'completed')
        self.assertEqual(len(self.client.calls),11)
        self.assertEqual(result['summaries'][-1]['end'],283.91)
        with self.memory.database.connect() as db:
            note=db.execute('SELECT text,evidence_ids FROM notes WHERE id=?',('qq-view-'+self.identity+'-overall',)).fetchone()
        self.assertIn(result['overview'],note['text'])
        self.assertEqual(set(json.loads(note['evidence_ids'])),set(result['overview_evidence_ids']))
        self.assertNotIn('cost_audit',result)

    def _routed_summary_service(self, *, strict=False):
        from bili_companion.cognition.routing import HydrogenRouteEngine,PRIMARY
        from bili_companion.cognition.gateway import HYDROGEN
        primary=Client();backup=Client();original=primary.generate
        def invalid_summary(model,payload):
            value=original(model,payload)
            value['_provider_response_metadata']={'reported_model':PRIMARY}
            data=json.loads(payload['contents'][0]['parts'][-1]['text'])
            if 'segments' in data:
                body=json.loads(value['candidates'][0]['content']['parts'][0]['text'])
                body['overview']=''
                value['candidates'][0]['content']['parts'][0]['text']=json.dumps(body)
            return value
        primary.generate=invalid_summary
        fallback=CognitionEngine(backup,self.ledger,self.memory,audit_costs=False,single_pass=True,
            observer_model=HYDROGEN,decider_model=HYDROGEN)
        route=HydrogenRouteEngine(primary,self.ledger,self.memory,audit_costs=False,single_pass=True,
            observer_model=PRIMARY,decider_model=PRIMARY,fallback=None if strict else fallback)
        route.account_id='hakimi-bilibili'
        return QQVideoRequests(self.database,self.memory,self.platform,self.collector,route,
            clock=lambda:1800000000),primary,backup

    def test_routed_summary_validates_paid_primary_before_backup_and_writes_once(self):
        service,primary,backup=self._routed_summary_service()
        with patch.object(self.memory,'revise',wraps=self.memory.revise) as revise:
            result=service.view(self.identity,self.url)
        self.assertEqual(result['synthesis_status'],'completed')
        self.assertEqual(len(primary.calls),2)
        self.assertEqual(len(backup.calls),1)
        overall=[call for call in revise.call_args_list if call.kwargs.get('note_id','').endswith('-overall')]
        self.assertEqual(len(overall),1)
        self.assertTrue(any(call.get('validation_error')=='invalid_overview' for call in result['calls']))
        with self.database.connect() as db:
            saved=json.loads(db.execute('SELECT result FROM qq_requests WHERE id=?',(self.identity,)).fetchone()[0])
        self.assertTrue(any(call.get('validation_error')=='invalid_overview' for call in saved['calls']))

    def test_strict_routed_summary_persists_enumerated_failure_without_backup(self):
        service,primary,backup=self._routed_summary_service(strict=True)
        result=service.view(self.identity,self.url)
        self.assertEqual(result['synthesis_status'],'unavailable')
        self.assertEqual(result['synthesis_error'],'invalid_overview')
        self.assertEqual(backup.calls,[])
        with self.database.connect() as db:
            saved=json.loads(db.execute('SELECT result FROM qq_requests WHERE id=?',(self.identity,)).fetchone()[0])
        self.assertEqual(saved['synthesis_error'],'invalid_overview')
        with self.memory.database.connect() as db:
            self.assertIsNone(db.execute('SELECT id FROM notes WHERE id=?',('qq-view-'+self.identity+'-overall',)).fetchone())

    def test_short_deadline_reports_actual_gaps_and_reaches_the_ending_first(self):
        self.platform.duration=1000
        self.client.delay=.09
        result=self.service.view(self.identity,self.url,timeout_seconds=2.9)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['coverage_status'],'partial')
        self.assertTrue(result['unobserved_intervals'])
        self.assertGreater(result['sampled_intervals'][-1]['end'],999)
        self.assertLess(result['coverage_fraction'],1)
        self.assertEqual(result['synthesis_status'],'completed')
        self.assertTrue(result['memory_saved'])

    def test_parallel_segments_use_independent_clients_and_bounded_concurrency(self):
        self.platform.duration=65
        clients=[];lock=threading.Lock();active=0;peak=0;first_wave=threading.Barrier(2)
        def factory():
            nonlocal active,peak
            client=Client()
            with lock:clients.append(client);ordinal=len(clients)
            original=client.generate
            def generate(*args):
                nonlocal active,peak
                with lock:active+=1;peak=max(peak,active)
                try:
                    if ordinal<=2:first_wave.wait(3)
                    time.sleep(.03)
                    return original(*args)
                finally:
                    with lock:active-=1
            client.generate=generate
            engine=CognitionEngine(client,self.ledger,self.memory,single_pass=True,audit_costs=False,
                observer_model='gemini-3.8-flash-high',decider_model='gemini-3.8-flash-high')
            engine.account_id=self.engine.account_id
            return engine
        service=QQVideoRequests(self.database,self.memory,self.platform,self.collector,self.engine,
            engine_factory=factory,max_workers=2)
        result=service.view(self.identity,self.url)
        self.assertEqual(result['status'],'observed')
        self.assertEqual(peak,2)
        self.assertEqual(len(clients),3)
        self.assertTrue(all(len(client.calls)==1 for client in clients))
        self.assertEqual(len(self.client.calls),1)  # Only the global synthesis uses this client.

    def test_failed_middle_segment_leaves_an_explicit_gap_in_overall_understanding(self):
        self.platform.duration=283.91
        original=self.service._segment
        def segment(request_id,index,*args):
            if index==4:raise TimeoutError('middle clip failed')
            return original(request_id,index,*args)
        with patch.object(self.service,'_segment',side_effect=segment):
            result=self.service.view(self.identity,self.url)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['coverage_status'],'partial')
        self.assertEqual(result['synthesis_status'],'completed')
        self.assertEqual(len(result['sampled_intervals']),9)
        self.assertEqual(len(result['unobserved_intervals']),1)
        self.assertGreater(result['unobserved_intervals'][0]['start'],100)
        self.assertLess(result['unobserved_intervals'][0]['end'],150)

    def test_platform_preserves_part_selection(self):
        platform = BilibiliPlatform(self.root/'unused-auth')
        info = {'id':'cid-part2','duration':20,'title':'part two'}
        with patch.object(platform,'_extract',return_value=(info,{'url':'https://x.bilivideo.com/v'},None)) as extract, \
                patch.object(platform,'_json',return_value={'code':-1}):
            target = platform.resolve_video('BV1234567890',page=2)
        self.assertEqual(extract.call_args.args[0],'https://www.bilibili.com/video/BV1234567890?p=2')
        self.assertIn('cid-part2',target.session_id)

    def test_extracted_cdn_can_rotate_without_changing_allowed_video_inputs(self):
        platform = BilibiliPlatform(self.root/'unused-auth')
        video_url = 'https://new-regional-cdn.example.net/media/video'
        audio_url = 'https://different-audio-cdn.example.org/media/audio'
        info = {'id':'cid-cdn','duration':20,'title':'regional CDN'}
        with patch.object(platform,'_extract',return_value=(info,{'url':video_url},{'url':audio_url})), \
                patch.object(platform,'_json',return_value={'code':-1}):
            target = platform.resolve_video('BV1234567890')
        self.assertEqual(target.url,video_url)
        self.assertEqual(target.audio_url,audio_url)
        for url in ('file:///tmp/video','https://user:password@cdn.example/video','https:///missing-host'):
            with self.assertRaises(PlatformError):
                _media_url(url)
        with self.assertRaises(ValueError):
            video_identity('https://new-regional-cdn.example.net/video/BV1234567890',self.platform)


    def test_short_redirect_stops_at_video_identity(self):
        import io
        from types import SimpleNamespace
        import urllib.request
        calls=[]
        def opener(handler):
            def open_(request,timeout):
                calls.append(request.full_url)
                return handler.redirect_request(request,io.BytesIO(),302,'Found',{},
                    'https://www.bilibili.com/video/BV1AoxW6kEAE?p=2&share_source=copy')
            return SimpleNamespace(open=open_)
        with patch('urllib.request.build_opener',side_effect=opener), patch.object(
                urllib.request.HTTPRedirectHandler,'redirect_request',side_effect=AssertionError('target page opened')):
            self.assertEqual(video_identity('https://b23.tv/KeBHDGf',self.platform),('BV1AoxW6kEAE',2))
        self.assertEqual(calls,['https://b23.tv/KeBHDGf'])

    def test_http_identity_failure_preserves_status_and_stage(self):
        import urllib.error
        with patch('bili_companion.runtime.qq_request.video_identity',side_effect=urllib.error.HTTPError(
                'https://b23.tv/KeBHDGf',412,'blocked',{},None)):
            result=self.service.view(self.identity,self.url)
        self.assertEqual((result['http_status'],result['error_detail'],result['error_stage']),
                         (412,'http_412','resolve_identity'))
        self.assertEqual(result['model_request_ids'],[])

    def test_media_failure_preserves_adapter_code_and_stage(self):
        with patch.object(self.platform,'resolve_video',side_effect=PlatformError('extract_failed',features=('http_403',))):
            result=self.service.view(self.identity,self.url)
        self.assertEqual((result['error_stage'],result['error_detail'],result['error_features']),
                         ('resolve_media','extract_failed',['http_403']))


if __name__ == '__main__':
    unittest.main()
