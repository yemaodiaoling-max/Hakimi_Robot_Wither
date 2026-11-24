import json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.prompt.prompt_context import render_prompt,render_vertex_prompt,activate_context
from hakimi.models.fallback_supervisor import fallback_prompt
from hakimi.models.fallback_runner import build_patch
from hakimi.prompt.prompt_language import DEFENSIVE,context as positive_context
from hakimi.models import model_trial
from hakimi.ops import terra_supervision
from tests.test_official_adapter import fixture_root

class TrialTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);fixture_root(self.root);self.base=self.root/'official_test_runtime'
        model_trial.write(self.root/'state/vertex_route.json',{'enabled':True,'model':model_trial.BASELINE})
        folder=self.root/'state/flash_lite_trial';folder.mkdir(exist_ok=True)
        (folder/'baseline_3_8.jsonl').write_text(json.dumps({'provider':'gemini','model':model_trial.BASELINE,'session_key':'group:100','message_id':'old','text':'先把这一项拆开看～','at':time.time()-500,'trigger_id':'old'})+'\n')

    def test_tagged_outputs_and_terra_comparison_share_the_existing_cycle(self):
        now=time.time();trial=model_trial.start(self.root,now=now)
        self.assertEqual(trial['ends_at']-trial['started_at'],86400)
        c=TurnCoordinator(self.base);c.publish({'session_key':'group:100','trigger_id':'new'});claim=c.claim_primary()
        self.assertTrue(c.submit_reply(claim['token'],['这步可以先拆开喵'],'gemini')['accepted'])
        out=json.loads((self.base/'state/outbox.jsonl').read_text());self.assertEqual(out['model'],model_trial.LITE)
        sent={'at':now+10,'provider':'gemini','session_key':'group:100','trigger_id':'new','message_id':'new-id','text':'这步可以先拆开喵'}
        with (self.base/'state/sent_messages.jsonl').open('a') as f:f.write(json.dumps(sent)+'\n')
        packet=model_trial.packet(self.root,now+20);self.assertEqual(packet['statistics']['trial']['observed_messages'],1)
        comparison={'verdict':'continue','summary':'口吻连贯，接话简洁，继续观察专业问答。','scores':{axis:{'baseline':4,'trial':4,'evidence_count':1} for axis in model_trial.AXES},'recommendations':['保持自然接话，公式按步骤展开。']}
        runner=Mock(return_value=({'style':{'verdict':'normal','guidance':'','uncertainty':'本轮覆盖日常接话。'},'relations':[],'comparison':comparison},{'input_tokens':100,'output_tokens':80}))
        budget=Mock();budget.reserve.return_value={'id':'fixture'}
        with patch.object(terra_supervision,'collect_dsh_samples',return_value=[]),patch.object(terra_supervision,'relation_candidates',return_value=[]):
            result=terra_supervision.run_cycle(self.root,now+20,runner=runner,budget=budget,force=True)
        self.assertEqual(result['status'],'completed');self.assertEqual(runner.call_count,1)
        self.assertIn('comparison',runner.call_args.args[1]['required'])
        self.assertIn('口吻连贯',model_trial.notice(self.root)['summary'])
        self.assertFalse(DEFENSIVE.search((self.root/'state/gemini_style_feedback.md').read_text()))

    def test_expiry_waits_for_real_tool_and_restores_model_after_turn(self):
        trial=model_trial.start(self.root,now=1000);trial['ends_at']=1001;model_trial.write(self.root/'state/model_trial.json',trial)
        clock=[1000.];c=TurnCoordinator(self.base,clock=lambda:clock[0]);c.publish({'session_key':'group:100','trigger_id':'active','created_at':1000});claim=c.claim_primary();c.tool_activity(claim['token'],'context.read',100)
        clock[0]=1002
        with patch('turn_coordinator.TurnCoordinator',return_value=c):
            self.assertEqual(model_trial.tick(self.root,1002)['status'],'finishing_active_turn')
            self.assertEqual(model_trial.read(self.root/'state/vertex_route.json')['model'],model_trial.LITE)
            c.tool_activity(claim['token']);c.complete(claim['token'])
            self.assertEqual(model_trial.tick(self.root,1003)['status'],'completed')
        self.assertEqual(model_trial.read(self.root/'state/vertex_route.json')['model'],model_trial.BASELINE)

    def test_all_rendered_instruction_prefixes_are_positive_and_data_remains_exact(self):
        payload={'session_key':'group:850245632','trigger_id':'prompt','recent_context_messages':[{'session_key':'group:850245632','user_id':'22','message_id':'1','text':'请不要替换这句原话 _untrusted'}]}
        c=TurnCoordinator(self.base);c.publish(payload);claim=c.claim_primary();activate_context(payload,self.base,claim=claim)
        values=[render_vertex_prompt(payload,self.base,claim),render_prompt(payload,self.base,claim),fallback_prompt(payload,self.base)]
        for value in values:
            head,_,data=value.rpartition('\n');json.loads(data)
            self.assertIsNone(DEFENSIVE.search(head),head)
            self.assertIn('请不要替换这句原话 _untrusted',data)
        patch_rows=build_patch(self.base/'runtime',{'provider':'deepseek-official','model':'deepseek-flash','api_key':'fixture-key','base_url':'https://api.deepseek.com'})
        prefix=next(row['config']['personaPrefix'] for row in patch_rows if row.get('id')=='system-prompt' and 'personaPrefix' in row.get('config',{}))
        self.assertIsNone(DEFENSIVE.search(prefix))

if __name__=='__main__':unittest.main()
