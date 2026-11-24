import json
import tempfile
import unittest
from pathlib import Path
from hakimi.models.task_routing import select, validate, FLASH, ARGON


class RoutingTests(unittest.TestCase):
    def test_local_rules_and_current_event_only(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, 'task_routing.json').write_text(json.dumps({'mode':'dual','single_model':ARGON}))
            def choose(text, **extra):
                row={'message_id':'2','user_id':'123','text':text, **extra}
                payload={'session_key':'group:12345','reply_target_message_id':'2',
                         'group_name':'严谨推导考据群','recent_context_messages':[
                         {'message_id':'1','user_id':'SELF','text':'详细推导全部证明'},row]}
                return select(d,payload,ARGON)
            for text in ['你好','你这证明什么','推导是什么意思','这张图好可爱','帮我查一下天气']:
                self.assertEqual(choose(text)[0],FLASH)
            for text in ['请详细推导这个方程','请证明这个定理','帮我推导这个方程','帮忙考据这段话的出处','先查资料再比较两种方案最后生成汇总']:
                self.assertEqual(choose(text)[0],ARGON)
            self.assertEqual(choose('这题怎么做',image_paths=['x'])[0],ARGON)
            self.assertEqual(choose('这图好看',image_paths=['x'])[0],FLASH)
            payload={'session_key':'group:12345','reply_target_message_id':'2','recent_context_messages':[
                {'message_id':'1','user_id':'456','text':'请详细推导'},
                {'message_id':'2','user_id':'123','text':'你好'}]}
            self.assertEqual(select(d,payload,ARGON)[0],FLASH)
            payload['internal_review']={}
            self.assertEqual(select(d,payload,ARGON)[0],ARGON)

    def test_hot_single_config_validation_and_session_wiring(self):
        import inspect
        from hakimi.ops import cloud_service
        with tempfile.TemporaryDirectory() as d:
            file=Path(d,'task_routing.json')
            for model in [FLASH,ARGON]:
                file.write_text(json.dumps({'mode':'single','single_model':model}))
                self.assertEqual(select(d,{},ARGON),(model,'single_config'))
            with self.assertRaises(ValueError): validate({'mode':'dual','single_model':'bad'})
            file.write_text('{broken')
            with self.assertRaises(ValueError): select(d,{},ARGON)
        from unittest.mock import patch
        from types import SimpleNamespace
        calls=[]
        instances=[]
        class FakeScoped:
            def __init__(self,args,cwd,env,state_dir,**kwargs):
                self.env=env; self.factory=kwargs['factory']; instances.append(self)
            def generate(self,prompt,coordinator,claim):
                calls.append((self.env['AGY_LLM_GATEWAY_MODELS'],claim['task_model_routing']))
                return {'status':'SUCCESS'}
            def close(self,*args): pass
        with tempfile.TemporaryDirectory() as d:
            route=SimpleNamespace(enabled=True,model=ARGON,gateway_directory=Path(d),
                client_environment=lambda env: dict(env,AGY_LLM_GATEWAY_MODELS=ARGON))
            payload={'session_key':'group:12345','reply_target_message_id':'1','recent_context_messages':[
                {'message_id':'1','user_id':'123','text':'你好'}]}
            with patch.object(cloud_service,'ROOT',Path(d)), patch.object(cloud_service,'_SESSION',None), \
                 patch.object(cloud_service,'_SESSION_SIGNATURE',None), \
                 patch('hakimi.models.vertex_route.load',return_value=route), \
                 patch('hakimi.models.vertex_route.enforce_primary'), \
                 patch('hakimi.models.scoped_session.ScopedSession',FakeScoped):
                file=Path(d,'task_routing.json')
                file.write_text(json.dumps({'mode':'dual','single_model':ARGON}))
                cloud_service.generate('prompt',None,{'payload':payload})
                self.assertEqual(calls[-1],(FLASH,{'model':FLASH,'reason':'routine_default'}))
                from hakimi.models.argon_session import ArgonSession
                self.assertIs(instances[-1].factory,ArgonSession)
                file.write_text(json.dumps({'mode':'single','single_model':FLASH}))
                cloud_service.generate('prompt',None,{'payload':payload})
                self.assertEqual(len(instances),1)
                self.assertEqual(calls[-1][1]['reason'],'single_config')
                file.write_text(json.dumps({'mode':'single','single_model':ARGON}))
                cloud_service.generate('prompt',None,{'payload':payload})
                self.assertEqual(calls[-1][0],ARGON)
                self.assertEqual(len(instances),2)
