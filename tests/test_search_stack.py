from decimal import Decimal
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
from hakimi.models import web_access,web_sources,web_quality,search_routing
from hakimi.models.search_budget import SearchBudget,BudgetUnavailable,deepseek_cost,cost_bounds
from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.bridge.reply_format import text_bubbles
from hakimi.bridge.interaction_policy import delivered

class SearchStackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        self.payload={'session_key':'group:123','trigger_id':'s1','reply_target_message_id':'1','recent_context_messages':[{'message_id':'1','user_id':'u','text':'今天Google量子纠错有什么新进展'}]}
        self.source={'url':'https://blog.google/quantum/article','title':'Quantum error correction experiment','text':'Quantum error correction evidence. '*40,'date':'2026-10-14T16:00:00Z'}
    def settings(self,**values):
        (self.base/'search_routing.json').write_text(json.dumps({'enabled':True,**values}),encoding='utf-8')
    def test_budget_pending_caps_and_actual_overrun_no_silence(self):
        budget=SearchBudget(self.base,{'daily_limit_usd':'.002','monthly_limit_usd':'.003'})
        one=budget.reserve('search','.00075');two=budget.reserve('search','.00075')
        with self.assertRaises(BudgetUnavailable):budget.reserve('third','.00075')
        budget.settle(one,None)
        with self.assertRaises(BudgetUnavailable):budget.reserve('third','.00075')
        budget.settle(two,0);budget.reserve('third','.00075')
        budget.settle(one,'.00080')
        with budget.db() as db:self.assertEqual(db.execute('select review from calls where id=?',(one,)).fetchone()[0],0)
        # Cost estimation mistakes only affect accounting, not a global switch.
        budget.settle(two,'0.5')
        self.assertFalse((self.base/'state/pause').exists())
    def test_cache_usage_settles_actual_and_missing_usage_retains_bounds(self):
        usage={'prompt_tokens':1000,'completion_tokens':20,'prompt_cache_hit_tokens':900}
        self.assertEqual(deepseek_cost(usage),Decimal('.0000297'))
        self.assertIsNone(deepseek_cost({'prompt_tokens':1000,'completion_tokens':20}))
        low,high=cost_bounds({'prompt_tokens':1000,'completion_tokens':20},None)
        self.assertLess(low,high)
    def test_offline_every_public_boundary_does_not_request(self):
        self.payload['recent_context_messages'][0]['text']='不准搜，凭记忆回答今天的新消息'
        transport=Mock(side_effect=AssertionError('network'))
        for name,args in [('web.search',{'query':'news'}),('web.read',{'url':'https://example.org/article'})]:
            self.assertEqual(web_access.run(name,args,1,self.base,self.payload,transport=transport)['error'],'WEB_DISABLED_FOR_THIS_TURN')
        self.assertFalse(search_routing.prepare(self.payload,self.base,judge=Mock())['network_allowed'])
        transport.assert_not_called()
    def test_low_budget_keeps_paid_search_room_for_explicit_request(self):
        self.settings(daily_limit_usd='.0015',monthly_limit_usd='.0015')
        transport=Mock(return_value={'billable':True,'data':{'results':[self.source]}})
        auto=web_access.run('web.search',{'query':'quantum error correction'},1,self.base,self.payload,transport=transport)
        self.assertNotIn('search',[call.args[0] for call in transport.call_args_list])
        transport.reset_mock();self.payload['trigger_id']='explicit';self.payload['recent_context_messages'][0]['text']='帮我搜一下量子纠错新进展'
        web_access.run('web.search',{'query':'quantum error correction'},1,self.base,self.payload,transport=transport)
        self.assertIn('search',[call.args[0] for call in transport.call_args_list])

    def test_paid_api_login_and_irrelevant_results_not_evidence(self):
        def transport(kind,args,timeout):
            if kind=='search':return {'billable':True,'data':{'results':[{'url':'https://accounts.google.com/login','title':'Sign in','content':'sign in to continue'},{'url':'https://blog.google/argon','title':'Gemini Argon announcement','content':'Gemini new product. '*30}]}}
            return {'body':'<rss><channel></channel></rss>','billable':False}
        result=web_access.run('web.search',{'query':'Google quantum error correction latest November 2026'},2,self.base,self.payload,transport=transport)
        self.assertEqual(result['status'],'no_usable_sources')
        self.assertEqual(result['results'],[])
    def test_dates_are_not_promoted_to_requested_month(self):
        item=web_quality.source(self.source,'Google quantum error correction latest November 2026',read=True)
        self.assertEqual(item['requested_period_status'],'outside_requested_month')
        self.assertEqual(item['published_at'],'2026-10-14T16:00:00+00:00')
        self.assertIsNone(web_quality.source({'url':'https://example.org/a','title':'Security check','content':'Please verify you are human'},read=True))
    def test_turn_deadline_independent_from_presearch_timeout_and_once_only(self):
        self.settings();judge=Mock(return_value={'search':False})
        first=search_routing.prepare(self.payload,self.base,timeout=1,judge=judge)
        second=search_routing.prepare(self.payload,self.base,timeout=1,judge=judge)
        self.assertEqual(first,second);self.assertEqual(judge.call_count,1)
        with web_sources.database(self.base) as db:
            deadline=db.execute('select deadline from turns').fetchone()[0]
        self.assertGreater(deadline-time.time(),35)
    def test_real_escaped_submit_ack_quote_trace_and_immutable_reference(self):
        source=web_sources.record(self.base,self.payload,web_quality.source(self.source,read=True))
        # A draft is not a delivered source.
        self.assertEqual(web_sources.lookup(self.base,self.payload)['sources'],[])
        coordinator=TurnCoordinator(self.base)
        coordinator.publish(self.payload);claim=coordinator.claim_primary()
        bubbles=text_bubbles('量子纠错文章发布日期是10月14日[W1]|它不是11月新消息')
        self.assertIn('&#91;W1&#93;',bubbles[0])
        result=coordinator.submit_reply(claim['token'],bubbles,'gemini');self.assertTrue(result['accepted'])
        record=json.loads((self.base/'state/outbox.jsonl').read_text(encoding='utf-8').splitlines()[0])
        # No ACK yet means no source claim about a sent message.
        self.assertEqual(web_sources.lookup(self.base,self.payload)['sources'],[])
        delivered('group:123',record['reply_id']+':0',None,True,self.base,text=bubbles[0],message_id='out1')
        question={**self.payload,'trigger_id':'s2','reply_target_message_id':'2','recent_context_messages':[{'message_id':'2','user_id':'u','text':'别联网，来源呢','reply_to':'out1'}]}
        transport=Mock(side_effect=AssertionError('offline'))
        trace=web_access.run('web.sources',{},1,self.base,question,transport=transport)
        self.assertEqual(trace['sources'][0]['source_id'],'W1')
        self.assertEqual(trace['sources'][0]['attribution'],'explicitly_cited')
        web_sources.record(self.base,self.payload,{**source,'text':'new draft changes'})
        self.assertNotEqual(web_sources.lookup(self.base,question)['sources'][0]['text'],'new draft changes')
        other={**question,'session_key':'group:999'}
        self.assertEqual(web_sources.lookup(self.base,other)['sources'],[])
        transport.assert_not_called()
    def test_unmarked_reply_has_reference_without_fact_verification_claim(self):
        web_sources.record(self.base,self.payload,web_quality.source(self.source,read=True))
        coordinator=TurnCoordinator(self.base);coordinator.publish(self.payload);claim=coordinator.claim_primary()
        coordinator.submit_reply(claim['token'],text_bubbles('这篇文章是10月14日发布的'),'gemini')
        record=json.loads((self.base/'state/outbox.jsonl').read_text(encoding='utf-8').splitlines()[0])
        delivered('group:123',record['reply_id']+':0',None,True,self.base,message_id='out2')
        trace=web_sources.lookup(self.base,self.payload)
        self.assertEqual(trace['sources'][0]['attribution'],'turn_reference_not_fact_verification')
    def test_paid_fetch_reuses_public_url_guard_before_http(self):
        from hakimi.models.web_fetch_worker import native
        with patch('hakimi.models.dsh_text_judge._installation',return_value=Path('.')), patch('hakimi.models.dsh_text_judge._provider',return_value={'base_url':'https://api.deepseek.com','api_key':'synthetic'}), patch('hakimi.models.vertex_web.public_url',side_effect=ValueError('private')), patch('hakimi.models.web_fetch_worker.build_opener') as opener:
            result=native('fetch',{'url':'https://127.0.0.1/private'},1)
        self.assertFalse(result['billable']);opener.assert_not_called()
    def test_hanging_network_child_is_killed_at_total_deadline(self):
        from hakimi.models import web_transport
        import subprocess,sys
        original=subprocess.Popen
        def slow(*args,**kwargs):
            return original([sys.executable,'-c','import time;time.sleep(10)'],**kwargs)
        started=time.monotonic()
        with patch('hakimi.models.web_transport.subprocess.Popen',side_effect=slow):
            result=web_transport.request('public',{'url':'https://example.org'},.2)
        self.assertEqual(result['error'],'web_deadline_exhausted')
        self.assertLess(time.monotonic()-started,2)

    def test_fallback_prompt_drops_new_page_bodies_before_hard_limits(self):
        from hakimi.models.fallback_prompt import fallback_prompt,append_fallback_context
        from hakimi.models.fallback_runner import MAX_PROMPT_BYTES,MAX_PROMPT_CHARS
        self.payload['_web_context']={'search':{'results':[dict(self.source,source_id='W1',text='NEW_WEB_BODY'*9000)]}}
        self.payload['recent_context_messages'][0]['text']='ORIGINAL_QUESTION'
        prompt=fallback_prompt(self.payload,self.base)
        self.assertIn('ORIGINAL_QUESTION',prompt);self.assertIn('available_public_sources',prompt)
        self.assertNotIn('NEW_WEB_BODY',prompt)
        self.assertLessEqual(len(prompt),MAX_PROMPT_CHARS);self.assertLessEqual(len(prompt.encode('utf-8')),MAX_PROMPT_BYTES)
        combined=append_fallback_context(prompt,'TOOL_RECEIPT_UNCHANGED'*100)
        self.assertIn('TOOL_RECEIPT_UNCHANGED'*100,combined)

    def test_read_pagination_and_cached_read_without_network(self):
        source=web_sources.record(self.base,self.payload,web_quality.source(self.source,read=True))
        self.payload['recent_context_messages'][0]['text']='离线回答'
        transport=Mock(side_effect=AssertionError('network'))
        result=web_access.run('web.read',{'url':source['url'],'offset':100,'limit':200},1,self.base,self.payload,transport=transport)
        self.assertEqual(result['text'],source['text'][100:300]);self.assertTrue(result['more'])
        transport.assert_not_called()

if __name__=='__main__':unittest.main()
