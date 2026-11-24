from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from bili_companion.cognition.live import CognitionEngine
from bili_companion.cognition.roles import RoleTask
from bili_companion.domain import Scope,Evidence,Observation
from bili_companion.memory import MemoryStore
from bili_companion.policy.budget import BudgetLedger
from bili_companion.runtime.storage import Database
from test_cognition_live import Client,response,observed


class RoleDispatchTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[1]/'state/test_tmp';root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.now=1_793_534_400
        self.db=Database(self.root/'roles.sqlite3');self.memory=MemoryStore(self.db)
        self.scope=Scope('a','live_room','room')
        path=self.root/'synthetic.mp4';path.write_bytes(b'synthetic')
        self.chunk=SimpleNamespace(path=path,start=0,end=8,received_at=self.now,modality='mixed',target_id='room',session_id='live')

    def parent(self,role=None):
        value=observed(interesting=False)
        value['child_request']=None if role is None else dict(role=role,reason='需要专注处理本次事实',evidence_indices=[0])
        return response(value)

    def engine(self,replies,*,active=lambda *args:True,daily='1'):
        client=Client(replies)
        engine=CognitionEngine(client,BudgetLedger(self.db,daily=daily),self.memory,clock=lambda:self.now,
            child_roles_enabled=True,epoch_is_current=active)
        return engine,client

    def test_no_request_is_one_call_and_no_child(self):
        engine,client=self.engine([self.parent()])
        result=engine.observe(self.chunk,self.scope,3)
        self.assertEqual(len(client.requests),1);self.assertEqual(result.child_status,'not_requested')
        self.assertEqual(engine.role_events_for(result.request_ids)[0]['role'],'supervisor')

    def test_expression_child_is_real_second_call_same_ledger_and_grounded_candidate(self):
        engine,client=self.engine([self.parent('expression'),response(dict(speak=True,text='这段钢琴好听',evidence_indices=[0]))])
        result=engine.observe(self.chunk,self.scope,3)
        self.assertEqual(result.child_status,'completed');self.assertEqual(result.candidate.text,'这段钢琴好听')
        self.assertEqual(len(client.requests),2);self.assertEqual(len(engine.ledger.entries()),2)
        events=engine.role_events_for(result.request_ids)
        self.assertEqual([e['role'] for e in events],['supervisor','expression'])
        self.assertEqual(events[1]['parent_id'],result.request_ids[0])
        self.assertTrue(all(e['currency']=='USD' and e['cost_usd']>0 for e in events))
        retry=engine.dispatcher.dispatch(RoleTask('expression','again',(0,)),observation=result.observation,
            scope=self.scope,session='live',epoch=3,parent_id=result.request_ids[0])
        self.assertEqual(retry.status,'round_limit');self.assertEqual(len(client.requests),2)

    def test_memory_child_preserves_observation_and_returns_only_grounded_summary(self):
        engine,client=self.engine([self.parent('memory'),response(dict(summary='本段听到钢琴，画面未知',evidence_indices=[0]))])
        result=engine.observe(self.chunk,self.scope,3)
        self.assertEqual(result.summary_evidence_ids,(result.observation.evidence[0].id,))
        self.assertEqual(result.summary,'本段听到钢琴，画面未知');self.assertEqual(len(client.requests),2)

    def test_invalid_role_recursion_and_unbound_epoch_never_dispatch(self):
        engine,client=self.engine([self.parent('shell')])
        result=engine.observe(self.chunk,self.scope,3)
        self.assertEqual(result.child_status,'invalid_request');self.assertEqual(len(client.requests),1)
        result=engine.dispatcher.dispatch(RoleTask('memory','nested',(0,)),observation=result.observation,
            scope=self.scope,session='live',epoch=3,parent_id='nested',depth=1)
        self.assertEqual(result.status,'denied')
        engine,client=self.engine([self.parent('memory')],active=lambda *args:False)
        result=engine.observe(self.chunk,self.scope,3)
        self.assertEqual(result.child_status,'stale_or_unbound_epoch');self.assertEqual(len(client.requests),1)

    def test_child_output_cannot_recursively_request_another_child(self):
        engine,client=self.engine([self.parent('memory'),response(dict(summary='只保留事实',evidence_indices=[0],child_request={'role':'expression'}))])
        result=engine.observe(self.chunk,self.scope,3)
        self.assertEqual(result.child_status,'unavailable_or_invalid')
        self.assertEqual(result.summary,'');self.assertEqual(len(result.observation.evidence),1)
        self.assertEqual(len(client.requests),2)

    def test_epoch_change_during_child_preflight_prevents_generation(self):
        active=[True]
        engine,client=self.engine([self.parent('memory')],active=lambda *args:active[0])
        original=client.count_tokens
        def counted(model,payload):
            value=original(model,payload)
            if len(client.counted)==2:active[0]=False
            return value
        client.count_tokens=counted
        result=engine.observe(self.chunk,self.scope,3)
        self.assertEqual(len(client.requests),1)
        self.assertEqual(len(engine.ledger.entries()),1)
        self.assertEqual(result.child_status,'unavailable_or_invalid')

    def test_child_budget_pause_keeps_paid_parent_evidence_and_timeout_liability(self):
        engine,client=self.engine([self.parent('memory')],daily='.0024')
        result=engine.observe(self.chunk,self.scope,3)
        self.assertEqual(result.child_status,'budget_paused');self.assertEqual(len(client.requests),1)
        self.assertEqual(len(result.observation.evidence),1)
        engine,client=self.engine([self.parent('memory'),TimeoutError()])
        result=engine.observe(self.chunk,self.scope,4)
        self.assertEqual(result.child_status,'unavailable_or_invalid')
        self.assertEqual(engine.ledger.entries()[-1]['state'],'unknown')
        self.assertEqual(len(result.request_ids),2)

    def test_expression_ttl_prevents_paid_child_while_memory_can_summarize_old_facts(self):
        engine,client=self.engine([response(dict(summary='整理实际历史事实',evidence_indices=[0]))])
        for scope,age in ((self.scope,61),(Scope('a','video','video'),301)):
            evidence=Evidence('old-'+scope.kind,scope,'live','旧观察事实',self.now-age,0,4,'audio','observation',.9)
            result=engine.dispatcher.dispatch(RoleTask('expression','reply',(0,)),observation=Observation((evidence,)),
                scope=scope,session='live',epoch=3,parent_id='expired-'+scope.kind)
            self.assertEqual(result.status,'expired')
        self.assertEqual(client.requests,[])
        memory=engine.dispatcher.dispatch(RoleTask('memory','remember',(0,)),observation=Observation((evidence,)),
            scope=scope,session='live',epoch=3,parent_id='old-memory')
        self.assertEqual(memory.status,'completed');self.assertEqual(len(client.requests),1)


if __name__=='__main__':unittest.main()
