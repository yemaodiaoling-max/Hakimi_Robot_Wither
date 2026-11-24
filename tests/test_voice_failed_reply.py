import unittest
from unittest.mock import Mock, patch
from hakimi.voice.tts import _explicit_text, failed_voice_reply
from hakimi.models import dsh_tools

class VoiceFailedReplyTests(unittest.TestCase):
    def test_reported_request_is_explicit(self):
        self.assertTrue(_explicit_text('@哈基米 猫猫，用语音反驳一下天然卷好不好'))
        self.assertFalse(_explicit_text('他说：用语音反驳一下天然卷好不好'))
        self.assertFalse(_explicit_text('不用语音反驳，打字就行'))

    def test_text_only_after_known_failure(self):
        result = {'error':'use_text_this_turn'}
        self.assertIn('没发出去', failed_voice_reply(result, '你好'))
        self.assertTrue(failed_voice_reply(result, '你好').endswith('你好'))
        self.assertIsNone(failed_voice_reply({'error':'already_replied'}, '你好'))
        self.assertIsNone(failed_voice_reply({'error':'turn_expired'}, '你好'))

    def test_dsh_failed_tool_finishes_with_truthful_text(self):
        claim = {'token':'t','payload':{}}
        with patch('hakimi.voice.tts.speak', return_value={'error':'voice_generation_timeout_use_text'}):
            result = dsh_tools.execute({'tool':'voice.speak','input':{'text':'你好'}}, claim, Mock(), '.')
        self.assertEqual(result['terminal']['action'], 'reply')
        self.assertIn('没发出去', result['terminal']['text'])
