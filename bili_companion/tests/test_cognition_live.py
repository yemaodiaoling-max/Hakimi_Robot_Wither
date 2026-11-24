import base64
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bili_companion.cognition.live import CognitionEngine, BudgetPaused, CognitionUnavailable, OBSERVER, DECIDER
from bili_companion.cognition.gateway import GatewayClient, _NoRedirect
from bili_companion.domain import Scope
from bili_companion.memory import MemoryStore
from bili_companion.policy.budget import BudgetLedger
from bili_companion.runtime.storage import Database


def response(value, *, finish='STOP', thoughts=20):
    return {'candidates':[{'finishReason':finish,'content':{'parts':[{'text':json.dumps(value)}]}}],
            'usageMetadata':{'promptTokenCount':1000,'candidatesTokenCount':100,
                             'thoughtsTokenCount':thoughts,'totalTokenCount':1100+thoughts}}


def observed(*, interesting=True, modality='audio', end=5):
    return {'events':[{'text':'合成测试音轨中有钢琴声','start':0,'end':end,
                      'modality':modality,'confidence':.9}],
            'missing_modalities':[], 'interesting':interesting}


class Client:
    def __init__(self, replies):
        self.replies, self.requests, self.counted = list(replies), [], []

    def count_tokens(self, model, payload):
        self.counted.append((model,payload))
        return 1000

    def generate(self, model, payload):
        self.requests.append((model,payload))
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class CognitionTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]/'state'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026,11,1,12,tzinfo=timezone.utc).timestamp()
        self.db = Database(self.root/'test.sqlite3')
        self.ledger = BudgetLedger(self.db)
        self.memory = MemoryStore(self.db)
        path = self.root/'synthetic.mp4'
        path.write_bytes(b'synthetic-media-placeholder-not-a-real-watch')
        self.chunk = SimpleNamespace(path=path,start=120.,end=150.,received_at=self.now,
                                     modality='mixed',target_id='room',session_id='session')
        self.scope = Scope('a','live_room','room')

    def engine(self, replies, **kwargs):
        client = Client(replies)
        engine = CognitionEngine(client,self.ledger,self.memory,clock=lambda:self.now,**kwargs)
        return engine, client

    def test_panel_unattributed_success_retains_debt_without_second_generation(self):
        engine,client=self.engine([response(observed())])
        client.usage_panel_actual_cost=True
        result,identity,counts=engine._call(OBSERVER,engine._payload('instruction',{}))
        self.assertEqual(len(client.requests),1)
        row=self.ledger.entries()[0]
        self.assertEqual(row['state'],'unknown')
        self.assertGreater(row['reserved'],0)
        self.assertEqual(result['candidates'][0]['finishReason'],'STOP')

    def test_panel_actual_zero_settles_without_nominal_rate_charge(self):
        from decimal import Decimal
        reply=response(observed());reply['_provider_actual_cost_usd']=Decimal(0)
        engine,client=self.engine([reply]);client.usage_panel_actual_cost=True
        engine._call(OBSERVER,engine._payload('instruction',{}))
        row=self.ledger.entries()[0]
        self.assertEqual((row['state'],row['actual']),('settled',0))

    def test_actual_media_payload_grounding_and_thinking_billing(self):
        engine, client = self.engine([response(observed()),response({'speak':True,'text':'这段钢琴好听','evidence_indices':[0]})])
        result = engine.observe(self.chunk,self.scope,3)
        self.assertEqual([m for m,_ in client.requests],[OBSERVER,DECIDER])
        part = client.requests[0][1]['contents'][0]['parts'][0]
        self.assertEqual(base64.b64decode(part['inlineData']['data']),self.chunk.path.read_bytes())
        self.assertEqual(part['videoMetadata']['fps'],.2)
        self.assertNotIn('mediaProcessing',part)  # Static default; deployed Vertex rejects explicit field.
        self.assertNotIn('tools',client.requests[0][1])
        evidence = result.observation.evidence[0]
        self.assertEqual((evidence.media_start,evidence.media_end),(120,125))
        self.assertEqual(evidence.observed_at,self.now-25)
        self.assertEqual(result.observation.coverage,((120,125),))
        self.assertEqual(result.candidate.watch_epoch,3)
        self.assertEqual(result.candidate.expires_at,self.now+35)
        self.assertEqual(self.memory.recall(self.scope,'钢琴'),[])  # Caller must epoch-fence before writing.
        rows = self.ledger.entries()
        self.assertEqual(rows[0]['actual'],600)  # .0003 input + .0003 output INCLUDING 20 thoughts.
        self.assertEqual(rows[1]['actual'],1200)
        self.assertTrue(all(row['state']=='settled' for row in rows))

    def test_budget_pause_before_generation_timeout_unknown_and_no_retry(self):
        tiny = BudgetLedger(Database(self.root/'tiny.sqlite3'),monthly='.001',daily='.001')
        client = Client([])
        engine = CognitionEngine(client,tiny,self.memory,clock=lambda:self.now)
        with self.assertRaises(BudgetPaused):
            engine.observe(self.chunk,self.scope,1)
        self.assertEqual(client.requests,[])
        engine,client = self.engine([TimeoutError('sensitive upstream detail')])
        with self.assertRaises(CognitionUnavailable) as error:
            engine.observe(self.chunk,self.scope,1)
        self.assertNotIn('sensitive',str(error.exception))
        self.assertEqual(len(client.requests),1)
        self.assertEqual(self.ledger.entries()[0]['state'],'unknown')

    def test_invalid_or_truncated_outputs_never_create_evidence_but_are_settled(self):
        invalid = [response(observed(end=31)), response(observed(),finish='MAX_TOKENS'),
                   response({**observed(),'tool':'send'}),response(observed(modality='metadata'))]
        for reply in invalid:
            with self.subTest(reply=reply):
                engine,_ = self.engine([reply])
                result = engine.observe(self.chunk,self.scope,1)
                self.assertEqual(result.status,'invalid_observation')
                self.assertEqual(result.observation.evidence,())
        self.assertTrue(all(row['state']=='settled' for row in self.ledger.entries()))
        engine,_ = self.engine([{'candidates':[]}])
        with self.assertRaises(CognitionUnavailable):
            engine.observe(self.chunk,self.scope,1)
        self.assertEqual(self.ledger.entries()[-1]['state'],'unknown')

    def test_decision_throttle_persists_and_stale_candidate_is_suppressed(self):
        engine,client = self.engine([response(observed()),response({'speak':True,'text':'你好','evidence_indices':[0]})])
        self.chunk.received_at = self.now-90
        first = engine.observe(self.chunk,self.scope,2)
        self.assertIsNone(first.candidate)
        engine,client = self.engine([response(observed())])
        self.chunk.received_at = self.now
        result = engine.observe(self.chunk,self.scope,2)
        self.assertEqual(len(client.requests),1)
        self.assertIsNone(result.candidate)

    def test_missing_modality_and_fabricated_reference_are_rejected(self):
        bad = observed(modality='video')
        bad['missing_modalities']=['video']
        engine,_=self.engine([response(bad)])
        self.assertEqual(engine.observe(self.chunk,self.scope,1).status,'invalid_observation')
        engine,_=self.engine([response(observed()),response({'speak':True,'text':'弹幕','evidence_indices':[99]})])
        self.assertEqual(engine.observe(self.chunk,self.scope,1).status,'invalid_decision')

    def test_overrun_allows_next_call_and_price_review_fails_closed(self):
        engine,client=self.engine([response(observed(interesting=False),thoughts=5000),
                                   response(observed(interesting=False))])
        engine.observe(self.chunk,self.scope,1)
        self.assertTrue(self.ledger.entries()[0]['actual']>self.ledger.entries()[0]['reserved'])
        engine.observe(self.chunk,self.scope,1)
        self.assertEqual(len(client.requests),2)
        self.now=datetime(2027,1,1,tzinfo=timezone.utc).timestamp()
        with self.assertRaises(CognitionUnavailable):
            engine.observe(self.chunk,self.scope,1)

    def test_gateway_fixed_target_and_redirect_rejection(self):
        with self.assertRaises(CognitionUnavailable):
            _NoRedirect().redirect_request(None,None,302,'',{},'https://evil.test')
        client=GatewayClient('unused','unused')
        with self.assertRaises(ValueError):
            client.generate('arbitrary-model',{})


if __name__=='__main__':
    unittest.main()

