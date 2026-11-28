import json
import unittest
from pathlib import Path

from hakimi.bridge.conversation_judge import decode_messages, select_messages, text_batch


class ConversationAudienceTests(unittest.TestCase):
    def test_reference_outside_window_resolves_author(self):
        rows=[{'message_id':'3','user_id':'alice','text':'那一步这样算吗','reply_to':'1'}]
        batch=text_batch(rows,context=[{'message_id':'1','user_id':'bob','text':'我的解法'}])
        self.assertEqual((batch[0]['audience'],batch[0]['reply_author']),('other','bob'))

    def test_reference_to_hakimi_uses_confirmed_sent_ids(self):
        rows=[{'message_id':'3','user_id':'alice','text':'再讲讲这一步','reply_to':'2'}]
        batch=text_batch(rows,own_message_ids=['2'])
        self.assertEqual((batch[0]['audience'],batch[0]['reply_author']),('hakimi','SELF'))

    def test_at_other_overrides_quoted_hakimi(self):
        rows=[{'message_id':'3','user_id':'alice','text':'你看她这样讲对吗','reply_to':'2','direct_targets':['bob']}]
        batch=text_batch(rows,own_message_ids=['2'])
        self.assertEqual(batch[0]['audience'],'other')
        self.assertEqual(batch[0]['mentions'],['bob'])

    def test_current_mention_metadata_wins_over_quoted_mentions(self):
        rows=[{'message_id':'3','user_id':'alice','text':'[引用 [CQ:at,qq=9000000000000000001]] 请看这里',
               'direct_targets':['bob']}]
        self.assertEqual(text_batch(rows)[0]['audience'],'other')

    def test_self_alias_is_clear_to_reviewer(self):
        rows=[{'message_id':'3','user_id':'alice','text':'猫猫你怎么看','self_id':'999','direct_targets':['999']}]
        row=text_batch(rows)[0]
        self.assertEqual((row['audience'],row['mentions']),('hakimi',['SELF']))

    def test_selection_remains_a_dsh_decision(self):
        rows=[{'message_id':'3','user_id':'alice','text':'@bob 猫猫也来一起看看','direct_targets':['bob']}]
        seen=[]
        def runner(task,base):
            data=json.loads(task);seen.append(data);data['decoded']=decode_messages(data)
            return {'selected_message_ids':['3']}
        self.assertEqual(select_messages(rows,'上一条回复',Path('.'),runner=runner),['3'])
        self.assertEqual(seen[0]['decoded'][0]['audience'],'other')
        self.assertIn('明确@对象优先于引用作者',seen[0]['task'])
        self.assertEqual(seen[0]['previous_reply'],'上一条回复')


if __name__=='__main__':unittest.main()
