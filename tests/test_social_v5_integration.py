"""Native tool, scheduler and ingestion integration; only temporary fixtures."""
import contextlib,io,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from hakimi.bridge import bridge_daemon as bridge
from hakimi.social.memory_social import record_social
from hakimi.memory.memory_store import store
from tests import test_memory_v4 as v4
from tests.test_conversation_scheduler import config
from hakimi.bridge.conversation_scheduler import ConversationScheduler
from hakimi.social import social_behavior
from hakimi.bridge.turn_coordinator import TurnCoordinator

class CurveToolTests(unittest.TestCase):
 setUp=v4.ToolIntegrationTests.setUp
 call=v4.ToolIntegrationTests.call
 def test_native_proposal_clamp_exact_note_and_retry(self):
  note='  我对他多了解了一些。\n保留原文 ✨  '
  args=dict(note=note,target_id='22',affinity=100,confidence=85,evidence_memory_ids=self.evidence_id)
  a=self.call('memory.social',**args);b=self.call('memory.social',**args)
  self.assertEqual(a['affinity'],65);self.assertEqual(a['affinity_policy']['proposed'],100)
  self.assertEqual(a['assessment_id'],b['assessment_id']);self.assertEqual(a['affinity_policy'],b['affinity_policy'])
  with self.store.db() as db:self.assertEqual(db.execute('SELECT text FROM memory_originals WHERE record_id=?',(a['record_id'],)).fetchone()[0],note)
 def test_no_farming_old_evidence_and_fresh_event_can_change(self):
  a=self.call('memory.social',note='我更了解他了。',target_id='22',affinity=65,confidence=85,evidence_memory_ids=self.evidence_id)
  b=self.call('memory.social',note='换个措辞，但没有新事实。',target_id='22',affinity=100,confidence=85,evidence_memory_ids=self.evidence_id)
  self.assertEqual(b['affinity'],a['affinity'])
  t=time.time()+5
  self.store.observe({'user_id':'22','session_key':'group:100','message_id':'fresh-curve','received_at':t,'text':'完成了约定的研究任务。','direct_targets':['8415435122']})
  with self.store.db() as db:mid='M'+str(db.execute("SELECT id FROM records WHERE origin='fresh-curve'").fetchone()[0])
  c=record_social(self.base,'group:100','这次有新的履约事实。','22',affinity=100,confidence=85,evidence_memory_ids=[mid],origin_message_id='fresh-turn',now=t+1,impact='ordinary')
  self.assertGreater(c['affinity'],b['affinity']);self.assertLessEqual(c['affinity']-b['affinity'],3)
 def test_unscored_stays_unknown(self):
  a=self.call('memory.social',note='还想再观察一下。',target_id='22',evidence_memory_ids=self.evidence_id)
  self.assertIsNone(a['affinity']);self.assertIsNone(a['affinity_policy']['applied'])

class BehaviorWiringTests(unittest.TestCase):
 def test_confirmed_owner_exempt_with_explicit_config(self):
  from hakimi import paths as _paths
  from _repo import INSTANCE
  with tempfile.TemporaryDirectory() as d,patch.object(_paths,'DEFAULTS_DIR',INSTANCE):
   p=social_behavior.behavior_for(d,'8112866014149232024','group:100',config={})
   self.assertTrue(p['protected']);self.assertFalse(p['at_gate'])
 def test_adaptive_opportunity_changes(self):
  with tempfile.TemporaryDirectory() as d:
   s=ConversationScheduler();s.path=Path(d)/'state/conversation_scheduler.json';s.observe('group:100','这个数学问题怎么推导？','22',1,1000)
   with patch.object(social_behavior,'behavior_for',return_value={'initiative_multiplier':0.2}):cold=s.decide('group:100',config(),1100)
   with patch.object(social_behavior,'behavior_for',return_value={'initiative_multiplier':1.15}):warm=s.decide('group:100',config(),1100)
   self.assertLess(cold.score,warm.score);self.assertFalse(cold.ready)
 def test_at_gate_adaptive_and_legacy_preserve_context(self):
  for enabled in (True,False):
   cfg=config();cfg['conversation_scheduler']['enabled']=enabled;e=bridge.DynamicHakimiEngine()
   event={'message_type':'group','group_id':100,'user_id':22,'self_id':99,'message_id':'gate-fixture','raw_message':'[CQ:at,qq=99] 请看这个问题','sender':{'nickname':'fixture'}}
   with patch.object(social_behavior,'should_reject_at',return_value=True),patch.object(social_behavior,'behavior_for',return_value={'initiative_multiplier':0.2}):
    instant,_,_=e.record_and_score(event,cfg,clean_text='@哈基米 请看这个问题')
   self.assertFalse(instant);self.assertEqual(len(e.context_windows['group:100']),1)
   if enabled:self.assertFalse(e.conversation_scheduler.group('group:100').pending[-1][5])

