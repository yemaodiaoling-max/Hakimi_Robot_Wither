import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from hakimi.models import google_search, web_access, web_quality, web_sources
from hakimi.models.search_budget import SearchBudget


class DshSearchFallbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.payload = {'session_key':'s', 'trigger_id':'t', 'reply_target_message_id':'1',
                        'recent_context_messages':[{'message_id':'1','user_id':'a','text':'帮我搜一下 lunar orbit'}]}
        self.source = {'title':'Lunar orbit dynamics', 'url':'https://example.org/orbit', 'snippet':'A measured lunar orbit.'}
        self.transport = Mock(side_effect=self.respond)
        self.configure()

    def configure(self, enabled=True, **settings):
        (self.base/'aux_config.json').write_text(json.dumps({'search_routing':{
            'dsh_on_budget':enabled, 'paid_search_mode':'deepseek'}}))
        (self.base/'search_routing.json').write_text(json.dumps({
            'daily_limit_usd':'0', 'monthly_limit_usd':'0', **settings}))

    def respond(self, kind, args, timeout):
        if kind=='dsh_search':
            self.assertEqual(args, {'query':'lunar orbit'})
            return {'sources':[dict(self.source,url='https://example.org:bad/invalid'), self.source]}
        if kind=='search':
            return {'billable':True, 'data':{'results':[dict(self.source,content='Lunar orbit data. '*10)]}}
        return {'body':'<rss/>'}

    def search(self):
        return web_access.run('web.search',{'query':'lunar orbit'},8,self.base,self.payload,transport=self.transport)

    def test_denied_reservation_uses_only_dsh_and_leaves_ledger_empty(self):
        result=self.search()
        self.assertEqual(result['results'][0]['provider'],'dsh_web_search')
        self.assertEqual([c.args[0] for c in self.transport.call_args_list],['dsh_search'])
        with SearchBudget(self.base,{}).db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],0)

    def test_network_denied_and_turn_limit_never_call_dsh(self):
        self.payload['recent_context_messages'][0]['text']='不准搜，凭记忆回答'
        self.assertEqual(self.search()['error'],'WEB_DISABLED_FOR_THIS_TURN')
        self.payload['recent_context_messages'][0]['text']='帮我搜一下'
        self.configure(max_web_calls=0)
        self.assertEqual(self.search()['error'],'WEB_TURN_LIMIT')
        self.transport.assert_not_called()

    def test_default_and_disabled_use_public_fallback(self):
        self.configure(False)
        self.assertNotIn('dsh_status',self.search())
        (self.base/'aux_config.json').unlink()
        self.assertNotIn('dsh_status',self.search())
        self.assertEqual({c.args[0] for c in self.transport.call_args_list},{'public'})

    def test_google_budget_and_supplier_limit_use_dsh_but_gateway_busy_uses_existing_route(self):
        self.configure(google_native=True, daily_limit_usd='1', monthly_limit_usd='2')
        for status,expected in [('budget_unavailable','dsh_search'),('google_http_429','dsh_search'),
                                ('gateway_busy','search'),('gateway_http_429','search')]:
            with self.subTest(status=status), patch.object(google_search,'request',return_value={'status':status}):
                self.transport.reset_mock()
                self.assertTrue(self.search()['results'])
                self.assertEqual([c.args[0] for c in self.transport.call_args_list],[expected])

    def test_rate_limited_paid_provider_uses_dsh_once(self):
        self.configure(daily_limit_usd='1',monthly_limit_usd='2')
        self.transport.side_effect=lambda k,a,t: {'billable':False,'http_status':429} if k=='search' else self.respond(k,a,t)
        self.assertTrue(self.search()['results'])
        self.assertEqual([c.args[0] for c in self.transport.call_args_list],['search','dsh_search'])

    def test_dsh_failure_continues_to_public_feed(self):
        self.transport.side_effect=lambda k,a,t: ({'error':'web_deadline_exhausted'} if k=='dsh_search' else
            {'body':'<rss><channel><item><title>Lunar orbit data</title><link>https://example.org/orbit</link></item></channel></rss>'})
        result=self.search()
        self.assertEqual(result['dsh_status'],'web_deadline_exhausted')
        self.assertEqual(result['results'][0]['provider'],'bing_rss')
        self.assertEqual([c.args[0] for c in self.transport.call_args_list],['dsh_search','public'])

    def test_compact_cards_are_deduped_and_long_url_is_readable_only_by_its_turn(self):
        long_url='https://example.org/orbit?key='+'x'*400
        body='preface '*120+'lunar orbit, measured in days. '*14
        rows=[web_quality.source(dict(self.source,url=url,text=body),read=True) for url in
              [long_url,long_url,'https://example.org/2','https://example.org/3','https://example.org/4']]
        cards=web_sources.search_cards(self.base,self.payload,rows,'lunar orbit')
        self.assertEqual(len(cards),3)
        self.assertLessEqual(sum(len(card['text']) for card in cards),600)
        self.assertLess(len(json.dumps(cards)),1700)
        self.assertNotIn(long_url,json.dumps(cards))
        self.assertIn('lunar orbit',cards[0]['text'])
        self.assertEqual(web_sources.cached(self.base,self.payload,long_url)['text'],body)
        detail=web_access.run('web.read',{'url':'W1'},8,self.base,self.payload,transport=self.transport)
        self.assertEqual(detail['url'],long_url)
        self.assertEqual(detail['text'],body)
        for other in [{'trigger_id':'other'},{'session_key':'other'}]:
            self.assertEqual(web_access.run('web.read',{'url':'W1'},8,self.base,dict(self.payload,**other),
                                           transport=self.transport)['error'],'WEB_UNKNOWN_SOURCE')
        self.transport.assert_not_called()

    def test_history_context_prioritizes_cited_links_and_marks_title_only(self):
        sources=[dict(web_quality.source(dict(self.source,snippet='')),source_id='W'+str(i+1),
                 attribution='explicitly_cited' if i==4 else 'turn_reference_not_fact_verification')
                 for i in range(5)]
        cards=web_sources.trace_cards({'sources':sources},'lunar orbit')['sources']
        self.assertEqual(len(cards),3)
        self.assertEqual(cards[0]['source_id'],'W5')
        self.assertEqual(cards[0]['evidence_level'],'title_only')
        self.assertEqual(len(sources),5)

    def test_google_429_reads_only_recognized_status(self):
        for raw,expected in [(b'{"status":"budget_unavailable"}','budget_unavailable'),
                             (b'{"status":"gateway_busy"}','gateway_busy'),(b'bad','gateway_http_429')]:
            error=HTTPError('https://example.org',429,'throttled',{},io.BytesIO(raw))
            with patch('hakimi.models.vertex_route.load') as route, patch.object(google_search.ssl,'create_default_context'), \
                 patch.object(google_search,'build_opener') as opener:
                route.return_value.error=None
                route.return_value.credentials.return_value=('fake','ca')
                opener.return_value.open.side_effect=error
                self.assertEqual(google_search.request('lunar orbit',True,5,self.base)['status'],expected)
