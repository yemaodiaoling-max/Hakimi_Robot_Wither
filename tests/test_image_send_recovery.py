import asyncio,json,tempfile,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from hakimi.transport import official_transport as transport
from hakimi.ops import cloud_supervisor
from hakimi.bridge.delivery_guard import DeliveryGuard
from tests.test_delivery_guard import OfflineEventLoop

class ImageRecoveryTests(unittest.TestCase):
    def run_async(self,coro):return asyncio.run(coro,loop_factory=OfflineEventLoop)

    def test_large_image_reuses_chunked_upload_and_small_image_uses_direct(self):
        async def check():
            with tempfile.TemporaryDirectory() as d:
                path=Path(d)/'large.gif';path.write_bytes(b'x'*(5*1024*1024+3))
                api=transport.OfficialAPI('fixture','fixture')
                api.call=AsyncMock(side_effect=[{'upload_id':'id','block_size':8*1024*1024,'parts':[{'index':1,'presigned_url':'https://upload.qq.com/part'}]}, {}, {'file_info':'uploaded'}])
                with patch('official_files.put_part',return_value='md5') as put:
                    self.assertEqual(await api.upload_image('g',path),'uploaded');self.assertEqual(put.call_count,1)
                self.assertTrue(api.call.call_args_list[0].args[1].endswith('/upload_prepare'))
                self.assertEqual(api.call.call_args_list[0].args[2]['file_type'],1)
                path=Path(d)/'small.png';path.write_bytes(b'small')
                api.call=AsyncMock(return_value={'file_info':'small'})
                self.assertEqual(await api.upload_image('u',path,private=True),'small')
                self.assertEqual(api.call.call_args.args[1],'/v2/users/u/files')
        self.run_async(check())

    def test_image_failure_before_post_keeps_other_group_text_deliverable(self):
        async def check():
            with tempfile.TemporaryDirectory() as d:
                base=Path(d);(base/'state').mkdir();image=base/'large.png';image.write_bytes(b'image')
                now=[1000.];out=base/'state/outbox.jsonl';guard=DeliveryGuard(base/'state/delivery_state.json',out,clock=lambda:now[0])
                segment=f'[CQ:image,file={image.as_uri()},sub_type=0]'
                out.write_text(''.join(json.dumps(row)+'\n' for row in [
                    {'reply_id':'image','session_key':'group:1','segments':[segment],'created_at':1000.,'trigger_id':'img'},
                    {'reply_id':'text','session_key':'group:2','segments':['新文字'],'created_at':1000.}]))
                guard.next_message();guard.begin_attempt()
                store=transport.RouteStore(base/'state/official_routes.sqlite3');api=type('API',(),{})()
                api.upload_image=AsyncMock(side_effect=transport.OfficialError('UPLOAD_PART_FAILED'));api.call=AsyncMock()
                adapter=transport.OfficialAdapter(base,{},api,store,clock=lambda:1000.)
                adapter.routes={'g':{'openid':'g','local_id':'1','active':True}}
                with patch('image_permissions.allowed_images_for_session',return_value=[str(image)]),patch('codex_help.report_fault'):
                    result=await adapter.send('group:1',segment)
                self.assertEqual(result['status'],'file_error');api.call.assert_not_called()
                guard.finish_attempt(result['status']);self.assertEqual(guard.state['paused_until'],0)
                now[0]+=4
                self.assertEqual(guard.next_message(),('group:2','新文字'))
        self.run_async(check())

    def test_media_timeout_before_post_is_definitely_unsent(self):
        async def check():
            with tempfile.TemporaryDirectory() as d:
                base=Path(d);store=transport.RouteStore(base/'routes.sqlite3')
                adapter=transport.OfficialAdapter(base,{},None,store)
                async def fail(*args):raise TimeoutError()
                adapter._send_once=fail
                result=await adapter.send('group:1','[CQ:image,file=file:///image.png,sub_type=1]')
                self.assertEqual(result['status'],'file_error')
        self.run_async(check())

    def test_long_delivery_pause_is_visible_to_health_checks(self):
        with tempfile.TemporaryDirectory() as d,patch.object(cloud_supervisor,'RUNTIME',Path(d)):
            state=Path(d)/'delivery_state.json'
            state.write_text(json.dumps({'paused_until':2000}))
            self.assertTrue(cloud_supervisor.delivery_paused(1000))
            state.write_text(json.dumps({'paused_until':1015}))
            self.assertFalse(cloud_supervisor.delivery_paused(1000))

if __name__=='__main__':unittest.main()
