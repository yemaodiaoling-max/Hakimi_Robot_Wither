import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch,MagicMock

if os.name=='nt':
    with patch.dict(sys.modules,{'fcntl':MagicMock()}):supervisor=importlib.import_module('cloud_supervisor')
else:
    from hakimi.ops import cloud_supervisor as supervisor


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.state=self.root/'state';self.state.mkdir()
        self.scope=patch.multiple(supervisor,ROOT=self.root,STATE=self.state,RUNTIME=self.state)
        self.scope.start();self.addCleanup(self.scope.stop)

    def test_style_adapts_only_with_new_samples_and_pauses_after_primary_return(self):
        state={}
        sample={'dsh_ids':['1'],'dsh':['一句'],'gemini':['原口吻'],'baseline_source':'真实样本'}
        with patch.object(supervisor,'takeover',return_value=True),patch.object(supervisor,'capture_samples',return_value=sample),\
             patch.object(supervisor,'budgeted_consult',return_value={'answer':'正常：自然接话','uncertainty':''}) as model:
            supervisor.style_check(state,1000);self.assertEqual(state['next_due'],1300)
            supervisor.style_check(state,1300);self.assertEqual(state['interval_minutes'],15)
            supervisor.style_check(state,state['next_due']);self.assertEqual(model.call_count,1)
            sample['dsh_ids']=['2'];model.return_value={'answer':'问题：少用句号和客服结尾','uncertainty':''}
            supervisor.style_check(state,state['next_due']);self.assertEqual(state['interval_minutes'],5)
            self.assertTrue((self.state/'dsh_style_feedback.md').exists())
        with patch.object(supervisor,'takeover',return_value=False):supervisor.style_check(state,5000)
        self.assertFalse(state['active'])

    def test_daily_shared_budget_reserves_and_accounts_usage(self):
        limit=supervisor.STYLE_CALL_LIMIT
        with patch.object(supervisor,'consult',return_value=({'answer':'正常','uncertainty':''},{'input_tokens':8000,'output_tokens':100})) as model:
            for _ in range(limit):supervisor.budgeted_consult({},'style',1000)
            with self.assertRaisesRegex(RuntimeError,'daily_budget_exhausted'):
                supervisor.budgeted_consult({},'style',1000)
            budget=json.loads((self.state/'budget.json').read_text())
            self.assertEqual(budget['tokens'],limit*8100);self.assertEqual(model.call_count,limit)
            self.assertEqual(budget['style_calls'],limit)

    def test_failed_call_keeps_budget_reservation(self):
        with patch.object(supervisor,'consult',side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError):supervisor.budgeted_consult({},'fault',1000)
        budget=json.loads((self.state/'budget.json').read_text())
        self.assertEqual(budget['tokens'],12000);self.assertEqual(budget['fault_calls'],1)

    def test_report_checks_current_health_without_running_model(self):
        (self.state/'latest.json').write_text('{"status":"healthy"}')
        capture=io.StringIO()
        with patch.object(supervisor,'inspect',return_value=['gateway_heartbeat']),\
             patch.object(sys,'argv',['supervisor','--report']),contextlib.redirect_stdout(capture):
            supervisor.main()
        value=json.loads(capture.getvalue())
        self.assertEqual(value['health']['status'],'needs_attention')
        self.assertEqual(value['report']['status'],'healthy')

    def test_five_minute_dispatch_does_not_increase_two_hour_health_frequency(self):
        with patch.object(supervisor,'style_check'),patch.object(supervisor,'quick_faults',return_value=[]) as quick,patch.object(supervisor,'inspect',return_value=[]) as inspect,\
             patch.object(supervisor,'budgeted_consult') as model,patch.object(sys,'argv',['supervisor']):
            for now in range(1000,8201,300):
                with patch.object(supervisor.time,'time',return_value=now):supervisor.main()
            self.assertEqual(inspect.call_count,2)
            model.assert_not_called()

    def test_gemini_uses_frozen_baseline_and_only_new_gemini_samples(self):
        runtime=self.root/'official_test_runtime';runtime.mkdir()
        (runtime/'group_profiles.json').write_text(json.dumps({'default_profile':'same'}),encoding='utf-8')
        baseline={'provider':'gemini','provenance':'recorded_gemini_output','source':{'file':'old'},
                  'session_key':'group:2','text_segments':['Medium 原口吻'],'id':'old'}
        (self.state/'gemini_medium_style_baseline.jsonl').write_text(json.dumps(baseline,ensure_ascii=False)+'\n',encoding='utf-8')
        rows=[{'at':100,'session_key':'group:1','provider':'gemini','message_id':'old-low','text':'旧样本'},
              {'at':200,'session_key':'group:1','provider':'dsh','message_id':'d','text':'dsh 样本'},
              {'at':300,'session_key':'group:1','provider':'gemini','message_id':'g','text':'Low 当前发言'}]
        (self.state/'sent_messages.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')
        sample=supervisor.gemini_samples(150)
        self.assertEqual(sample['current'],['Low 当前发言'])
        self.assertEqual(sample['baseline'],['Medium 原口吻'])
        self.assertIn('同风格群',sample['baseline_source'])

    def test_gemini_style_is_exclusive_deduplicated_and_clears_old_feedback(self):
        state={};sample={'ids':['g1'],'current':['Low'],'baseline':['Medium'],'baseline_source':'固定基准'}
        with patch.object(supervisor,'gemini_samples',return_value=sample),\
             patch.object(supervisor,'budgeted_consult',return_value={'answer':'问题：语气过于生硬'}) as model:
            supervisor.gemini_style_check(state,1000,True);model.assert_not_called()
            supervisor.gemini_style_check(state,1000,False);self.assertEqual(state['next_due'],1300)
            supervisor.gemini_style_check(state,1300,False);self.assertEqual(state['interval_minutes'],5)
            self.assertIn('问题', (self.state/'gemini_style_feedback.md').read_text(encoding='utf-8'))
            supervisor.gemini_style_check(state,1600,False);self.assertEqual(model.call_count,1)
            sample['ids']=['g2'];model.return_value={'answer':'正常：语气一致'}
            supervisor.gemini_style_check(state,1900,False)
            self.assertEqual(state['interval_minutes'],15)
            self.assertEqual((self.state/'gemini_style_feedback.md').read_text(encoding='utf-8'),'')

    def test_gemini_and_dsh_share_style_budget(self):
        limit=supervisor.STYLE_CALL_LIMIT
        with patch.object(supervisor,'consult',return_value=({'answer':'正常','uncertainty':''},{'input_tokens':100,'output_tokens':10})):
            for _ in range(limit-1):supervisor.budgeted_consult({'target':'dsh'},'style',1000)
            supervisor.budgeted_consult({'target':'gemini_low'},'style',1000)
            with self.assertRaisesRegex(RuntimeError,'daily_budget_exhausted'):
                supervisor.budgeted_consult({'target':'gemini_low'},'style',1000)
