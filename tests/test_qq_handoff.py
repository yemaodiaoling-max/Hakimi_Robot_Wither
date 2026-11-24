"""Isolated QQ entry-point acceptance; imports only the current candidate functions."""
import ast
from contextlib import closing
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

CANDIDATE = Path(os.environ.get('HAKIMI_CANDIDATE_ROOT',Path(__file__).resolve().parents[1]/'src'/'hakimi'))


def module(name, **attrs):
    value = ModuleType(name)
    value.__dict__.update(attrs)
    return value


def select(payload):
    rows = payload['recent_context_messages']
    target = next((row for row in rows if row['message_id'] == payload.get('reply_target_message_id')), rows[-1])
    return rows, target


def function(path, name, namespace):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    target = next(node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[target],type_ignores=[]),str(path),'exec'),namespace)
    return namespace[name]


class QQEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.payload = dict(session_key='group:123',recent_context_messages=[
            dict(user_id='user',message_id='m1',text='https://www.bilibili.com/video/BV1234567890?p=2')])
        self.report = dict(source='bilibili_companion_qq_on_demand',funding='qq_on_demand',
            status='observed',overview='连贯的整片理解及结尾',chapters=[{'start':0,'end':20,'summary':'全片脉络'}],
            coverage_status='complete',coverage_fraction=1,unobserved_intervals=[],view_mode='continuous-v1',evidence=[dict(id='e1',text='确实听到钢琴',media_start=2,media_end=4,
                modality='audio',attribution='observation',confidence=.9)],
            coverage=[dict(start=2,end=4,modality='audio')],sampled_intervals=[dict(start=0,end=20,modality='mixed')],
            summaries=[dict(text='钢琴演奏',evidence_ids=['e1'])],memory_saved=True,
            cost_audit=[dict(cost_usd=.001)],model_request_ids=['model1'])
        self.context = dict(owner_token='owner',trigger_id='trigger',expires_at=time.time()+120)
        self.coordinator = SimpleNamespace(check_token=lambda *_:True,snapshot=lambda:{'payload':self.payload})
        self.modules = {
            'hakimi':module('hakimi',paths=SimpleNamespace(base=lambda:self.base)),
            'hakimi.core':module('hakimi.core'),
            'hakimi.core.context_window':module('hakimi.core.context_window',select_context=select),
            'hakimi.core.runtime_io':module('hakimi.core.runtime_io',read_json_cached=lambda *_:{}),
            'hakimi.tools':module('hakimi.tools'),
            'hakimi.tools.math_render':module('hakimi.tools.math_render',log_tool_call=lambda *_a,**_k:None),
            'hakimi.prompt':module('hakimi.prompt'),
            'hakimi.prompt.prompt_context':module('hakimi.prompt.prompt_context',activate_context=lambda *_:None),
            'hakimi.prompt.tool_catalog':module('hakimi.prompt.tool_catalog',BY_NAME={}),
            'hakimi.bridge':module('hakimi.bridge'),
            'hakimi.bridge.turn_coordinator':module('hakimi.bridge.turn_coordinator',TurnCoordinator=lambda *_:self.coordinator),
        }
        self.patcher = patch.dict(sys.modules,self.modules)
        self.patcher.start()
        spec = importlib.util.spec_from_file_location('hakimi.tools.bilibili_video',CANDIDATE/'tools'/'bilibili_video.py')
        self.video = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.video)
        sys.modules['hakimi.tools.bilibili_video'] = self.video
        self.modules['hakimi.tools'].bilibili_video = self.video
        self.process = Mock(returncode=0)
        self.process.communicate.return_value = (json.dumps(self.report).encode(),b'')
        self.Popen = patch.object(self.video.subprocess,'Popen',return_value=self.process).start()

    def tearDown(self):
        patch.stopall()
        self.patcher.stop()
        self.temp.cleanup()

    def test_prepare_calls_companion_without_old_cache_or_qq_model_and_preserves_part(self):
        result = self.video.prepare(self.payload,self.base,timeout_seconds=42)
        command = self.Popen.call_args.args[0]
        self.assertEqual(command[1:3],['-m','bili_companion.runtime.qq_request'])
        self.assertIn('?p=2',command[command.index('--url')+1])
        self.assertGreater(float(command[command.index('--timeout')+1]),40)
        self.assertLessEqual(float(command[command.index('--timeout')+1]),41)
        brief = self.video.brief(result)
        self.assertEqual(brief['summaries'][0]['text'],'钢琴演奏')
        self.assertEqual(brief['evidence'][0]['modality'],'audio')
        self.assertNotIn('cost_audit',brief)
        self.assertEqual(self.video.images(result,self.payload,self.base),[])
        first_id = command[command.index('--request-id')+1]
        self.video.prepare(self.payload,self.base)
        second_command = self.Popen.call_args.args[0]
        self.assertEqual(first_id,second_command[second_command.index('--request-id')+1])
        self.assertEqual(len(first_id),64)
        self.video.prepare(dict(self.payload,trigger_id='new-turn'),self.base)
        third_command = self.Popen.call_args.args[0]
        self.assertNotEqual(first_id,third_command[third_command.index('--request-id')+1])

    def test_share_resolution_spends_the_same_caller_deadline(self):
        with patch.object(self.video.time,'monotonic',side_effect=[100,100,101,106]), \
                patch.object(self.video,'_resolve_qq_share',return_value=self.payload['recent_context_messages'][0]['text']) as resolve:
            result = self.video.prepare(self.payload,self.base,timeout_seconds=10)
        self.assertEqual(result['status'],'observed')
        self.assertLessEqual(resolve.call_args.kwargs['timeout'],6)
        command = self.Popen.call_args.args[0]
        self.assertEqual(float(command[command.index('--timeout')+1]),3)
        self.assertEqual(self.process.communicate.call_args.kwargs['timeout'],4)

    def test_child_imports_explicit_companion_source_without_inherited_stage_paths(self):
        source = str(self.base/'isolated-companion'/'src')
        with patch.dict(self.video.os.environ,{},clear=True), \
                patch.object(self.video,'read_json_cached',return_value={
                    'companion_config':'/isolated/watcher.json','companion_source':source}):
            result = self.video.prepare(self.payload,self.base)
        self.assertEqual(result['status'],'observed')
        self.assertEqual(self.Popen.call_args.kwargs['env']['PYTHONPATH'],source)

    def test_instance_config_is_read_from_base(self):
        self.video.ROOT = self.base/'different-global-instance'
        with patch.object(self.video,'read_json_cached',return_value={'companion_config':'/isolated/watcher.json'}) as settings:
            self.video.prepare(self.payload,self.base)
        self.assertEqual(settings.call_args.args[0],self.base/'video_tools_config.json')
        self.assertIn('/isolated/watcher.json',self.Popen.call_args.args[0])

    def test_official_runtime_uses_passed_instance_parent(self):
        self.video.ROOT = self.base/'different-global-instance'
        runtime = self.base/'official_test_runtime'
        with patch.object(self.video,'read_json_cached',return_value={'companion_config':'/isolated/runtime.json'}) as settings:
            self.video.prepare(self.payload,runtime)
        self.assertEqual(settings.call_args.args[0],self.base/'video_tools_config.json')
        self.assertIn('/isolated/runtime.json',self.Popen.call_args.args[0])

    def test_structured_qq_card_parser_preserves_actual_link(self):
        card = {'app':'com.tencent.miniapp_01','meta':{'detail_1':{
            'appid':'1109937557','qqdocurl':'https://www.bilibili.com/video/BV1234567890?p=2'}}}
        self.assertEqual(self.video.find_url(card),'https://www.bilibili.com/video/BV1234567890?p=2')
        self.payload['recent_context_messages'][0]['text'] = card
        report = self.video.prepare(self.payload,self.base)
        self.assertEqual(report['status'],'observed')

    def test_primary_and_miniapp_share_delegation_and_keep_context_gate(self):
        namespace = dict(BASE_DIR=self.base,time=time,Path=Path,datetime=datetime,timezone=timezone,
            require_context=lambda *_:self.context,ToolError=ValueError)
        path = CANDIDATE/'tools'/'bot_tools.py'
        function(path,'_schema',namespace)
        run = function(path,'run_tool',namespace)
        primary = run('bilibili_video','group:123',{'url':self.payload['recent_context_messages'][0]['text']})
        miniapp = run('qq_miniapp_video','group:123',{'message_id':'m1'})
        self.assertEqual(primary['source'],'bilibili_companion_qq_on_demand')
        self.assertEqual(primary['data']['summaries'],miniapp['data']['summaries'])
        with self.assertRaises(ValueError):
            run('bilibili_video','group:123',{'url':'BV9999999999'})
        self.assertEqual(self.Popen.call_count,2)

    def test_vertex_watch_and_miniapp_receive_companion_understanding(self):
        tool = function(CANDIDATE/'models'/'vertex_session.py','_tool',{})
        for name,args in [('video.watch',{'url':self.payload['recent_context_messages'][0]['text']}),
                          ('video.watch_miniapp',{'message_id':'m1'})]:
            result = tool(SimpleNamespace(cwd=self.base),name,args,{'payload':self.payload},55)
            self.assertTrue(result['ok'])
            self.assertEqual(result['result']['summaries'],self.report['summaries'])
            self.assertEqual(result['image_paths'],[])
        self.assertEqual(self.Popen.call_count,2)

    def test_dsh_watch_and_miniapp_receive_same_understanding(self):
        namespace = dict(validate_request=lambda _n,data:data,TERMINAL=set(),Path=Path,time=time)
        tool = function(CANDIDATE/'models'/'dsh_tools.py','execute',namespace)
        for name,args in [('video.watch',{'url':self.payload['recent_context_messages'][0]['text']}),
                          ('video.watch_miniapp',{'message_id':'m1'})]:
            claim = dict(payload=dict(self.payload,listening_window={'irrelevant':True}),token='owner',deadline=time.time()+90)
            result = tool({'tool':name,'input':args},claim,self.coordinator,self.base)
            self.assertTrue(result['ok'])
            self.assertEqual(result['result']['summaries'],self.report['summaries'])
            self.assertEqual(claim['_images'],[])
        self.assertEqual(self.Popen.call_count,2)

    def test_partial_timeout_result_is_returned_and_takeover_remains_a_gate(self):
        partial = dict(self.report,status='partial',warning='QQ_VIDEO_TIMEOUT')
        self.process.communicate.side_effect = [subprocess.TimeoutExpired('view',10),(json.dumps(partial).encode(),b'')]
        with patch.object(self.video.os,'killpg',create=True):
            result = self.video.prepare(self.payload,self.base,timeout_seconds=10)
        self.assertEqual(result['status'],'partial')
        self.assertNotIn('error',result)
        self.Popen.reset_mock()
        stopped = self.video.prepare(self.payload,self.base,is_active=lambda:False)
        self.assertEqual(stopped['error'],'TURN_ALREADY_TAKEN_OVER')
        self.Popen.assert_not_called()

    def test_overview_coverage_and_ending_survive_many_segment_receipts(self):
        report = dict(self.report,overview='整体论证主线与结尾结论',duration=600,
            summaries=[{'start':i*30,'end':(i+1)*30,'text':'实际段落'+str(i)+'细节'*100} for i in range(20)],
            sampled_intervals=[{'start':i*30,'end':(i+1)*30,'modality':'mixed'} for i in range(20)])
        brief = self.video.brief(report)
        self.assertEqual(brief['overview'],report['overview'])
        self.assertEqual(brief['coverage_status'],'complete')
        self.assertEqual(brief['unobserved_intervals'],[])
        self.assertEqual(len(brief['summaries']),20)
        self.assertEqual(brief['summaries'][-1]['end'],600)
        self.assertIn('实际段落19',json.dumps(brief,ensure_ascii=False))
        self.assertEqual(next(iter(brief)),'overview')

    def test_hard_timeout_can_return_completed_native_observation_checkpoint(self):
        configuration=self.base/'watcher.json'
        configuration.write_text(json.dumps({'state_dir':str(self.base)}))
        with closing(sqlite3.connect(self.base/'qq_requests.sqlite3')) as db,db:
            db.execute('CREATE TABLE qq_requests (id TEXT PRIMARY KEY,result TEXT)')
            db.execute('INSERT INTO qq_requests VALUES (?,?)',('checkpoint',json.dumps(self.report)))
        report=self.video._saved_viewing_result(configuration,'checkpoint')
        self.assertEqual(report['status'],'partial')
        self.assertEqual(report['warning'],'QQ_VIDEO_TIMEOUT')
        self.assertEqual(report['overview'],self.report['overview'])
        self.assertEqual(report['evidence'],self.report['evidence'])

    def test_automatic_fallback_helpers_keep_structured_result_compatible(self):
        report = self.video.prepare(self.payload,self.base)
        self.payload['_video'] = report
        self.assertEqual(self.video.brief(self.payload['_video'])['evidence'][0]['text'],'确实听到钢琴')
        self.assertEqual(self.video.images(report,self.payload,self.base),[])
        self.assertEqual(self.video.selected_frames(['F1'],report,'group:123',self.base),[])


if __name__ == '__main__':
    unittest.main()
