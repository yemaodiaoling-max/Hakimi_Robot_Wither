import asyncio,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from hakimi.models.fallback_runner import parse_response,FallbackError
from hakimi.models import fallback_supervisor as s

class Protocol(unittest.TestCase):
    def test_empty_placeholders(self):
        r=parse_response(json.dumps(dict(action='tool',tool='time.now',input={},math={},files=[],sticker='',memory=None)))
        self.assertEqual(r,dict(action='tool',tool='time.now',input={},text=''))
    def test_malformed_types_and_extra_actions(self):
        for changes in ({'input':None},{'input':'{}'},{'tool':{}},{'text':'x'*2001},{'math':{'markdown':'x'}},{'unexpected':1}):
            with self.subTest(changes=changes),self.assertRaises(FallbackError) as caught:
                parse_response(json.dumps(dict(dict(action='tool',tool='time.now',input={},text=''),**changes)))
            self.assertTrue(caught.exception.protocol_hint)
    def test_no_values_in_feedback(self):
        with self.assertRaises(FallbackError) as caught:
            parse_response(json.dumps(dict(action='tool',tool='time.now',input='private-secret',text='')))
        self.assertNotIn('private-secret',caught.exception.protocol_hint)
    def test_valid_reply_unchanged(self):
        self.assertEqual(parse_response('{"action":"reply","text":"hello"}'),{'action':'reply','text':'hello'})

class Loop(unittest.IsolatedAsyncioTestCase):
    async def exercise(self,budget=20,healthy=True,remaining=90,always_bad=False,invalid=None):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);(base/'state').mkdir()
            claim={'token':'t','trigger_id':'id','payload':{},'deadline':time.time()+remaining}
            calls=[];done=asyncio.Event();loop=asyncio.get_running_loop()
            class C:
                lane_id=''
                def lane_ids(self):return ['']
                def for_lane(self,l):return self
                def claim_fallback(self):return claim if not calls else None
                def enable_post_reply(self,t):pass
                def check_token(self,*a):return healthy
                def complete(self,*a,**kw):done.set();return True
            def runner(prompt,base):
                calls.append(prompt)
                if len(calls)==1 or always_bad:
                    return parse_response(invalid or '{"action":"tool","tool":"time.now","input":null,"text":""}')
                return {'action':'silent','text':''}
            def submit(*args,**kwargs):loop.call_soon_threadsafe(done.set);return {'accepted':True}
            with patch.object(s,'coordinator_for',return_value=C()),patch.object(s,'availability',return_value={'fast':False}),patch.object(s.bilibili_video,'prepare',return_value=None),patch.object(s,'prepare_images',return_value=()),patch.object(s,'worker_healthy',return_value=False),patch.object(s,'fallback_prompt',return_value='prompt'),patch.object(s,'submit_fallback_result',side_effect=submit),patch.object(s,'report_fault'):
                task=asyncio.create_task(s.fallback_loop(lambda:{'fallback':{'enabled':True,'max_calls_per_hour':budget}},base,runner))
                await asyncio.wait_for(done.wait(),5)
                task.cancel()
                try:await task
                except asyncio.CancelledError:pass
            return calls,json.loads((base/'state/fallback_status.json').read_text())
    async def test_repairs_once(self):
        calls,state=await self.exercise()
        self.assertEqual(len(calls),2);self.assertIn('input must be',calls[1]);self.assertEqual(len(state['calls']),2)

    async def test_invalid_reply_reports_field_and_retries(self):
        for raw,field in [('{"action":"reply"}','text'),('{"action":"reply","text":123}','text'),
                          ('{"action":"bad","text":""}','action'),('{"action":"reply","text":"x","foo":1}','foo'),
                          ('{"action":"silent","text":"","reply_to":"1"}','reply_to')]:
            with self.subTest(raw=raw):
                calls,state=await self.exercise(invalid=raw)
                self.assertEqual(len(calls),2)
                self.assertIn(field,state['last_protocol_hint'])
                self.assertIn(state['last_protocol_hint'],calls[1])
    async def test_no_budget_no_ownership_no_time(self):
        for kw in ({'budget':1},{'healthy':False},{'remaining':10}):
            calls,state=await self.exercise(**kw)
            self.assertEqual(len(calls),1)
    async def test_second_failure_stops(self):
        calls,state=await self.exercise(always_bad=True)
        self.assertEqual(len(calls),2);self.assertEqual(state['state'],'failed')

if __name__=='__main__':unittest.main()
