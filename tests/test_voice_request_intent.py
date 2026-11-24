"""Current-turn voice requests, shared by both model routes (no audio/network)."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from hakimi.voice import tts


def payload(text, **extra):
    return dict(session_key='group:1', trigger_id='turn-1', reply_target_message_id='m1',
                recent_context_messages=[dict(message_id='m1', user_id='u1', text=text)], **extra)


class VoiceRequestIntentTests(unittest.TestCase):
    def setUp(self):
        enabled=patch.object(tts,'settings',return_value={'enabled':True,'tts_enabled':True})
        enabled.start();self.addCleanup(enabled.stop)

    def test_recipient_beneficiary_and_modifiers(self):
        positive = [
            '用语音对群友389说一句俏皮话吧', '@哈基米 用语音对群友389说一句俏皮话吧',
            '请用语音温柔地对群友389说一句晚安', '能不能用语音替我向群友389问候一下',
            '可以用语音帮我给群友389说句生日快乐吗', '帮我发一条语音',
            '发一段语音给群友389', '给群友389发一条语音', '用语音回我',
            '用语音回答这个问题', '请朗读“你好，群友389”', '念给我听',
            '用声音对群友389说一句你好', 'read aloud', 'send a voice message',
            '晚安 可以给我发一条语音吗', 'please read aloud', '那你用语音说一遍',
            '你好呀 请帮我朗读一下', '你用语音对群友389解释为什么会下雨',
            '用语音解释这个功能支持什么能力',
            '刚才没听见，请用语音再说一遍', '昨天太吵了，今天请用语音说一遍',
        ]
        for text in positive:
            with self.subTest(text=text):
                self.assertTrue(tts.explicit(payload(text)))
                self.assertEqual(tts.speech_mode(payload(text), Path('.')), 'requested')

    def test_discussion_capability_quotes_and_silence(self):
        negative = [
            '昨天我用语音对群友389说了个笑话', '为什么用语音对方听不到我说话',
            '我经常用语音输入，语音回复挺方便', '昨天我用语音说了个笑话',
            '语音回复挺方便', '你的语音挺好听', '刚才那条语音说了什么', '语音为什么这么卡',
            '这条语音谁发的？', '你会发语音吗', '你能用语音说话吗？',
            '你支持语音回复吗', '他说用语音对群友389说一句俏皮话',
            '他说：用语音对群友389说一句俏皮话', '他说，用语音回我',
            '“用语音对群友389说一句俏皮话吧”是什么意思',
            '例如：请用语音回答', '[语音消息，voice.read message_id=m1]',
            '别用语音，打字就好', '不用语音对群友389说', '不要对群友389温柔地发语音', '请用语音回复，不过这次只用文字',
            '用语音对群友389说一句晚安，不要发语音',
        ]
        for text in negative:
            with self.subTest(text=text):
                self.assertFalse(tts.explicit(payload(text)))
                self.assertNotEqual(tts.speech_mode(payload(text), Path('.')),'requested')

    def test_old_quote_or_other_author_cannot_authorize(self):
        p=payload('这句话是什么意思')
        p['recent_context_messages'].insert(0,dict(message_id='old',user_id='u2',text='用语音对群友389说晚安'))
        p['recent_context_messages'][-1]['reply_to']='old'
        self.assertFalse(tts.explicit(p))
        p['recent_context_messages'][0]['user_id']='u1'
        self.assertFalse(tts.explicit(p))

    def test_native_voice_optional_keeps_text_only_veto(self):
        p=payload('只用文字回答')
        p['recent_context_messages'][0]['voice_ref']='m1'
        with patch('hakimi.voice.store.preview',return_value={'text':'只用文字回答'}):
            self.assertIsNone(tts.speech_mode(p,Path('.')))

    def test_gemini_selection_uses_shared_intent(self):
        from hakimi.prompt.tool_selection import select_names
        with tempfile.TemporaryDirectory() as root:
            names={'voice.speak','reply.send'}
            self.assertIn('voice.speak',select_names(payload('用语音对群友389说一句俏皮话吧'),names,Path(root)))
            self.assertIn('voice.speak',select_names(payload('他说：用语音对群友389说晚安'),names,Path(root)))
            self.assertFalse(tts.explicit(payload('他说：用语音对群友389说晚安')))

    def test_both_providers_reach_same_prepare_and_submit(self):
        for provider in ('gemini','dsh'):
            with self.subTest(provider=provider):
                p=payload('用语音对群友389说一句俏皮话吧')
                claim={'token':'token','payload':p,'deadline':time.time()+90}
                owner=Mock();owner.check_token.return_value=True
                owner.snapshot.return_value={}
                owner.submit_reply.return_value={'accepted':True}
                def prepare(base,incoming,text,timeout):
                    self.assertEqual(tts.speech_mode(incoming,base),'requested')
                    return {'name':'test.silk'}
                with patch.object(tts,'prepare',side_effect=prepare) as generated, patch('hakimi.tools.math_render.log_tool_call'):
                    self.assertTrue(tts.speak(Path('.'),claim,owner,provider,'你好')['ok'])
                    generated.assert_called_once()
                owner.submit_reply.assert_called_once_with('token',[],provider,files=[{'name':'test.silk'}])

    def test_result_log_redacts(self):
        for ok in (False,True):
            with self.subTest(ok=ok):
                claim={'payload':payload('用语音对群友389说晚安')}
                result={'ok':True,'voice':{'backend':'gpt-sovits','text':'DO_NOT_LOG'}} if ok else {'error':'voice_deadline_use_text'}
                with patch.object(tts,'_speak',return_value=result), patch('hakimi.tools.math_render.log_tool_call') as log:
                    self.assertIs(tts.speak(Path('.'),claim,Mock(),'dsh','SECRET_TEXT'),result)
                    log.assert_called_once()
                    fields=log.call_args.kwargs
                    self.assertEqual(fields['trigger_id'],'turn-1')
                    self.assertEqual(fields['provider'],'dsh')
                    self.assertNotIn('SECRET',str(log.call_args))
                    self.assertNotIn('DO_NOT_LOG',str(log.call_args))
                    if ok:self.assertEqual(fields['backend'],'gpt-sovits')
                    else:self.assertEqual(fields['error'],'voice_deadline_use_text')
                    self.assertEqual(log.call_args.args[2],'ok' if ok else 'error')

    def test_same_turn_failure_then_success_logs_both_real_attempts(self):
        claim={'payload':payload('发语音')}
        failure={'error':'voice_text_must_be_short_plain_text'}
        success={'ok':True,'voice':{'backend':'gpt-sovits'}}
        with patch.object(tts,'_speak',side_effect=[failure,success]), patch('hakimi.tools.math_render.log_tool_call') as log:
            self.assertIs(tts.speak(Path('.'),claim,Mock(),'dsh','x'*81),failure)
            self.assertIs(tts.speak(Path('.'),claim,Mock(),'dsh','你好'),success)
            self.assertEqual([call.args[2] for call in log.call_args_list],['error','ok'])
        claim={'payload':payload('发语音'),'_reply_submitted':True}
        with patch.object(tts,'_speak',return_value={'error':'already_replied'}), patch('hakimi.tools.math_render.log_tool_call') as log:
            tts.speak(Path('.'),claim,Mock(),'dsh','你好')
            log.assert_not_called()

    def test_early_rejection_and_exception_are_visible_without_changing_result(self):
        owner=Mock();owner.check_token.return_value=False
        claim={'token':'DO_NOT_LOG','payload':payload('发语音')}
        with patch('hakimi.tools.math_render.log_tool_call') as log:
            self.assertEqual(tts.speak(Path('.'),claim,owner,'gemini','DO_NOT_LOG'),{'error':'turn_expired'})
            self.assertEqual(log.call_args.kwargs['error'],'turn_expired')
            self.assertNotIn('DO_NOT_LOG',str(log.call_args))
        claim={'payload':payload('发语音')}
        with patch.object(tts,'_speak',side_effect=ValueError('DO_NOT_LOG')), patch('hakimi.tools.math_render.log_tool_call') as log:
            with self.assertRaisesRegex(ValueError,'DO_NOT_LOG'):
                tts.speak(Path('.'),claim,owner,'gemini')
            self.assertEqual(log.call_args.kwargs['error'],'voice_exception_ValueError')
            self.assertNotIn('DO_NOT_LOG',str(log.call_args))


if __name__=='__main__':
    unittest.main()
