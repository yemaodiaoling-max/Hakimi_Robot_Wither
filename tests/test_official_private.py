"""C2C protocol fixtures; no real messages or model requests."""
import asyncio,json,shutil,tempfile,time,unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import AsyncMock,patch
from hakimi.transport import official_transport as transport
from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.bridge.delivery_guard import DeliveryGuard
from hakimi.models.provider_health import ProviderHealth
from _repo import repo_file

ROOT=Path(__file__).resolve().parents[1]

class PrivateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'tests');self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();(self.base/'state/img_cache').mkdir(parents=True);(self.base/'stickers').mkdir()
        self.now=time.time();self.store=transport.RouteStore(self.base/'state/routes.sqlite3',clock=lambda:self.now)
        self.store.set('group_openid','GROUP_TEST');self.api=AsyncMock()
        self.api.call.return_value={'id':'SENT_ONE'}
        self.adapter=transport.OfficialAdapter(self.base,{'test_group_number':'850245632','allow_proactive':True,'allow_private_chat':True},self.api,self.store,clock=lambda:self.now)

    def data(self,id='PRIVATE_INPUT',user='CONTACT_A',**extra):
        return {'id':id,'author':{'user_openid':user,'username':'同名'},'content':'测试上下文',
                'timestamp':datetime.fromtimestamp(self.now,timezone.utc).isoformat(),**extra}

    async def receive(self,**extra):return await self.adapter.translate('C2C_MESSAGE_CREATE',self.data(**extra))

    def pending(self,event,segments,provider='gemini',source=None):
        session='private:'+str(event['user_id']);c=TurnCoordinator(self.base,clock=lambda:self.now)
        guard=DeliveryGuard(self.base/'state/delivery_state.json',self.base/'state/outbox.jsonl',clock=lambda:self.now)
        c.publish({'session_key':session,'trigger_id':'private-'+provider,'created_at':self.now,
                   'recent_context_messages':[{'session_key':session,'message_id':str(event['message_id']),'user_id':str(event['user_id']),'text':event['raw_message']}]})
        if provider=='dsh':
            ProviderHealth(self.base,clock=lambda:self.now).set_mode('dsh');claim=c.claim_fallback()
        else:claim=c.claim_primary()
        self.assertIsNotNone(claim)
        self.store.attach_turn(claim['trigger_id'],source or str(event['message_id']))
        self.assertTrue(c.submit_reply(claim['token'],segments,provider)['accepted'])
        message=guard.next_message();self.assertIsNotNone(message)
        return message,c,guard

    async def test_c2c_normalization_dedup_and_no_group_registration(self):
        event=await self.receive(content='[CQ:at,qq=12] hello',group_openid='GROUP_TEST')
        self.assertEqual(event['message_type'],'private');self.assertNotIn('group_id',event)
        self.assertIn('&#91;CQ:at,qq=12&#93;',event['raw_message'])
        self.assertEqual(str(event['user_id']),self.store.actor('CONTACT_A'))
        self.assertNotIn('CONTACT_A',json.dumps(event));self.assertEqual(len(self.adapter.routes),1)
        self.assertEqual(self.store.message(event['message_id'])['group_openid'],'private:CONTACT_A')
        self.assertIsNone(await self.receive())
        self.assertIsNotNone(self.adapter.route_for_session('private:'+str(event['user_id'])))
        self.assertIsNone(self.adapter.route_for_session('private:'+self.store.actor('UNSEEN')))
        restored=transport.OfficialAdapter(self.base,self.adapter.cfg,self.api,self.store)
        self.assertIsNotNone(restored.route_for_session('private:'+str(event['user_id'])))

    async def test_disabled_private_and_malformed_events_do_not_change_group_routes(self):
        self.adapter.cfg['allow_private_chat']=False
        self.assertIsNone(await self.receive())
        self.adapter.cfg['allow_private_chat']=True
        self.assertIsNone(await self.receive(author={'id':'GROUP_MEMBER_ONLY'}))
        self.assertIsNone(await self.receive(timestamp='2020-01-01T00:00:00Z'))
        self.assertEqual(len(self.adapter.private_routes),0);self.assertEqual(len(self.adapter.routes),1)

    async def test_text_uses_private_api_without_group_permission_or_mentions(self):
        event=await self.receive();message,c,guard=self.pending(event,['第一条','第二条'])
        result=await self.adapter.send(*message)
        self.assertEqual(result['status'],'success')
        method,path,body=self.api.call.await_args.args
        self.assertEqual(path,'/v2/users/CONTACT_A/messages');self.assertEqual(body['msg_id'],'PRIVATE_INPUT')
        self.assertEqual(body['msg_seq'],1);self.assertNotIn('message_reference',body)
        self.assertEqual(self.api.call.await_count,1)
        guard.begin_attempt();guard.finish_attempt('success');self.now+=20
        self.api.call.return_value={'id':'SENT_TWO'}
        self.assertEqual((await self.adapter.send(*guard.next_message()))['status'],'success')
        self.assertEqual(self.api.call.await_args.args[2]['msg_seq'],2)

    async def test_dsh_winner_reuses_same_private_send_path(self):
        event=await self.receive();message,c,_=self.pending(event,['继续用哈基米口吻'],'dsh')
        self.assertEqual(c.snapshot()['provider'],'dsh')
        self.assertEqual((await self.adapter.send(*message))['status'],'success')
        self.assertEqual(self.api.call.await_args.args[1],'/v2/users/CONTACT_A/messages')

    async def test_group_source_cannot_authorize_private_even_with_same_openid_bytes(self):
        event=await self.receive(user='GROUP_TEST')
        wrong,_=self.store.remember('GROUP_SOURCE',None,self.now,group='GROUP_TEST',user_id='22')
        message,_,_=self.pending(event,['never send'],source=wrong)
        self.assertEqual((await self.adapter.send(*message))['status'],'rejected');self.api.call.assert_not_awaited()

    async def test_private_image_registration_and_outgoing_media_endpoint(self):
        def download(url,path):
            from PIL import Image
            Image.new('RGB',(8,8),'white').save(path)
        with patch.object(transport,'download_image',side_effect=download):
            event=await self.receive(attachments=[{'content_type':'image/png','url':'https://example.com/test.png'}])
        self.assertIn('img_private_'+str(event['user_id'])+'_',event['raw_message'])
        path=self.base/'stickers/probe.jpg';shutil.copy2(next((self.base/'state/img_cache').glob('*.jpg')),path)
        segment=f'[CQ:image,file={path.as_uri()},sub_type=1]'
        message,_,_=self.pending(event,[segment]);self.api.upload_image.return_value='PRIVATE_MEDIA'
        self.assertEqual((await self.adapter.send(*message))['status'],'success')
        self.api.upload_image.assert_awaited_once_with('CONTACT_A',path,private=True)
        self.assertEqual(self.api.call.await_args.args[2]['content'],' ')

    async def test_attachment_upload_cache_is_separate_for_groups_and_users(self):
        from hakimi.transport.official_files import upload
        path=self.base/'note.pdf';path.write_bytes(b'%PDF protocol fixture')
        api=AsyncMock();api._file_cache={};api.call.return_value={'file_info':'FILE_INFO','ttl':300}
        await upload(api,'SAME',path,4);await upload(api,'SAME',path,4,private=True)
        paths=[call.args[1] for call in api.call.await_args_list]
        self.assertEqual(paths,['/v2/groups/SAME/files','/v2/users/SAME/files'])
        await upload(api,'SAME',path,4,private=True);self.assertEqual(api.call.await_count,2)

    async def test_gateway_delivers_c2c_to_existing_queue(self):
        self.api.token.return_value='test-token';self.api.call.return_value={'url':'wss://api.bot.qq.com/gateway'}
        self.api.clock=lambda:self.now;self.api._expires=self.now+3600
        frames=[{'op':10,'d':{'heartbeat_interval':1000}},
                {'op':0,'s':1,'t':'READY','d':{'session_id':'test-session','user':{'id':'BOT_SELF'}}},
                {'op':0,'s':2,'t':'C2C_MESSAGE_CREATE','d':self.data()}]
        class Socket:
            async def recv(self):
                if frames:return json.dumps(frames.pop(0))
                await asyncio.sleep(30)
            async def send(self,value):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
        queue=asyncio.Queue()
        with patch.object(transport.websockets,'connect',return_value=Socket()):
            task=asyncio.create_task(self.adapter.gateway(queue,lambda *args:None))
            try:event=await asyncio.wait_for(queue.get(),2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(event['message_type'],'private')

    async def test_same_actor_personal_memory_and_persona_are_available_without_group_raw_chat(self):
        from hakimi.memory.smart_memory import save_memory
        from hakimi.prompt.prompt_context import render_prompt,build_context
        from hakimi.models.fallback_supervisor import fallback_prompt
        event=await self.receive();user=str(event['user_id']);session='private:'+user
        shutil.copy2(repo_file('style_guide.md'),self.base/'style_guide.md');shutil.copy2(repo_file('group_profiles.json'),self.base/'group_profiles.json')
        (self.base/'config.json').write_text('{}')
        save_memory(self.base,'group:123',{'user_id':user,'key':'称呼','note':'称呼我为MEMORY_CHECK'}, {user:{}},now=self.now)
        payload={'session_key':session,'recent_context_messages':[{'session_key':session,'message_id':str(event['message_id']),'user_id':user,'text':'还记得我吗'}]}
        self.assertIn('MEMORY_CHECK',json.dumps(build_context(payload,self.base),ensure_ascii=False))
        for prompt in (render_prompt(payload,self.base),fallback_prompt(payload,self.base)):
            self.assertIn('哈基米',prompt);self.assertIn('MEMORY_CHECK',prompt)
        other='private:'+self.store.actor('CONTACT_B')
        self.assertNotIn('MEMORY_CHECK',json.dumps(build_context({'session_key':other,'recent_context_messages':[]},self.base)))

if __name__=='__main__':unittest.main()
