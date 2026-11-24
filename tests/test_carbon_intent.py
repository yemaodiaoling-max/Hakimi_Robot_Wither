import json
import tempfile
import unittest
from pathlib import Path
from hakimi.models.task_routing import ARGON, CARBON, FLASH, select


class CarbonIntent(unittest.TestCase):
    def test_current_request_and_quoted_subject(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'task_routing.json').write_text(json.dumps(dict(mode='dual', single_model=ARGON, dual_model=CARBON)))
            old = dict(message_id='11', user_id='a', text='我们在说拓扑超导')
            for text, ref, expected in [
                    ('请用Carbon回答', '', CARBON), ('不用Carbon，请帮我证明这条定理', '', FLASH),
                    ('你试着提出一个全新的高温超导理论', '', CARBON),
                    ('3代至强为什么有两种制程，区别呢？', '', CARBON),
                    ('从计算上讲讲拓扑超导可能的好处？', '', CARBON),
                    ('帮我分析这段发言', '', CARBON), ('那为什么呢', '11', CARBON),
                    ('那为什么呢', '', FLASH), ('你怎么又来了', '', FLASH),
                    ('Carbon上线了', '', FLASH), ('帮我查天气', '', FLASH)]:
                with self.subTest(text=text,ref=ref):
                    current = dict(message_id='12',user_id='b',text=text,reply_to=ref)
                    payload = dict(session_key='group:123',reply_target_message_id='12',recent_context_messages=[old,current])
                    self.assertEqual(select(root,payload,ARGON)[0],expected)
