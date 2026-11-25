import importlib.util
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from hakimi.models.search_budget import SearchBudget

module=Path(__file__).resolve().parents[1]/'private/deploy/qq_google_search.py'
spec=importlib.util.spec_from_file_location('qq_google_search',module)
google=importlib.util.module_from_spec(spec)
spec.loader.exec_module(google)


class SearchGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.runtime=patch.object(google,'RUNTIME',self.base)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.g=MagicMock()
        self.g.HYDROGEN_MODEL='gemini-4-hydrogen'
        self.g.REPORT=self.base/'report.json'
        self.g.REPORT.write_text('{}')
        self.g.route.return_value=('https://example.test/inference','generateContent')
        self.g._upstream_headers.return_value={}
        self.g.reserve_cost_nano.return_value=1000000
        self.g._provider_terminal.return_value=True
        self.g.BudgetBlocked=type('BudgetBlocked',(RuntimeError,),{})
        self.ledger=self.g.CreditLedger.return_value
        self.ledger.settle_success.return_value={'actual_nano':600000}
        self.settings={'daily_limit_usd':'.20','monthly_limit_usd':'2'}

    def call(self,value):
        @contextmanager
        def upstream(*args,**kwargs):
            from io import BytesIO
            yield BytesIO(json.dumps(value).encode())
        with patch.object(google,'urlopen',side_effect=upstream) as http:
            result=google._perform(self.g,'public query',True,self.settings)
        return result,http

    def test_both_ledgers_keep_possible_query_fee(self):
        result,_=self.call({'usageMetadata':{},'candidates':[{'groundingMetadata':{
          'webSearchQueries':['one','two'],'groundingChunks':[{'web':{'uri':'https://test.example/topic','title':'public article'}}]}}]})
        self.assertEqual(result[0],200)
        self.assertEqual(self.ledger.settle_success.call_args.kwargs['pending_search_nano'],28000000)
        with SearchBudget(self.base,self.settings).db() as db:
            self.assertEqual(db.execute('select actual,lower,upper from calls').fetchone(),(None,600000,28600000))

    def test_returned_query_still_counts_without_a_source_url(self):
        result,_=self.call({'usageMetadata':{},'candidates':[{'finishReason':'MAX_TOKENS',
            'groundingMetadata':{'webSearchQueries':['one','two']}}]})
        self.assertEqual(result[1]['status'],'no_google_sources')
        self.assertEqual(result[1]['finish_reason'],'MAX_TOKENS')
        self.assertEqual(result[1]['chunk_count'],0)
        self.assertEqual(self.ledger.settle_success.call_args.kwargs['pending_search_nano'],28000000)
        with SearchBudget(self.base,self.settings).db() as db:
            self.assertEqual(db.execute('select lower,upper from calls').fetchone(),(600000,28600000))

    def test_active_spend_cap_is_distinct_from_admin_pause(self):
        from io import BytesIO
        import time
        self.g.spend_cap_snapshot.return_value=time.monotonic()+60
        for enabled,status in [(True,'budget_unavailable'),(False,'grounding_paused')]:
            server=MagicMock()
            body=b'{"query":"lunar orbit","explicit":true}'
            server.headers={'Content-Length':str(len(body))}
            server.rfile=BytesIO(body)
            with patch.object(google,'config',return_value={'google_native':enabled}):
                google.handle(server,self.g)
            server.reply.assert_called_once_with(429 if enabled else 503,{'status':status})
            self.g.SLOTS.acquire.assert_not_called()

    def test_low_budget_blocks_paid_request(self):
        self.settings['daily_limit_usd']='.01'
        result,http=self.call({})
        self.assertEqual(result[0],429)
        self.ledger.reserve.assert_not_called()
        http.assert_not_called()

    def test_unconfirmed_result_remains_reserved(self):
        self.call({'candidates':[{'groundingMetadata':{'groundingChunks':[{'web':{'uri':'https://test.example/ok'}}]}}]})
        with SearchBudget(self.base,self.settings).db() as db:
            self.assertEqual(db.execute('select actual,status from calls').fetchone(),(None,'unknown'))
