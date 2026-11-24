import json
from pathlib import Path
import tempfile
import time
import unittest

from hakimi.voice import tts
from hakimi.prompt.tool_selection import select_names
from hakimi.models.fallback_prompt import fallback_prompt


class VoiceMediumTests(unittest.TestCase):
    def test_current_medium_is_independent_of_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base/'state').mkdir()
            (base/'voice_config.json').write_text(json.dumps({'enabled':True,'tts_enabled':True}))
            with tts.receipts(base) as db:
                db.execute('INSERT INTO receipts(session,filename,text,at) VALUES (?,?,?,?)',
                           ('group:123','prior.silk','上一次',time.time()))
            cases = [
                ('用语音', 'requested', True),
                ('猫猫，这回用语音给这一幕配个旁白', 'requested', True),
                ('请用语音把这几个名字串成一副对联', 'requested', True),
                ('把它倒过来，用语音', 'requested', True),
                ('这条语音挺好听', None, True),
                ('她说：“用语音把这几个名字串成一副对联”', None, True),
                ('我用语音输入', None, True),
                ('用语音想个结尾，但这次只用文字', None, False),
            ]
            for text, send, visible in cases:
                with self.subTest(text=text):
                    p = dict(session_key='group:123',trigger_id='t',reply_target_message_id='1',
                             recent_context_messages=[dict(user_id='9',message_id='1',text=text)])
                    self.assertEqual(tts.speech_mode(p,base), send)
                    self.assertEqual('voice.speak' in select_names(p,{'voice.speak','reply.send'},base), visible)
                    self.assertEqual('voice.speak(text≤80字)' in fallback_prompt(p,base), visible)
