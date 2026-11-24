import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from hakimi.bridge.interaction_policy import delivered
from hakimi.transport.sent_media_context import enrich_sent_rows, recalled_sticker


class SentMediaContextTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        (self.base/'state').mkdir()
        self.file = self.base/'stickers/collected/sandrone_proud.jpg'
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(b'fixture, not decoded')
        self.session = 'group:100'
        delivered(self.session,'receipt:3',None,False,self.base,now=1000,
                  text=f'[CQ:image,file={self.file.as_uri()},sub_type=1]',provider='gemini',
                  trigger_id='turn',message_id='42',sticker_digest=hashlib.sha256(self.file.read_bytes()).hexdigest())

    def query(self,text='上一张图是什么',reply='',session=None,at=1040):
        session = session or self.session
        row = dict(session_key=session,user_id='9',message_id='55',received_at=at,text=text,reply_to=reply)
        return dict(session_key=session,created_at=at,recent_context_messages=[row],reply_target_message_id='55')

    def test_receipt_label_and_quoted_image_are_exact(self):
        receipt=json.loads((self.base/'state/sent_messages.jsonl').read_text())
        self.assertEqual(receipt['text'],'')
        self.assertEqual(receipt['media']['asset'],'collected/sandrone_proud.jpg')
        self.assertIn('sandrone_proud.jpg',enrich_sent_rows([receipt],self.base)[0]['text'])
        self.assertEqual(recalled_sticker(self.query('这是啥',reply='42'),self.base,1040),('42',self.file))
        self.assertEqual(recalled_sticker(self.query(),self.base,1040),('42',self.file))

    def test_unrelated_questions_and_cross_session_quotes(self):
        for query in [self.query('晚上好'),self.query(reply='99'),self.query(reply='42',session='private:100'),
                      self.query(reply='42',at=999),self.query(reply='42',at=5000)]:
            self.assertIsNone(recalled_sticker(query,self.base,5000))

    def test_replaced_asset_is_not_the_sent_image(self):
        self.file.write_bytes(b'different data')
        self.assertIsNone(recalled_sticker(self.query(reply='42'),self.base,1040))

    def test_new_text_reply_ends_implicit_image_followup(self):
        delivered(self.session,'receipt:4',None,False,self.base,now=1010,text='收到',message_id='43')
        self.assertIsNone(recalled_sticker(self.query(),self.base,1040))
        self.assertEqual(recalled_sticker(self.query(reply='42'),self.base,1040),('42',self.file))
