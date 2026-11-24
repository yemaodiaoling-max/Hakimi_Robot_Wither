import json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from hakimi.ops import sol_repair as sol
from hakimi.ops import cloud_supervisor
from contextlib import contextmanager


@contextmanager
def layout(root):
    """Fake checkout: ALLOWED sources live in <root>/src/<name>."""
    root=Path(root);code=root/'src';code.mkdir(exist_ok=True)
    def src(name):
        if name not in sol.ALLOWED:raise ValueError('repair_edit_scope')
        return code/name
    with patch.object(sol,'src',src),patch.object(sol,'checkout',lambda:root):yield code

class SolRepairTests(unittest.TestCase):
    def test_fast_check_detects_long_pause_without_any_model_or_integrity_scan(self):
        with tempfile.TemporaryDirectory() as d,patch.object(cloud_supervisor,'RUNTIME',Path(d)):
            root=Path(d);sol.write(root/'official_worker.json',{'online':True,'at':1000})
            sol.write(root/'delivery_state.json',{'paused_until':2000})
            with patch.object(cloud_supervisor,'run',return_value=Mock(stdout='active\nactive\nactive\n')):
                self.assertIn('delivery_paused',cloud_supervisor.quick_faults(1000))
                sol.write(root/'delivery_state.json',{'paused_until':1015})
                self.assertEqual(cloud_supervisor.quick_faults(1000),[])

    def test_no_fault_never_starts_model(self):
        with tempfile.TemporaryDirectory() as d:
            runner=Mock();result=sol.run_cycle(d,[],{},1000,budget=Mock(),check=lambda:[],runner=runner)
            self.assertEqual(result['status'],'healthy');runner.assert_not_called()

    def test_duplicate_detection_one_call_and_cooldown_uses_shared_budget(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);code=self.enterContext(layout(root));(code/'official_transport.py').write_text('pass\n')
            runner=Mock(return_value=({'status':'report','summary':'需要核对固定连接指标。','edits':[]},{'input_tokens':100,'output_tokens':50}))
            budget=Mock();budget.reserve.return_value={'id':'r'}
            def run(now):return sol.run_cycle(root,['delivery_paused'],{},now,budget=budget,check=lambda:[],runner=runner)
            self.assertEqual(run(1000)['status'],'observing')
            self.assertEqual(run(1300)['status'],'report')
            self.assertEqual(run(1400)['status'],'cooldown')
            self.assertEqual(runner.call_count,1);budget.reserve.assert_called_once();budget.settle.assert_called_once()
            self.assertEqual(runner.call_args.kwargs['model'],'gpt-6.1-sol')
            self.assertNotIn('禁止',runner.call_args.args[0].split('\n')[0])

    def test_daily_two_calls_cap(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);code=self.enterContext(layout(root));(code/'official_transport.py').write_text('pass\n')
            runner=Mock(return_value=({'status':'report','summary':'人工查看。','edits':[]},{}));budget=Mock()
            for now in (1000,1300,3200,5200):sol.run_cycle(root,['delivery_paused'],{},now,budget=budget,check=lambda:[],runner=runner)
            self.assertEqual(runner.call_count,2)

    def test_patch_is_literal_unique_compiles_and_keeps_persona_outside_scope(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);code=self.enterContext(layout(root));(code/'official_transport.py').write_text('def f():\n    return 1\n')
            edit={'path':'official_transport.py','before':'return 1','after':'return 2'}
            self.assertIn('return 2',sol.changed_sources(root,[edit])['official_transport.py'])
            self.assertIn('return 1',(code/'official_transport.py').read_text())
            for name in ('prompt_context.py','style_guide.md','config.json','../secret'):
                with self.assertRaises(ValueError):sol.changed_sources(root,[dict(edit,path=name)])
            with self.assertRaises(SyntaxError):sol.changed_sources(root,[dict(edit,after='return (')])

    def test_pending_patch_waits_for_active_turn_without_model_call(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'official_test_runtime/state').mkdir(parents=True)
            sol.write(root/'official_test_runtime/state/turn_state.json',{'status':'primary_active','expires_at':10**12})
            self.assertEqual(sol.activate(root,{},lambda:[])['status'],'deferred')

    def test_validated_patch_updates_package_source_and_health_failure_rolls_back(self):
        for failure in (False,True):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as d:
                root=Path(d);runtime=root/'official_test_runtime';(runtime/'state').mkdir(parents=True)
                with layout(root) as code:
                    (code/'official_transport.py').write_text('VALUE=1\n')
                    job=root/'job';(job/'stage/src').mkdir(parents=True);(job/'stage/src/official_transport.py').write_text('VALUE=2\n')
                    pending={'job':str(job),'names':['official_transport.py'],'hashes':{'official_transport.py':sol.digest(code/'official_transport.py')}}
                    with patch.object(sol.subprocess,'run',return_value=Mock(returncode=0)) as services,patch.object(sol.time,'sleep'):
                        if failure:
                            with self.assertRaisesRegex(ValueError,'repair_health_not_restored'):sol.activate(root,pending,lambda:['gateway_heartbeat'])
                        else:self.assertEqual(sol.activate(root,pending,lambda:[])['status'],'applied')
                    self.assertEqual((code/'official_transport.py').read_text(),'VALUE=1\n' if failure else 'VALUE=2\n')
                    # The package is the only copy: nothing is written into the runtime folder.
                    self.assertFalse((runtime/'official_transport.py').exists())
                    self.assertTrue(any('start' in call.args[0] for call in services.call_args_list))

    def test_syntax_failure_never_activates_and_still_counts_usage(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);code=self.enterContext(layout(root));(code/'official_transport.py').write_text('def f():\n    return 1\n')
            runner=Mock(return_value=({'status':'patch','summary':'fix','edits':[{'path':'official_transport.py','before':'return 1','after':'return ('}]},{'input_tokens':1}))
            budget=Mock();sol.run_cycle(root,['delivery_paused'],{},1000,budget=budget,check=lambda:[],runner=runner)
            with patch.object(sol,'activate') as deploy:
                result=sol.run_cycle(root,['delivery_paused'],{},1300,budget=budget,check=lambda:[],runner=runner)
            self.assertEqual(result['status'],'failed');deploy.assert_not_called();budget.settle.assert_called_once()
            self.assertIn('return 1',(code/'official_transport.py').read_text())

if __name__=='__main__':unittest.main()
