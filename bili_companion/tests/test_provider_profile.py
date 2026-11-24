from copy import deepcopy
from datetime import datetime,timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bili_companion.cognition.gateway import CognitionUnavailable
from bili_companion.cognition.live import CognitionEngine
from bili_companion.cognition.profile import load_profile
from bili_companion.domain import Scope
from bili_companion.memory import MemoryStore
from bili_companion.policy.budget import BudgetLedger
from bili_companion.runtime.storage import Database
from bili_companion.runtime.watcher import WatchState,build
from test_cognition_live import Client,response,observed


class ProviderProfileTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[1]/'state/test_tmp';root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.now=datetime(2026,11,2,tzinfo=timezone.utc).timestamp()
        self.path=self.root/'profile.json';self.key=self.root/'private-provider.key'
        self.key.write_text('synthetic-test-key')
        self.model='gemini-3.8-flash-high'
        self.raw=dict(version=1,activated=True,provider='openai_compatible',base_url='https://provider.example/v1',
            api_key_file=str(self.key),observer_model=self.model,decider_model=self.model,supervisor_model=self.model,
            ledger_currency='USD',child_roles_enabled=True,reasoning_effort='low',reasoning_override_verified=False,
            review_valid_until='2026-11-30',input_token_upper_bound=16000,media_mode='frames_audio',
            output_limit_parameter='max_completion_tokens',verification={self.model:dict(audio=True,video=True,
                usage=True,structured_output=True,input_bound=True,output_token_cap=True,
                verified_at=self.now-1,base_url='https://provider.example/v1',output_limit_parameter='max_completion_tokens',max_output_tokens=1536,
                media_mode='frames_audio')},
            max_output_tokens=1536,prices={self.model:dict(
                input_usd_per_million='.075',output_usd_per_million='4',billing_currency='USD',
                source_price=dict(input_per_million='.075',output_per_million='4'),
                source='synthetic-account-price-review',input_modalities=['text','image','audio','video'])})

    def profile(self,raw=None):
        self.path.write_text(json.dumps(self.raw if raw is None else raw))
        return load_profile(self.path,clock=lambda:self.now)

    def test_default_budget_stays_ten_and_explicit_increase_is_bounded(self):
        profile=self.profile()
        self.assertEqual(profile.budgets({}),('10','1'))
        self.assertEqual(profile.budgets(dict(monthly_usd='40',daily_usd='4')),('40','4'))
        self.assertEqual(profile.budgets(dict(monthly_usd='60',daily_usd='6')),('60','6'))
        for monthly,daily in (('61','6'),('60','7'),('NaN','1')):
            with self.assertRaises((ValueError,CognitionUnavailable)):
                profile.budgets(dict(monthly_usd=monthly,daily_usd=daily))

    def test_actual_panel_opt_in_has_explicit_twenty_cent_ceiling(self):
        value=deepcopy(self.raw)
        value.update(usage_panel_actual_cost=True,payload_token_envelope=True,per_request_usd_limit='.20')
        self.assertEqual(self.profile(value).raw['per_request_usd_limit'],'.20')
        value['per_request_usd_limit']='.21'
        with self.assertRaises(CognitionUnavailable):self.profile(value)
        value['per_request_usd_limit']='.20'
        value['usage_panel_actual_cost']=False
        with self.assertRaises(CognitionUnavailable):self.profile(value)
        value['usage_panel_actual_cost']='true'
        with self.assertRaises(CognitionUnavailable):self.profile(value)

    def test_missing_activation_capabilities_origin_and_price_fail_closed(self):
        for mutation in ('inactive','audio','video','usage','input_bound','output_token_cap','origin','prices','currency','underpriced'):
            value=deepcopy(self.raw)
            if mutation=='inactive':value['activated']=False
            elif mutation=='origin':value['verification'][self.model]['base_url']='https://other.example/v1'
            elif mutation=='prices':value['prices']={}
            elif mutation=='currency':value['ledger_currency']='CNY'
            elif mutation=='underpriced':value['prices'][self.model]['input_usd_per_million']='.001'
            else:value['verification'][self.model][mutation]=False
            with self.subTest(mutation=mutation),self.assertRaises(CognitionUnavailable):self.profile(value)

    def test_review_expiry_and_unknown_model_never_use_legacy_prices(self):
        profile=self.profile()
        with self.assertRaises(CognitionUnavailable):profile.prices('gemini-3.5-flash-lite',self.now)
        with self.assertRaises(CognitionUnavailable):profile.prices(self.model,datetime(2026,12,1,tzinfo=timezone.utc).timestamp())
        self.assertEqual(profile.prices(self.model,self.now),(Decimal('.075'),Decimal('4')))

    def test_qq_profile_does_not_require_price_review_currency_or_usage_attestation(self):
        value=deepcopy(self.raw)
        value.pop('prices')
        value.pop('ledger_currency')
        value['review_valid_until']='2000-01-01'
        value['per_request_usd_limit']='not-a-price'
        value['verification'][self.model]['usage']=False
        self.path.write_text(json.dumps(value))
        profile=load_profile(self.path,clock=lambda:self.now,audit_costs=False)
        self.assertEqual(profile.observer_model,self.model)
        with self.assertRaises(CognitionUnavailable):load_profile(self.path,clock=lambda:self.now)

    def test_verified_media_mode_cap_and_explicit_request_limit_are_bound(self):
        value=deepcopy(self.raw)
        value.update(media_mode='mp4_image_url',input_token_upper_bound=150000,per_request_usd_limit='.03')
        with self.assertRaises(CognitionUnavailable):self.profile(value)
        value['verification'][self.model]['media_mode']='mp4_image_url'
        profile=self.profile(value)
        self.assertEqual(profile.raw['per_request_usd_limit'],'.03')
        value['per_request_usd_limit']='.11'
        with self.assertRaises(CognitionUnavailable):self.profile(value)
        value['per_request_usd_limit']='.03'
        value['max_output_tokens']=2048
        with self.assertRaises(CognitionUnavailable):self.profile(value)

    def test_cognition_uses_configured_models_prices_and_evidence_version(self):
        profile=self.profile();db=Database(self.root/'watcher.sqlite3')
        ledger=BudgetLedger(db,monthly='40',daily='4');memory=MemoryStore(db)
        client=Client([response(observed()),response(dict(speak=False,text='',evidence_indices=[]))])
        engine=CognitionEngine(client,ledger,memory,clock=lambda:self.now,observer_model=self.model,
            decider_model=self.model,price_resolver=profile.prices)
        media=self.root/'synthetic.mp4';media.write_bytes(b'synthetic')
        chunk=SimpleNamespace(path=media,start=0,end=8,received_at=self.now,modality='mixed',target_id='room',session_id='live')
        result=engine.observe(chunk,Scope('a','live_room','room'),1)
        self.assertEqual([model for model,_ in client.requests],[self.model,self.model])
        self.assertTrue(result.observation.evidence[0].version.startswith(self.model+':'))
        self.assertEqual([entry['actual'] for entry in ledger.entries()],[555,555])

    def test_custom_model_requires_explicit_price_resolver(self):
        db=Database(self.root/'watcher.sqlite3')
        with self.assertRaises(CognitionUnavailable):
            CognitionEngine(Client([]),BudgetLedger(db),MemoryStore(db),observer_model=self.model)

    def test_single_pass_profile_explicitly_disables_extra_child_generation(self):
        value=deepcopy(self.raw)
        value.update(single_pass=True,child_roles_enabled=False)
        profile=self.profile(value)
        self.assertTrue(profile.raw['single_pass'])
        self.assertFalse(profile.raw['child_roles_enabled'])
        for single,child in ((True,True),(False,False),('true',False)):
            value.update(single_pass=single,child_roles_enabled=child)
            with self.assertRaises(CognitionUnavailable):self.profile(value)

    def test_runtime_migration_keeps_unknown_ledger_and_state_and_no_gateway_dependency(self):
        self.profile()
        state_dir=self.root/'watch';db=Database(state_dir/'watcher.sqlite3')
        ledger=BudgetLedger(db)
        ledger.reserve('legacy-unknown','.02',self.now);ledger.ambiguous('legacy-unknown')
        WatchState(db,'8729095131').change(lambda s:s.update(watch_epoch=17,runtime_version=2,next_observation_at=12345))
        captured={}
        class Adapter:
            def __init__(inner,*args,**kwargs):captured.update(kwargs)
        config=dict(provider_profile=str(self.path),monthly_usd='40',daily_usd='4',account_id='hakimi-bilibili',
            priority_room='8729095131',state_dir=str(state_dir),native_database=str(self.root/'native.sqlite3'),
            bridge_database=str(self.root/'bridge.sqlite3'),auth_state_dir=str(self.root/'auth'))
        with patch.dict(sys.modules,{'bili_companion.cognition.openai_provider':SimpleNamespace(OpenAICompatibleClient=Adapter)}):
            watcher=build(config)
        self.assertEqual(watcher.engine.ledger.monthly,40_000_000)
        self.assertEqual(watcher.engine.ledger.daily,4_000_000)
        self.assertEqual(watcher.engine.ledger.entries()[0]['state'],'unknown')
        self.assertEqual(watcher.engine.ledger.entries()[0]['reserved'],20_000)
        self.assertEqual(watcher.state.snapshot()['watch_epoch'],17)
        self.assertEqual(watcher.state.snapshot()['next_observation_at'],12345)
        self.assertEqual(captured['input_token_upper_bound'],16000)
        self.assertEqual(watcher.engine.decision_interval,300)

    def test_hydrogen_routes_watcher_and_qq_workers_through_vertex(self):
        from bili_companion.cognition.gateway import HYDROGEN,GatewayClient
        from bili_companion.runtime.qq_request import build as build_qq
        config=dict(vertex_model=HYDROGEN,provider_profile='unused',account_id='hakimi-bilibili',
            gateway_key_file=str(self.key),gateway_ca_file=str(self.key),priority_room='8729095131',
            state_dir=str(self.root/'watch'),native_database=str(self.root/'native.sqlite3'),
            bridge_database=str(self.root/'bridge.sqlite3'),auth_state_dir=str(self.root/'auth'),
            monthly_usd='60',daily_usd='6')
        watcher=build(config)
        qq=build_qq(config)
        for engine in (watcher.engine,qq.engine,qq.engine_factory()):
            self.assertIsInstance(engine.client,GatewayClient)
            self.assertEqual((engine.observer_model,engine.decider_model),(HYDROGEN,HYDROGEN))
        self.assertEqual(watcher.engine.price_resolver(HYDROGEN,self.now),(Decimal('1.65'),Decimal('8.25')))
        self.assertEqual(watcher.engine.ledger.monthly,60_000_000)
        config['monthly_usd']='96'
        self.assertEqual(build(config).engine.ledger.monthly,96_000_000)
        config['monthly_usd']='96.01'
        with self.assertRaises(ValueError):build(config)

    def test_single_pass_build_uses_actual_send_cooldown(self):
        value=deepcopy(self.raw)
        value.update(single_pass=True,child_roles_enabled=False)
        self.profile(value)
        config=dict(provider_profile=str(self.path),account_id='hakimi-bilibili',priority_room='8729095131',
            state_dir=str(self.root/'watch'),native_database=str(self.root/'native.sqlite3'),
            bridge_database=str(self.root/'bridge.sqlite3'),auth_state_dir=str(self.root/'auth'))
        for configured,expected in ((30,90),(180,180)):
            with self.subTest(configured=configured):
                config['cooldown_seconds']=configured
                with patch.dict(sys.modules,{'bili_companion.cognition.openai_provider':SimpleNamespace(OpenAICompatibleClient=lambda *args,**kwargs:None)}):
                    watcher=build(config)
                self.assertEqual(watcher.engine.decision_interval,expected)
                self.assertEqual(watcher.executor.config['cooldown_seconds'],expected)


if __name__=='__main__':unittest.main()