class ReviewCompatibilityTests(unittest.TestCase):
 def test_completed_legacy_marker_does_not_block_review(self):
  from hakimi.bridge.turn_coordinator import TurnCoordinator
  from hakimi.memory.personal_review import _backlog
  with tempfile.TemporaryDirectory() as d:
   base=Path(d);c=TurnCoordinator(base)
   result=c.publish({'session_key':'group:100','recent_context_messages':[]})
   claim=c.claim_primary();self.assertTrue(c.complete(claim['token']))
   marker=base/'state/pending_trigger.json'
   marker.write_text(json.dumps({'trigger_id':result['trigger_id']}),encoding='utf-8')
   self.assertEqual(_backlog(base),'')
   marker.write_text(json.dumps({'trigger_id':'unknown-unhandled'}),encoding='utf-8')
   self.assertEqual(_backlog(base),'pending_real_trigger')

class ReviewDeadlineIntegrationTests(unittest.TestCase):
 """Real TurnCoordinator claims with an injected clock; no sleeps or private data."""
 call=v4.ToolIntegrationTests.call

 def setUp(self):
  v4.ToolIntegrationTests.setUp(self)
  self.assertTrue(self.coordinator.complete(self.claim['token']))

 def coordinator_at(self,start):
  self.clock=[start]
  return TurnCoordinator(self.base,fallback_timeout_seconds=120,ttl_seconds=600,
                         primary_timeout_seconds=180,primary_max_seconds=420,
                         grace_seconds=90,confirmation_seconds=30,
                         clock=lambda:self.clock[0])

 def review_payload(self,review_id):
  return {'session_key':'group:100','wake_mode':'个人记忆反思','recent_context_messages':[],
          'internal_review':{'review_id':review_id,'group':'group:100','candidates':[
           {'person_id':'22','user_id':'22','evidence':[{'memory_id':self.evidence_id,'excerpt':'公开交流'}],
            'evidence_memory_ids':[self.evidence_id]}]}}

 def publish_review(self,coordinator,start,review_id='deadline-review'):
  result=coordinator.publish(self.review_payload(review_id))
  self.assertTrue(result['accepted'])
  claim=coordinator.claim_primary()
  self.assertIsNotNone(claim)
  self.claim=claim
  from hakimi.prompt.prompt_context import activate_context
  activate_context(claim['payload'],self.base,claim=claim,now=start)
  return claim

 def test_internal_review_survives_eight_seconds_writes_social_and_releases(self):
  start=time.time();coordinator=self.coordinator_at(start);claim=self.publish_review(coordinator,start)
  self.assertEqual(claim['deadline'],start+80)
  self.clock[0]=start+8
  self.assertTrue(coordinator.check_token(claim['token']))
  with patch.object(self.qq_cli,'TurnCoordinator',lambda base:TurnCoordinator(base,clock=lambda:self.clock[0])):
   result=self.call('memory.social',note='八秒后写入的内部反思印象',target_id='22',evidence_memory_ids=self.evidence_id)
  self.assertTrue(result['saved'])
  with self.store.db() as db:
   self.assertEqual(db.execute('SELECT count(*) FROM social_assessments').fetchone()[0],1)
  self.assertTrue(coordinator.complete(claim['token']))
  self.assertFalse(coordinator.check_token(claim['token']))
  self.assertTrue(coordinator.publish(self.review_payload('deadline-review-next'))['accepted'])

 def test_internal_review_token_expires_at_eighty_seconds(self):
  start=time.time();coordinator=self.coordinator_at(start);claim=self.publish_review(coordinator,start)
  self.assertEqual(claim['deadline'],start+80)
  self.clock[0]=start+80
  self.assertFalse(coordinator.check_token(claim['token']))

 def test_ordinary_turn_keeps_existing_deadline(self):
  start=time.time();coordinator=self.coordinator_at(start)
  result=coordinator.publish({'session_key':'group:100','trigger_id':'ordinary-deadline',
                              'created_at':start,'recent_context_messages':[]})
  self.assertTrue(result['accepted'])
  claim=coordinator.claim_primary()
  self.assertEqual(claim['deadline'],start+420)
  self.clock[0]=start+80
  self.assertTrue(coordinator.check_token(claim['token']))

