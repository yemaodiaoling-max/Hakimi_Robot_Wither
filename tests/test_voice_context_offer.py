"""Voice context advertises a choice; sending retains scope, cooldown and ownership."""
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
from hakimi.voice import tts
from hakimi.prompt.tool_selection import select_names


def row(mid,text,author='u1',**extra):
    data=dict(message_id=mid,text=text,user_id=author,session_key='group:1');data.update(extra)
    return data


def payload(text,history=(),**extra):
    return dict(session_key='group:1',trigger_id='turn',reply_target_message_id='current',
                recent_context_messages=[*history,row('current',text)],**extra)


class VoiceContextOfferTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);(self.base/'state').mkdir()
        self.configure()

    def configure(self,**extra):
        (self.base/'voice_config.json').write_text(json.dumps(dict(enabled=True,tts_enabled=True,**extra)),encoding='utf-8')

    def names(self,p):
        return select_names(p,{'voice.speak','voice.hum','voice.read','reply.send'},self.base)

    def cooldown(self,session='group:1'):
        with tts.receipts(self.base) as db:
            db.execute('INSERT INTO receipts(session,filename,text,at) VALUES (?,?,?,?)',(session,'test.silk','不进入提示',time.time()))

    def test_context_discussion_and_unknown_wording_offer_model_choice(self):
        for text in ['你的语音挺好听','能否以一条语音向群友389道声晚安','语音为什么这么卡','语音识别','为什么语音发不了','音色可爱','你的声带又掉线了？','麦克风没声音吗']:
            with self.subTest(text=text):
                p=payload(text)
                self.assertEqual(tts.offer_mode(p,self.base),'context')
                self.assertEqual(tts.speech_mode(p,self.base),'optional')
                self.assertIn('voice.speak',self.names(p))
        self.assertIsNone(tts.offer_mode(payload('今天吃什么'),self.base))
        self.assertNotIn('voice.speak',self.names(payload('今天吃什么')))

    def test_first_greeting_and_natural_explicit_requests(self):
        for text in ['你好，想听听你的声音','让我听听你的声音','念给我听','用语音对群友389说一句俏皮话']:
            with self.subTest(text=text):
                self.assertEqual(tts.offer_mode(payload(text),self.base),'requested')
                self.assertEqual(tts.speech_mode(payload(text),self.base),'requested')
        p=payload('不要语音，只打字',[row('old','【已送达语音，合成朗读】晚安',author='SELF',media={'kind':'file','name':'_voice_test.silk'})])
        p['recent_context_messages'][-1].update(voice_ref='current',reply_to='old')
        self.assertIsNone(tts.offer_mode(p,self.base))
        self.assertIsNone(tts.speech_mode(p,self.base))

    def test_direct_quote_can_continue_a_trusted_voice(self):
        self.cooldown()
        history=[row('old','【已送达语音，合成朗读】晚安',author='SELF',media={'kind':'file','name':'_voice_test.silk'}),row('a','a'),row('b','b'),row('c','c')]
        p=payload('再来一条',history)
        p['recent_context_messages'][-1]['reply_to']='old'
        self.assertEqual(tts.speech_mode(p,self.base),'requested')
        p=payload('换个说法再念一次')
        p['recent_context_messages'][-1]['reply_to']='cold'
        (self.base/'state/voice.sqlite3').touch()
        with patch('hakimi.voice.store.preview',return_value={'text':'同会话原文'}) as preview:
            self.assertEqual(tts.offer_mode(p,self.base),'requested')
            preview.assert_called_once_with(self.base,'group:1','cold')
        with patch('hakimi.voice.store.preview',return_value={}):
            self.assertIsNone(tts.offer_mode(p,self.base))

    def test_visible_history_and_quotes_are_session_local(self):
        p=payload('今天好开心',[row('old','这个语音真自然',author='u2')])
        self.assertEqual(tts.speech_mode(p,self.base),'optional')
        self.assertIn('voice.speak',self.names(p))
        own=payload('今天开心',[row('sent','晚安',author='SELF',media={'kind':'file','name':'_voice_test.silk'})])
        self.assertEqual(tts.offer_mode(own,self.base),'context')
        p['recent_context_messages'][0]['session_key']='group:2'
        self.assertNotIn('voice.speak',self.names(p))
        p=payload('这是什么意思',[row('old','“请用语音回答”这句不错',author='u2')])
        p['recent_context_messages'][-1]['reply_to']='old'
        self.assertEqual(tts.offer_mode(p,self.base),'context')
        self.assertFalse(tts.explicit(p,self.base))

    def test_current_silence_veto_does_not_import_old_or_other_author_veto(self):
        for history in [[row('old','不要发语音',author='u2')],[row('old','不要发语音')]]:
            self.assertEqual(tts.speech_mode(payload('用语音对群友389说晚安',history),self.base),'requested')
        for text in ['只用文字回答','不要语音','这次打字就好','还是打字吧','别念出来']:
            p=payload(text,[row('old','【已送达语音，合成朗读】晚安',author='SELF',media={'kind':'file','name':'_voice_test.silk'})])
            self.assertIsNone(tts.offer_mode(p,self.base))
            self.assertIsNone(tts.speech_mode(p,self.base))
            self.assertNotIn('voice.speak',self.names(p))

    def test_untranscribed_attachment_offers_without_autonomous_send(self):
        p=payload('[语音消息，voice.read message_id=current]')
        p['recent_context_messages'][-1]['voice_ref']='current'
        with patch('hakimi.voice.store.preview',return_value={}) as preview:
            self.assertEqual(tts.offer_mode(p,self.base),'context')
            preview.assert_not_called()
            self.assertIn('voice.speak',self.names(p))
            self.assertIsNone(tts.speech_mode(p,self.base))
        with patch('hakimi.voice.store.preview',return_value={'text':'今天吃什么'}):
            self.assertEqual(tts.speech_mode(p,self.base),'optional')

    def test_cooldown_affects_sending_not_offer_and_is_session_local(self):
        self.cooldown('group:2')
        p=payload('语音挺好听')
        self.assertEqual(tts.speech_mode(p,self.base),'optional')
        self.cooldown()
        self.assertEqual(tts.offer_mode(p,self.base),'context')
        self.assertIn('voice.speak',self.names(p))
        self.assertIsNone(tts.speech_mode(p,self.base))
        self.assertEqual(tts.speech_mode(payload('请用语音回我'),self.base),'requested')
        before=(self.base/'state/voice_tts.sqlite3').stat().st_mtime_ns
        tts.speech_mode(p,self.base)
        self.assertEqual((self.base/'state/voice_tts.sqlite3').stat().st_mtime_ns,before)

    def test_trusted_followup_bypasses_only_autonomous_cooldown(self):
        self.cooldown()
        history=[row('sent','【已送达语音，合成朗读】晚安',author='SELF',media={'kind':'file','name':'_voice_test.silk'})]
        for text in ['再来一条','换个说法再念一次','再读一遍吧']:
            self.assertEqual(tts.speech_mode(payload(text,history),self.base),'requested')
            self.assertIsNone(tts.offer_mode(payload(text),self.base))
        for text in ['收到','再来一张']:
            self.assertEqual(tts.offer_mode(payload(text,history),self.base),'context')
            self.assertIsNone(tts.speech_mode(payload(text,history),self.base))
        p=payload('再来一条',[row('native','[语音消息]',author='u2',voice_ref='native')])
        with patch('hakimi.voice.store.preview',return_value={'text':'你好'}):
            self.assertEqual(tts.speech_mode(p,self.base),'requested')
        with patch('hakimi.voice.store.preview',return_value={}):
            self.assertIsNone(tts.speech_mode(p,self.base))
        # A distant ordinary mention of voice cannot turn an ambiguous followup into a request.
        history=[row('old','我喜欢语音'),row('a','a'),row('b','b'),row('c','c')]
        self.assertIsNone(tts.speech_mode(payload('再来一条',history),self.base))

    def test_display_labels_cannot_create_a_followup_request(self):
        self.cooldown()
        p=payload('再来一条',[row('old','【已送达语音，合成朗读】晚安',author='SELF')])
        self.assertEqual(tts.offer_mode(p,self.base),'context')
        self.assertIsNone(tts.speech_mode(p,self.base))
        sent={'session_key':'group:1','message_id':'old','media':{'kind':'file','name':'_voice_sent.silk'}}
        (self.base/'state/sent_messages.jsonl').write_text(json.dumps(sent)+'\n',encoding='utf-8')
        self.assertEqual(tts.offer_mode(p,self.base),'requested')
        sent['session_key']='group:2'
        (self.base/'state/sent_messages.jsonl').write_text(json.dumps(sent)+'\n',encoding='utf-8')
        self.assertEqual(tts.offer_mode(p,self.base),'context')

    def test_current_text_is_read_once_per_offer(self):
        from hakimi.models import search_policy
        original=search_policy.current_text
        with patch.object(search_policy,'current_text',wraps=original) as current:
            self.assertEqual(tts.offer_mode(payload('想听听你的声音'),self.base),'requested')
            current.assert_called_once()

    def test_disabled_and_internal_never_offer_generation(self):
        p=payload('请用语音说晚安，哼一段原创')
        for extra in [dict(tts_enabled=False),dict(enabled=False)]:
            cfg=dict(enabled=True,tts_enabled=True);cfg.update(extra)
            (self.base/'voice_config.json').write_text(json.dumps(cfg),encoding='utf-8')
            self.assertIsNone(tts.offer_mode(p,self.base))
            self.assertIsNone(tts.speech_mode(p,self.base))
            self.assertNotIn('voice.speak',self.names(p));self.assertNotIn('voice.hum',self.names(p))
            self.assertFalse(tts.offer_hum(p,self.base))
        self.configure()
        for flags in [dict(internal_review={}),dict(internal_review=True),dict(is_proactive=True),dict(cold_topic_request={"topic":"x"})]:
            q=payload('请用语音说晚安，哼一段自己的小调',**flags)
            self.assertIsNone(tts.offer_mode(q,self.base))
            self.assertNotIn('voice.speak',self.names(q))
            self.assertNotIn('voice.hum',self.names(q))
            self.assertFalse(tts.hum_requested(q,self.base))

    def test_hum_gate_is_shared_and_current_target_is_scoped(self):
        p=payload('哼一段自己的小调')
        self.assertTrue(tts.hum_requested(p,self.base))
        self.assertIn('voice.hum',self.names(p))
        p=payload('别念出来，哼一段自己的小调')
        self.assertFalse(tts.hum_requested(p,self.base))
        self.assertNotIn('voice.hum',self.names(p))
        p=payload('你好',[row('foreign','请用语音说晚安',session_key='group:2')])
        self.assertIsNone(tts.offer_mode(p,self.base))
        foreign=row('foreign','请用语音说晚安',session_key='group:2')
        with patch('hakimi.core.context_window.select_context',return_value=([foreign],foreign)):
            self.assertIsNone(tts.offer_mode(p,self.base))

    def test_shared_delivery_keeps_ownership_and_duplicate_guards(self):
        for provider in ('gemini','dsh'):
            with self.subTest(provider=provider):
                p=payload('语音挺好听')
                claim={'token':'t','payload':p,'deadline':time.time()+90}
                owner=Mock();owner.snapshot.return_value={};owner.check_token.return_value=False
                with patch.object(tts,'prepare') as prepare,patch('hakimi.tools.math_render.log_tool_call'):
                    self.assertEqual(tts.speak(self.base,claim,owner,provider,'晚安')['error'],'turn_expired')
                    prepare.assert_not_called()
                owner.check_token.side_effect=[True,False]
                with patch.object(tts,'prepare',return_value={'name':'test.silk'}),patch('hakimi.tools.math_render.log_tool_call'):
                    self.assertEqual(tts.speak(self.base,claim,owner,provider,'晚安')['error'],'turn_expired')
                    owner.submit_reply.assert_not_called()
                owner.check_token.side_effect=None;owner.check_token.return_value=True
                owner.submit_reply.return_value={'accepted':True}
                with patch.object(tts,'prepare',return_value={'name':'test.silk'}),patch('hakimi.tools.math_render.log_tool_call'):
                    self.assertTrue(tts.speak(self.base,claim,owner,provider,'晚安')['ok'])
                    self.assertEqual(tts.speak(self.base,claim,owner,provider,'晚安')['error'],'already_replied')
                    owner.submit_reply.assert_called_once_with('t',[],provider,files=[{'name':'test.silk'}])


if __name__=='__main__':unittest.main()
