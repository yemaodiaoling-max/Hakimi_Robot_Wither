import asyncio,base64,copy,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from test_official_adapter import fixture_root
from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.models.provider_health import ProviderHealth
from hakimi.chat.image_permissions import register_cached_image
from hakimi.models.fallback_media import prepare_images
from hakimi.models.fallback_runner import parse_response,FallbackError
from hakimi.models.dsh_tools import execute,resolve_tools
from hakimi.models.vertex_session import VertexSession

class MediaToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();fixture_root(self.root);self.base=self.root/'official_test_runtime'
        self.c=TurnCoordinator(self.base)
        self.payload={'session_key':'group:850245632','trigger_id':'media-recovery','recent_context_messages':[
            {'session_key':'group:850245632','user_id':'22','message_id':'1','text':'这个图是什么颜色'}]}
        folder=self.base/'state/img_cache';folder.mkdir(parents=True,exist_ok=True)
        self.image=folder/'img_group_850245632_probe.png';Image.new('RGB',(128,128),'blue').save(self.image)
        register_cached_image(self.payload['session_key'],self.image,self.base)

    def fallback_claim(self):
        ProviderHealth(self.base).set_mode('dsh');self.c.publish(self.payload)
        claim=self.c.claim_fallback();claim['_images']=[];return claim

    def test_vertex_media_stays_with_matching_function_response(self):
        for tool in ('image.read','video.watch','video.watch_miniapp'):
            with self.subTest(tool=tool):
                self.payload['trigger_id']=tool;self.c.publish(self.payload);claim=self.c.claim_primary()
                seen=[];model={'role':'model','parts':[
                    {'functionCall':{'name':tool,'id':'image-call','args':{'path':str(self.image)}},'thoughtSignature':'signature-1'},
                    {'functionCall':{'name':'time.now','id':'time-call','args':{}},'thoughtSignature':'signature-2'}]}
                def transport(body,timeout):
                    seen.append(copy.deepcopy(body))
                    content=model if len(seen)==1 else {'role':'model','parts':[{'text':'蓝色喵'}]}
                    return {'candidates':[{'content':content,'finishReason':'STOP'}]}
                session=VertexSession([],self.base,{},transport=transport,image_loader=lambda claim:[],
                    tool_runner=lambda *args:{'ok':True,'image_paths':[str(self.image)]})
                self.assertEqual(session.generate('看图',self.c,claim,timeout=30)['status'],'SUCCESS')
                contents=seen[1]['contents'];self.assertEqual(contents[-2],model)
                parts=contents[-1]['parts'];self.assertTrue(all(set(p)=={'functionResponse'} for p in parts))
                picture,clock=[p['functionResponse'] for p in parts]
                self.assertEqual(picture['id'],'image-call');self.assertEqual(clock['id'],'time-call')
                self.assertEqual(picture['parts'][0]['inlineData']['mimeType'],'image/png')
                self.assertNotIn('parts',clock);self.assertEqual(len(contents[0]['parts']),1)

    async def test_dsh_lists_then_reads_authorized_image_and_next_call_has_pixels(self):
        claim=self.fallback_claim()
        menu=execute({'tool':'tools.list','input':{}},claim,self.c,self.base)['result']['tools']
        self.assertIn('image.read',{r['name'] for r in menu});self.assertNotIn('stickers.collect',{r['name'] for r in menu})
        listed=execute({'tool':'images.list','input':{}},claim,self.c,self.base)
        self.assertEqual(listed['result']['paths'],[str(self.image)])
        request=parse_response(json.dumps({'action':'tool','text':'','tool':'image.read','input':{'path':str(self.image)},'sticker':None}))
        async def generate(prompt,active):
            self.assertEqual(len(active['_images']),1)
            pixels=active['_images'][0];self.assertTrue(base64.b64decode(pixels['data']).startswith(b'\xff\xd8'))
            self.assertNotIn(pixels['data'],prompt)
            return {'action':'reply','text':'蓝色喵'}
        result=await resolve_tools(request,claim,self.c,self.base,generate,{'calls':[time.time()]},{'max_calls_per_hour':20})
        self.assertEqual(result['text'],'蓝色喵')
        with self.assertRaises(ValueError):execute({'tool':'image.read','input':{'path':'/etc/passwd'}},claim,self.c,self.base)
        self.assertEqual(prepare_images(self.payload,self.base),[])
        self.c.complete(claim['token'])
        with self.assertRaises(ValueError):execute({'tool':'image.read','input':{'path':str(self.image)}},claim,self.c,self.base)

    async def test_dsh_invalid_tool_receipt_recovers_without_executing_it(self):
        claim=self.fallback_claim()
        for name,data in [('shell',{'command':'anything'}),('stickers.collect',{}),('memory.search',{'path':'anything'})]:
            with self.subTest(tool=name):
                request=parse_response(json.dumps({'action':'tool','text':'草稿','tool':name,'input':data}))
                with self.assertRaises(ValueError):execute(request,claim,self.c,self.base)
                calls=[]
                async def generate(prompt,active):
                    calls.append(prompt);self.assertIn('"ok":false',prompt)
                    return {'action':'reply','text':'接住啦'}
                result=await resolve_tools(request,claim,self.c,self.base,generate,{'calls':[time.time()]},{'max_calls_per_hour':20})
                self.assertEqual(result['text'],'接住啦');self.assertEqual(len(calls),1)
        for value in ({'action':'tool','text':'','tool':'time.now','input':[]},
                      {'action':'tool','text':'','tool':'time.now','input':{},'memory':{'note':'extra'}}):
            with self.assertRaises(FallbackError):parse_response(json.dumps(value))

if __name__=='__main__':unittest.main()
