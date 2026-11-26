import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hakimi.prompt.tool_selection import select_names
from hakimi.voice import tts
from hakimi.chat.file_delivery import directory,stage


class VoiceSelection(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base=Path(temp.name)
        (self.base/'state').mkdir()
        (self.base/'voice_config.json').write_text(json.dumps({'enabled':True,'tts_enabled':True}),encoding='utf-8')
        self.row=dict(session_key='group:123',user_id='5',message_id='11',text='你好')
        self.payload=dict(session_key='group:123',recent_context_messages=[self.row],
                          reply_target_message_id='11',trigger_id='turn1')
        self.names={'voice.speak','voice.hum','voice.read','reply.send'}

    def tools(self):
        return select_names(self.payload,self.names,self.base)&self.names

    def test_both_channels_offer_a_compact_choice_and_respect_text_only(self):
        from hakimi.models.fallback_prompt import fallback_prompt
        from hakimi.models.vertex_session import declarations
        for text, visible in [('特别喜欢你的语音', True), ('听过别人的语音', True),
                              ('还是打字吧', False), ('今天午饭吃什么', False)]:
            with self.subTest(text=text):
                self.row['text'] = text
                native = [tool for tool in declarations(self.tools()) if tool['name'] == 'voice.speak']
                self.assertEqual(len(native), int(visible))
                if visible:
                    self.assertNotIn('description', native[0])
                    schema = native[0]['parametersJsonSchema']
                    self.assertEqual(schema['properties'], {'text': {'type': 'string', 'maxLength': 80}})
                    self.assertEqual(schema['required'], ['text'])
                self.assertEqual(fallback_prompt(self.payload, self.base).count('voice.speak(text≤80字)'), int(visible))

    def test_text_speech_and_hum(self):
        self.assertEqual(self.tools(),{'reply.send'})
        self.row['text']='请用语音回复我'
        self.assertEqual(self.tools(),{'reply.send','voice.speak'})
        self.row['text']='哼一段自己的小调'
        self.assertEqual(self.tools(),{'reply.send','voice.hum'})

    def test_verified_input_cooldown_optout_and_prepared_file(self):
        self.row['voice_ref']='11'
        with patch('hakimi.memory.voice_receipts.preview',return_value={}):
            self.assertIn('voice.speak',self.tools())
            self.assertIsNone(tts.speech_mode(self.payload,self.base))
        with patch('hakimi.memory.voice_receipts.preview',return_value={'text':'你好'}):
            self.assertIn('voice.speak',self.tools())

            self.row['text']='只用文字'
            self.assertNotIn('voice.speak',self.tools())
            self.row['text']='你好'
            name=tts.generated_name('group:123','turn1','下午好')
            root=directory('group:123',self.base);root.mkdir(parents=True)
            (root/name).write_bytes(b'sample')
            with tts.receipts(self.base) as db:
                db.execute('insert into receipts(session,filename,text,at) values(?,?,?,?)',('group:123',name,'下午好',time.time()))
            self.assertIn('voice.speak',self.tools())
            self.assertIsNone(tts.speech_mode(self.payload,self.base))
            self.assertEqual(len(stage([{'name':name}],self.payload,self.base)),1)
            with self.assertRaises(ValueError):
                stage([{'name':name}],dict(self.payload,trigger_id='turn2'),self.base)
            self.row['text']='请用语音回复我'
            self.assertIn('voice.speak',self.tools())

    def test_speech_tool_success_finishes_in_one_model_request(self):
        from hakimi.models.vertex_session import VertexSession
        self.row['text']='请用语音回答'
        response={'candidates':[{'finishReason':'STOP','content':{'role':'model','parts':[
            {'functionCall':{'name':'voice.speak','args':{'text':'下午好'}}}]}}]}
        provider=Mock(side_effect=[response])
        executor=Mock(return_value={'ok':True})
        coordinator=Mock()
        session=VertexSession([],self.base,{},transport=provider,tool_runner=executor,image_loader=lambda claim:[])
        result=session.generate('测试',coordinator,dict(token='owner',payload=self.payload,trigger_id='turn1'))
        self.assertEqual(result['status'],'SUCCESS')
        self.assertTrue(result['reply_submitted'])
        provider.assert_called_once()
        executor.assert_called_once()
