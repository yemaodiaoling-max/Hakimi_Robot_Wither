"""Offline routing acceptance: no external requests or production credentials."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bili_companion.cognition.gateway import CognitionUnavailable, HYDROGEN
from bili_companion.cognition.live import CognitionEngine, BudgetPaused
from bili_companion.cognition.routing import HydrogenRouteEngine, PRIMARY, build_route
from bili_companion.memory import MemoryStore
from bili_companion.policy.budget import BudgetLedger
from bili_companion.runtime.storage import Database
from test_cognition_live import Client, response as synthetic_response


def response(*args,**kwargs):
    value=synthetic_response(*args,**kwargs)
    value['_provider_response_metadata']={'reported_model':PRIMARY}
    return value


class HydrogenRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name)/'calls.sqlite3')
        self.ledger = BudgetLedger(self.db, monthly='1', daily='1')
        self.memory = MemoryStore(self.db)

    def route(self, primary, backup, *, enabled=True):
        common = dict(clock=lambda:1793600000, max_output_tokens=128,
            price_resolver=lambda model, now:(Decimal('1'),Decimal('2')), request_usd_limit='.04')
        fallback = CognitionEngine(backup,self.ledger,self.memory,
            observer_model=HYDROGEN,decider_model=HYDROGEN,**common) if enabled else None
        return HydrogenRouteEngine(primary,self.ledger,self.memory,fallback=fallback,
            observer_model=PRIMARY,decider_model=PRIMARY,**common)

    def test_primary_success_never_calls_vertex(self):
        primary,backup=Client([response({})]),Client([])
        engine=self.route(primary,backup)
        engine._call(PRIMARY,{})
        self.assertEqual([m for m,_ in primary.requests],[PRIMARY])
        self.assertEqual(backup.requests,[])
        self.assertEqual([r['state'] for r in self.ledger.entries()],['settled'])

    def test_failed_primary_retains_reservation_and_backup_settles_separately(self):
        primary,backup=Client([TimeoutError()]),Client([response({})])
        engine=self.route(primary,backup)
        _,identity,_=engine._call(PRIMARY,{})
        self.assertEqual([m for m,_ in backup.requests],[HYDROGEN])
        self.assertEqual([r['state'] for r in self.ledger.entries()],['unknown','settled'])
        events=engine.role_events_for([identity])
        self.assertEqual([e['model'] for e in events],[PRIMARY,HYDROGEN])
        self.assertEqual([e['state'] for e in events],['unknown','settled'])

    def test_strict_primary_failure_never_calls_vertex(self):
        primary,backup=Client([TimeoutError()]),Client([])
        with self.assertRaises(CognitionUnavailable):
            self.route(primary,backup,enabled=False)._call(PRIMARY,{})
        self.assertEqual(backup.requests,[])
        self.assertEqual(len(self.ledger.entries()),1)

    def test_qq_summary_high_gets_8192_and_reserves_it_without_changing_backup(self):
        primary,backup=Client([TimeoutError()]),Client([response({})])
        engine=self.route(primary,backup)
        payload={'generationConfig':{'maxOutputTokens':128}}
        engine._call(PRIMARY,payload,purpose='video-qq-summary')
        self.assertEqual(primary.requests[0][1]['generationConfig']['maxOutputTokens'],8192)
        self.assertEqual(backup.requests[0][1]['generationConfig']['maxOutputTokens'],128)
        self.assertEqual(payload['generationConfig']['maxOutputTokens'],128)
        self.assertEqual(self.ledger.entries()[0]['reserved'],17548)

    def test_oversized_output_refused_outside_high_qq_summary(self):
        primary,backup=Client([]),Client([])
        with self.assertRaises(CognitionUnavailable):
            self.route(primary,backup,enabled=False)._call(PRIMARY,
                {'generationConfig':{'maxOutputTokens':8192}},purpose='video-observe')
        self.assertEqual(primary.requests,[])

    def test_paid_truncation_settles_primary_before_backup(self):
        primary,backup=Client([response({},finish='MAX_TOKENS')]),Client([response({})])
        self.route(primary,backup)._call(PRIMARY,{})
        self.assertEqual([r['state'] for r in self.ledger.entries()],['settled','settled'])

    def test_wrong_reported_model_is_rejected_after_settlement(self):
        wrong=response({});wrong['_provider_response_metadata']={'reported_model':'gemini-flash'}
        primary,backup=Client([wrong]),Client([response({})])
        self.route(primary,backup)._call(PRIMARY,{})
        self.assertEqual([r['state'] for r in self.ledger.entries()],['settled','settled'])
        self.assertEqual(len(backup.requests),1)

    def test_missing_reported_model_is_not_verified(self):
        primary,backup=Client([synthetic_response({})]),Client([response({})])
        self.route(primary,backup)._call(PRIMARY,{})
        self.assertEqual(len(backup.requests),1)

    def test_strict_truncation_cannot_fallback(self):
        primary,backup=Client([response({},finish='MAX_TOKENS')]),Client([])
        with self.assertRaises(CognitionUnavailable):
            self.route(primary,backup,enabled=False)._call(PRIMARY,{})
        self.assertEqual(backup.requests,[])
        self.assertEqual(self.ledger.entries()[0]['state'],'settled')

    def test_strict_build_never_initializes_vertex_and_production_caps_are_preserved(self):
        profile=SimpleNamespace(observer_model=PRIMARY,decider_model=PRIMARY,base_url='https://synthetic.example/v1',
            api_key_file='synthetic.key',prices=lambda model,now:(Decimal(1),Decimal(2)),raw=dict(
                single_pass=True,input_token_upper_bound=1000,media_mode='frames_audio',
                output_limit_parameter='max_tokens',max_output_tokens=2048))
        config={'provider_profile':'synthetic-profile.json','hydrogen_route':{'fallback_enabled':False}}
        with patch('bili_companion.cognition.routing.load_profile',return_value=profile), \
                patch('bili_companion.cognition.routing.OpenAICompatibleClient',return_value=Client([])), \
                patch('bili_companion.cognition.routing.GatewayClient.for_hydrogen',return_value=Client([])) as vertex:
            engine=build_route(config,self.ledger,self.memory)
            self.assertIsNone(engine.fallback)
            vertex.assert_not_called()
            config['hydrogen_route']['fallback_enabled']=True
            engine=build_route(config,self.ledger,self.memory)
            self.assertEqual(engine.fallback.max_output,1536)
            self.assertEqual(engine.fallback.request_limit,Decimal('.04'))

    def test_backup_denied_when_primary_liability_consumes_cap(self):
        self.ledger.daily=2000
        primary,backup=Client([TimeoutError()]),Client([response({})])
        with self.assertRaises(BudgetPaused) as caught:
            self.route(primary,backup)._call(PRIMARY,{})
        self.assertEqual(len(caught.exception.request_ids),1)
        self.assertEqual(backup.requests,[])
        self.assertEqual(self.ledger.entries()[0]['state'],'unknown')

    def test_concurrent_calls_keep_models_prices_and_unique_liabilities(self):
        barrier=threading.Barrier(2)
        class Primary:
            def count_tokens(self,model,payload):return 1000
            def generate(self,model,payload):
                if model != PRIMARY:raise AssertionError('wrong primary model')
                barrier.wait(timeout=3)
                if payload['fail']:raise TimeoutError()
                return response({})
        class Backup:
            def count_tokens(self,model,payload):return 1000
            def generate(self,model,payload):
                if model != HYDROGEN:raise AssertionError('wrong backup model')
                return response({})
        engine=self.route(Primary(),Backup())
        with ThreadPoolExecutor(max_workers=2) as pool:
            outputs=list(pool.map(lambda fail:engine._call(PRIMARY,{'fail':fail}),[True,False]))
        self.assertEqual(len({r['id'] for r in self.ledger.entries()}),3)
        self.assertEqual(sorted(r['state'] for r in self.ledger.entries()),['settled','settled','unknown'])
        self.assertEqual(engine.observer_model,PRIMARY)
        self.assertEqual(engine.fallback.observer_model,HYDROGEN)


if __name__ == '__main__':unittest.main()
