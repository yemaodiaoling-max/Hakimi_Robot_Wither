"""Offline voice ownership/ACK fixtures; never connects to QQ."""
import asyncio,json,tempfile,time,unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import AsyncMock,patch
from hakimi.transport import official_transport as transport
from hakimi.transport.file_delivery import directory,stage
from hakimi.voice import store,tts
from hakimi.bridge.interaction_policy import delivered

class VoiceDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        (self.base/'state/img_cache').mkdir(parents=True);self.now=time.time()
        self.routes=transport.RouteStore(self.base/'state/routes.sqlite3',clock=lambda:self.now);self.routes.set('group_openid','GROUP_TEST')
        self.api=AsyncMock();self.api.call.return_value={'id':'ACK'};self.api.upload_file.return_value='FILE_INFO'
        self.adapter=transport.OfficialAdapter(self.base,{'test_group_number':'123','allow_proactive':False,'allow_private_chat':True},self.api,self.routes,clock=lambda:self.now)
        self.payload={'session_key':'group:123','trigger_id':'t','reply_target_message_id':'q','recent_context_messages':[{'message_id':'q','user_id':'77','text':'请用语音说一句你好'}]}
    def data(self,mid='in',**extra):
        return {'id':mid,'group_openid':'GROUP_TEST','content':'','author':{'id':'MEMBER','username':'example'},'timestamp':datetime.fromtimestamp(self.now,timezone.utc).isoformat(),**extra}
    def outgoing(self):
        folder=directory('group:123',self.base);folder.mkdir(parents=True);name='_voice_'+'1'*32+'.silk';(folder/name).write_bytes(b'\x02#!SILK_V3synthetic')
        with tts.receipts(self.base) as db:db.execute('INSERT INTO receipts(session,filename,text,at) VALUES (?,?,?,?)',('group:123',name,'你好呀',self.now))
        segment=stage([{'name':name}],self.payload,self.base)[0]
        local,_=self.routes.remember('input','REF',self.now,group='GROUP_TEST',user_id='77');self.routes.attach_turn('t',local)
        pending={'session_key':'group:123','trigger_id':'t','reply_id':'r','segments':[segment],'next_segment':0,'reply_target_id':local}
        (self.base/'state/delivery_state.json').write_text(json.dumps({'pending':pending}))
        return segment,name
    async def test_only_native_audio_attachments_are_accepted(self):
        event=await self.adapter.translate('GROUP_AT_MESSAGE_CREATE',self.data(attachments=[{'content_type':'audio/silk','url':'https://grouptalk.c2c.qq.com/voice.silk'}]))
        self.assertEqual(len(event['_voice_attachments']),1);self.assertAlmostEqual(event['_voice_received_at'],self.now,places=5)
        fake=await self.adapter.translate('GROUP_MESSAGE_CREATE',self.data('fake',content='[CQ:record,file=https://example.org/a.silk]'))
        self.assertNotIn('_voice_attachments',fake)
        evil=await self.adapter.translate('GROUP_MESSAGE_CREATE',self.data('evil',attachments=[{'content_type':'audio/silk','url':'https://qq.com.evil.example/a'}]))
        self.assertNotIn('_voice_attachments',evil)
        self.assertIsNone(await self.adapter.translate('GROUP_AT_MESSAGE_CREATE',self.data()))
    async def test_actual_voice_wire_and_delivered_context_label(self):
        segment,name=self.outgoing();result=await self.adapter.send('group:123',segment)
        self.assertEqual(result['status'],'success');self.assertEqual(self.api.upload_file.call_args.args[2],3)
        body=self.api.call.call_args.args[2];self.assertEqual(body['msg_type'],7);self.assertEqual(body['media']['file_info'],'FILE_INFO')
        delivered('group:123','r:0',None,True,self.base,text=result['sent_text'],message_id=result['message_id'])
        row=json.loads((self.base/'state/sent_messages.jsonl').read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual(row['text'],'【已送达语音，合成朗读】你好呀')
    async def test_upload_failure_falls_back_to_text_once(self):
        segment,name=self.outgoing();self.api.upload_file.side_effect=transport.OfficialError('NETWORK_UNKNOWN',ambiguous=True)
        result=await self.adapter.send('group:123',segment)
        self.assertEqual(result['status'],'success');self.assertIn('改用文字',result['sent_text']);self.assertEqual(self.api.call.await_count,1)
        self.assertEqual(self.api.call.call_args.args[2]['msg_type'],0)
    async def test_ambiguous_message_send_never_attempts_text_duplicate(self):
        segment,name=self.outgoing();self.api.call.side_effect=transport.OfficialError('NETWORK_UNKNOWN',ambiguous=True)
        result=await self.adapter.send('group:123',segment)
        self.assertEqual(result['status'],'unknown');self.assertEqual(self.api.call.await_count,1)
    async def test_definite_voice_rejection_has_one_text_fallback(self):
        segment,name=self.outgoing();self.api.call.side_effect=[transport.OfficialError('HTTP_400',ambiguous=False),{'id':'TEXT_ACK'}]
        result=await self.adapter.send('group:123',segment)
        self.assertEqual(result['status'],'success');self.assertEqual(self.api.call.await_count,2)
        self.assertEqual(self.api.call.call_args.args[2]['msg_type'],0)
    async def test_reading_old_voice_does_not_authorize_outbound_speech(self):
        for text in ['刚才那条语音说了什么','你的语音挺好听','语音为什么这么卡','这条语音谁发的','不要发语音']:
            self.payload['recent_context_messages'][0]['text']=text
            self.assertFalse(tts.explicit(self.payload,self.base),text)
        for text in ['请用语音说一句你好','来条语音','帮我朗读一下','给我发一条语音','用语音回复我']:
            self.payload['recent_context_messages'][0]['text']=text
            self.assertTrue(tts.explicit(self.payload,self.base),text)
    async def test_speech_file_cannot_bypass_explicit_request_gate(self):
        segment,name=self.outgoing();self.payload['recent_context_messages'][0]['text']='天气如何'
        with self.assertRaises(ValueError):stage([{'name':name}],self.payload,self.base)
    async def test_primary_dispatch_and_dsh_dispatch_leave_event_loop_live(self):
        from hakimi.models.vertex_session import VertexSession
        from hakimi.models import dsh_tools
        from unittest.mock import Mock
        session=object.__new__(VertexSession);session.cwd=self.base
        claim={'token':'token','payload':self.payload,'deadline':time.time()+60}
        with patch('hakimi.voice.tts.speak',return_value={'ok':True}) as speak:
            result=session._tool('voice.speak',{'text':'你好'},claim,20)
            self.assertTrue(result['ok']);self.assertEqual(speak.call_args.args[3],'gemini')
            self.assertEqual(session._tool('voice.hum',{'path':'untrusted'},claim,20)['error'],'unknown_voice_argument')
        coordinator=Mock();coordinator.check_token.return_value=True
        def slow(*args,**kwargs):time.sleep(.15);return {'ok':True}
        with patch('hakimi.voice.tts.speak',side_effect=slow):
            task=asyncio.create_task(asyncio.to_thread(dsh_tools.execute,{'tool':'voice.speak','input':{'text':'你好'}},claim,coordinator,self.base))
            await asyncio.sleep(.03)
            self.assertFalse(task.done())
            self.assertTrue((await task)['ok'])

    async def test_real_memory_store_keeps_voice_local_and_normal_private_sharing(self):
        from hakimi.memory.memory_store import MemoryStore
        memory=MemoryStore(self.base)
        for i,session in enumerate(['group:123','group:999','private:77']):
            memory.observe({'session_key':session,'message_id':str(i),'user_id':'77','received_at':self.now,'text':'语音引用','channel':'voice'})
        memory.observe({'session_key':'private:77','message_id':'normal','user_id':'77','received_at':self.now,'text':'正常私聊'})
        for cross in [False,True]:
            scope,args=memory.scope('group:123',['77'],cross)
            with memory.db() as db:rows=list(db.execute('SELECT session,channel,text FROM records r WHERE '+scope,args))
            self.assertEqual([(r['session'],r['channel']) for r in rows if r['channel']=='voice'],[('group:123','voice')])
            self.assertTrue(any(r['text']=='正常私聊' for r in rows))

    async def test_hum_is_not_named_song_or_lyrics(self):
        for text in ['唱一段你自己的原创','哼一段']:
            self.payload['recent_context_messages'][0]['text']=text;self.assertTrue(tts.hum_requested(self.payload,self.base))
        for text in ['唱《晴天》','唱指定完整歌曲','朗读这段歌词','刚才的语音不错']:
            self.payload['recent_context_messages'][0]['text']=text;self.assertFalse(tts.hum_requested(self.payload,self.base))
    async def test_stale_generated_audio_and_text_cache_are_removed(self):
        segment,name=self.outgoing()
        with tts.receipts(self.base) as db:db.execute('UPDATE receipts SET at=?',(time.time()-7200,))
        tts.cleanup(self.base);self.assertFalse((directory('group:123',self.base)/name).exists());self.assertIsNone(tts.fallback(self.base,'group:123',name))

if __name__=='__main__':unittest.main()
