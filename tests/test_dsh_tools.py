import asyncio,json,re,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from test_official_adapter import fixture_root
from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.models.provider_health import ProviderHealth
from hakimi.models.fallback_runner import parse_response,FallbackError
from hakimi.models.dsh_tools import execute,validate_request,resolve_tools
from hakimi.tools.math_render import prepare_text,submit_text

class MathRoutingTests(unittest.TestCase):
    def test_math_bars_survive_and_chat_stays_outside(self):
        result=prepare_text(r'先看这里喵！|绝对值 $|x|=1$。|这下就好懂啦')
        self.assertIn('$|x|=1$',result['markdown']);self.assertIn('喵',result['before']);self.assertIn('好懂',result['after'])
        self.assertNotIn('喵',result['markdown'])
        for value in ['普通聊天','预算$35/月','`print("$x$")`']:
            self.assertIsNone(prepare_text(value))

    def test_automatic_math_keeps_unlimited_rendered_pages_and_stale_owner_does_not_render(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            base=Path(folder);c=TurnCoordinator(base);ProviderHealth(base).set_mode('dsh')
            c.publish({'session_key':'group:100','trigger_id':'math-route','recent_context_messages':[{'text':'formula'}]});claim=c.claim_fallback()
            with patch('math_render.render',return_value=['image'+str(i) for i in range(7)]) as render:
                result=submit_text(c,claim['token'],'dsh','先看喵！|$x=1$|收尾',base)
                self.assertTrue(result['accepted']);record=json.loads((base/'state/outbox.jsonl').read_text())
                self.assertTrue(record['rendered_math']);self.assertEqual(len(record['segments']),9)
                self.assertFalse(submit_text(c,claim['token'],'dsh','$x=1$',base)['accepted']);self.assertEqual(render.call_count,1)

class DshToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_format_descriptor_trailer_is_accepted_but_second_action_is_not(self):
        reply='{"action":"reply","text":"收到"}'
        self.assertEqual(parse_response(reply+'{"type":"json_object"}')['text'],'收到')
        for trailer in (reply,'{"type":"json_object","action":"tool"}','{"type":"json_object"}{}'):
            with self.assertRaises(FallbackError):parse_response(reply+trailer)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();fixture_root(self.root);self.base=self.root/'official_test_runtime'
        self.c=TurnCoordinator(self.base);ProviderHealth(self.base).set_mode('dsh')
        self.c.publish({'session_key':'group:850245632','trigger_id':'tool-test','recent_context_messages':[
            {'session_key':'group:850245632','user_id':'22','message_id':'1','text':'工具验证：称呼MEMORY_TOOL_CHECK'}]})
        self.claim=self.c.claim_fallback();self.claim['_images']=()

    def call(self,name,input):return execute({'tool':name,'input':input,'text':''},self.claim,self.c,self.base)

    async def test_real_cli_memory_write_search_read_and_provider(self):
        written=self.call('memory.remember',{'kind':'person','user_id':'22','key':'称呼','note':'称呼MEMORY_TOOL_CHECK'})
        self.assertTrue(written['ok'])
        source=[json.loads(line) for line in (self.base/'state/session_memory.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(source[-1]['provider'],'dsh')
        found=self.call('memory.search',{'query':'MEMORY_TOOL_CHECK','scope':'all_groups'})
        raw=json.dumps(found,ensure_ascii=False);self.assertIn('MEMORY_TOOL_CHECK',raw)
        mid=re.search(r'M\d+',raw).group()
        self.assertIn('MEMORY_TOOL_CHECK',json.dumps(self.call('memory.read',{'memory_id':mid}),ensure_ascii=False))
        calls=[json.loads(line) for line in (self.base/'state/tool_calls.jsonl').read_text().splitlines()]
        self.assertTrue(all(r['provider']=='dsh' for r in calls));self.assertNotIn(self.claim['token'],json.dumps(calls))

    async def test_calculator_time_and_tool_menu(self):
        self.assertEqual(self.call('calculator.evaluate',{'expression':'2+2'})['result']['data']['value'],4)
        self.assertTrue(self.call('time.now',{'timezone':'Asia/Shanghai'})['ok'])
        tools=self.call('tools.list',{})['result']['tools'];names={r['name'] for r in tools}
        self.assertIn('math.send',names);self.assertIn('memory.remember',names);self.assertNotIn('stickers.collect',names)

    async def test_requests_reject_terminal_commands_unknown_fields_and_stale_owner(self):
        for name,input in [('shell',{'command':'anything'}),('stickers.collect',{'image':'x','filename':'x.png'}),('memory.search',{'path':'arbitrary'})]:
            with self.assertRaises(ValueError):validate_request(name,input)
        self.c.complete(self.claim['token'])
        with self.assertRaises(ValueError):self.call('memory.remember',{'note':'not written'})

    async def test_terminal_math_request_is_real_reply_structure_without_extra_model_call(self):
        result=self.call('math.send',{'markdown':'$$x=1$$','before':'先看这步'})
        self.assertTrue(result['ok']);self.assertTrue(self.claim['_reply_submitted'])
        outbox=[json.loads(line) for line in (self.base/'state/outbox.jsonl').read_text().splitlines()]
        self.assertTrue(outbox[-1]['rendered_math'])
        request={'action':'tool','tool':'math.send','input':{'markdown':'$x=1$'},'text':''}
        self.assertEqual(parse_response(json.dumps(request))['tool'],'math.send')
        request['tool']='stickers.collect'
        with self.assertRaises(ValueError):execute(parse_response(json.dumps(request)),self.claim,self.c,self.base)

    async def test_tool_roundtrip_returns_results_to_model_once(self):
        request={'action':'tool','tool':'calculator.evaluate','input':{'expression':'2+2'},'text':'未计算'}
        prompts=[]
        async def generate(prompt,claim):
            prompts.append(prompt);return {'action':'reply','text':'算出来是4喵'}
        result=await resolve_tools(request,self.claim,self.c,self.base,generate,{'calls':[time.time()]},{'max_calls_per_hour':20})
        self.assertEqual(result['action'],'reply');self.assertEqual(len(prompts),1);self.assertIn('calculator.evaluate',prompts[0])

if __name__=='__main__':unittest.main()
