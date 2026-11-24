import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from hakimi.models.task_routing import select, validate, FLASH, ARGON, CARBON


class CarbonCompanionTests(unittest.TestCase):
    def test_carbon_wait_is_bounded_by_turn_deadline_and_flash_wait_unchanged(self):
        from unittest.mock import patch,MagicMock
        from hakimi.models.vertex_session import VertexSession
        for model,remaining,expected in [(CARBON,420,200),(CARBON,20,20),(FLASH,420,48),(ARGON,420,48)]:
            response=MagicMock()
            response.__enter__.return_value=response
            response.__iter__.return_value=iter([b'data: {"candidates":[{"finishReason":"STOP","content":{"parts":[]}}]}\n',b'\n'])
            response.headers={}
            opener=MagicMock();opener.open.return_value=response
            env={'AGY_LLM_GATEWAY_URL':'https://127.0.0.1:18765','AGY_LLM_GATEWAY_MODELS':model,
                 'AGY_LLM_GATEWAY_CA_CERT':'test-cert','AGY_LLM_GATEWAY_API_KEY':'test-only'}
            with patch('hakimi.models.vertex_session.ssl.create_default_context'),patch('hakimi.models.vertex_session.build_opener',return_value=opener):
                VertexSession([],Path('.'),env)._request({'contents':[]},remaining)
            self.assertEqual(opener.open.call_args.kwargs['timeout'],expected)

    def test_only_explicit_dual_companion_changes_and_internal_reviews_stay_argon(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path=Path(directory)/'task_routing.json'
            payload={'session_key':'group:123','reply_target_message_id':'q','recent_context_messages':[
                {'message_id':'q','user_id':'1','text':'请详细推导这个方程'}]}
            config={'mode':'dual','single_model':ARGON}
            path.write_text(json.dumps(config))
            self.assertEqual(select(directory,payload,ARGON)[0],ARGON)
            config['dual_model']=CARBON;path.write_text(json.dumps(config))
            self.assertEqual(select(directory,payload,ARGON)[0],CARBON)
            for text,extra in [('帮忙考据这段话的出处',{}),
                               ('先查资料再比较两种方案最后生成汇总',{}),
                               ('这题怎么做',{'image_paths':['fixture']})]:
                payload['recent_context_messages'][0].update(text=text,**extra)
                self.assertEqual(select(directory,payload,ARGON)[0],CARBON)
            payload['recent_context_messages'][0].pop('image_paths',None)
            payload['recent_context_messages'][0]['text']='你好'
            self.assertEqual(select(directory,payload,ARGON)[0],FLASH)
            payload['internal_review']={}
            self.assertEqual(select(directory,payload,ARGON)[0],ARGON)
            config['mode']='single';path.write_text(json.dumps(config))
            payload.pop('internal_review')
            self.assertEqual(select(directory,payload,ARGON)[0],ARGON)
            config['single_model']=CARBON;path.write_text(json.dumps(config))
            self.assertEqual(select(directory,payload,ARGON)[0],CARBON)
            for value in ('bad',FLASH):
                with self.assertRaises(ValueError):validate(dict(config,dual_model=value))

    @unittest.skip('private gateway registration script is not distributed')
    def test_carbon_gateway_patch_is_idempotent_and_preserves_old_prices(self):
        root=Path(__file__).resolve().parents[1]
        spec=importlib.util.spec_from_file_location('register_carbon',root/'deploy/register_carbon_gateway.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        source_path=root/'gateway/credit_ledger.py'
        if not source_path.exists():
            source_path=Path.home()/'.local/share/hakimi-gateway/credit_ledger.py'
        # This test reconstructs the pre-Carbon ledger. Use the snapshot
        # preceding the subsequent Hydrogen migration for that historical check.
        legacy = source_path.parent/'rollback/hydrogen_20261112/credit_ledger.py'
        if legacy.exists():
            source_path = legacy
        source=source_path.read_text(encoding='utf-8') if source_path.exists() else ''
        if not source:
            self.skipTest('gateway pricing fixture is tested separately during deployment')
        source=module.patch(source)
        self.assertEqual(module.patch(source),source)
        original = source.replace("CARBON_MODEL = 'gemini-4-carbon'\n",'').replace(
            'MODEL, LITE_MODEL, ARGON_MODEL, CARBON_MODEL','MODEL, LITE_MODEL, ARGON_MODEL').replace(
            "    if model == CARBON_MODEL:\n        # Conservative published standard rates; no assumed promotion expiry.\n        return Price(4000, 200, 20000, 'global-gemini-4-carbon-standard-ceiling')\n",'').replace(
            '    if model == CARBON_MODEL:\n        return price_at(model=model)\n','')
        self.assertEqual(module.patch(original),source)
        gateway_source=(root/'gateway/gateway.py').read_text(encoding='utf-8') if (root/'gateway/gateway.py').exists() else (Path.home()/'.local/share/hakimi-gateway/gateway.py').read_text(encoding='utf-8')
        gateway_source=module.patch_gateway(gateway_source)
        original_gateway=gateway_source.replace("upstream = urlopen(req, timeout=90 if request_model == 'gemini-4-carbon' else 45)", 'upstream = urlopen(req, timeout=45)')
        self.assertEqual(module.patch_gateway(original_gateway),gateway_source)
        self.assertEqual(module.patch_gateway(gateway_source),gateway_source)
        namespace={'__name__':'carbon_price_test'}
        import sys,types
        testmodule=types.ModuleType(namespace['__name__']);sys.modules[testmodule.__name__]=testmodule
        try:
            exec(compile(source,'credit_ledger.py','exec'),testmodule.__dict__)
            price=testmodule.price_at(model=CARBON)
            self.assertEqual(price.rates(),(4000,200,20000))
            self.assertEqual(testmodule.max_price(CARBON),price)
            self.assertEqual(testmodule.price_at('2026-11-10',ARGON).rates(),(2000,100,10000))
            self.assertEqual(testmodule.price_at('2026-11-10','gemini-3.8-flash').rates(),(750,75,3750))
        finally:sys.modules.pop(testmodule.__name__,None)


if __name__=='__main__':unittest.main()