class TerraSamplingIntegrationTests(unittest.TestCase):
 def test_large_batch_fills_cap_and_keeps_every_selected_style_sample(self):
  from hakimi.ops import terra_supervision as t
  rows=[{'at':i,'message_id':str(i),'session_key':'group:'+str(i%3)} for i in range(100)]
  chosen=t._stratified(rows,12)
  self.assertEqual(len(chosen),12);self.assertEqual(len({r['message_id'] for r in chosen}),12)
  self.assertEqual(len({r['session_key'] for r in chosen}),3)
  dsh=[{'id':str(i),'session_key':'group:100','at':i,'text':'讨论细节'*175} for i in range(12)]
  gemini=[{'id':str(i),'session_key':'group:100','at':i,'text':'自然接话'*175} for i in range(8)]
  relations=[{'subject_id':str(100+i),'target_id':'200','evidence':[{'memory_id':'M'+str(j),'excerpt':'公开交流'*45} for j in range(6)]} for i in range(3)]
  prompt=t._prompt(dsh,gemini,relations);payload=json.loads(prompt[prompt.index('{'):])
  self.assertLessEqual(len(prompt),20000)
  self.assertEqual(len(payload['dsh_samples']),12);self.assertEqual(len(payload['gemini_comparison']),8)
 def test_only_first_cycle_catches_up_four_hours(self):
  from hakimi.ops import terra_supervision as t
  from hakimi.ops.terra_budget import budget_for
  with tempfile.TemporaryDirectory() as d:
   base=Path(d);(base/'state').mkdir();now=time.time()
   rows=[{'at':now-3*3600,'provider':'dsh','message_id':'old-dsh','session_key':'group:100','text':'实际已送达的风格样本'},
         {'at':now-60,'provider':'gemini','message_id':'new-gemini','session_key':'group:100','text':'同群对照样本'}]
   (base/'state/sent_messages.jsonl').write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf-8')
   result={'style':{'verdict':'normal','guidance':'自然接住当前话题，减少模板起手。','uncertainty':'仅有限样本'},'relations':[]}
   with patch.object(t,'relation_candidates',return_value=[]):
    first=t.run_cycle(base,now,runner=lambda *_:(result,{'input_tokens':100,'output_tokens':20}),budget=budget_for(base))
    later=t.run_cycle(base,now+7201,runner=lambda *_:(_ for _ in ()).throw(AssertionError('Old sample must not be reviewed again')),budget=budget_for(base))
   self.assertTrue(first['feedback_written']);self.assertEqual(first['dsh_samples'],1)
   self.assertFalse(later['budget_model'])

class IngestionTests(unittest.IsolatedAsyncioTestCase):
 async def test_rejected_at_still_in_history_and_database(self):
  with tempfile.TemporaryDirectory() as d:
   base=Path(d);(base/'state').mkdir();cfg=config();e=bridge.DynamicHakimiEngine()
   event={'post_type':'message','message_type':'group','group_id':100,'user_id':22,'self_id':99,'message_id':'persist-gate','raw_message':'[CQ:at,qq=99] 请看这个问题','sender':{'nickname':'fixture'}}
   with contextlib.ExitStack() as stack:
    for name,value in {'BASE_DIR':base,'INBOX_LOG':base/'state/chat_history.jsonl','ONLINE_STATUS_FILE':base/'state/online.json','engine':e}.items():stack.enter_context(patch.object(bridge,name,value))
    stack.enter_context(patch.object(bridge,'load_config',return_value=cfg));stack.enter_context(patch.object(bridge,'session_is_allowed',return_value=True))
    stack.enter_context(patch.object(social_behavior,'should_reject_at',return_value=True));stack.enter_context(patch.object(social_behavior,'behavior_for',return_value={'initiative_multiplier':0.2}))
    stack.enter_context(patch.object(bridge,'enqueue_wake_beacon',side_effect=AssertionError('Rejected @ cannot enqueue instant wake')))
    with contextlib.redirect_stdout(io.StringIO()):await bridge.process_incoming_event(event)
   rows=[json.loads(x) for x in (base/'state/chat_history.jsonl').read_text(encoding='utf-8').splitlines()]
   self.assertEqual(len(rows),1);self.assertEqual(rows[0]['message_id'],'persist-gate')
   with store(str(base)).db() as db:self.assertEqual(db.execute("SELECT count(*) FROM records WHERE origin='persist-gate' AND kind='chat'").fetchone()[0],1)

if __name__=='__main__':unittest.main(verbosity=2)
