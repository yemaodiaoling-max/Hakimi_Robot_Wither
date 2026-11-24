from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from hakimi.models.search_policy import network_allowed,decide


class SearchPolicyTests(unittest.TestCase):
    def payload(self,text):
        return {'session_key':'group:123','reply_target_message_id':'2','recent_context_messages':[
            {'session_key':'group:123','message_id':'2','user_id':'7','text':text}]}

    def test_common_offline_requests_win_before_judge(self):
        for text in ['不准搜，今天的新模型如何','仅凭记忆回答','别搜','不要查','凭记忆回答','离线回答','别联网，查一下最新新闻']:
            payload=self.payload(text);judge=Mock(return_value={'search':True})
            self.assertFalse(network_allowed(payload),text)
            self.assertEqual(decide(payload,Path('.'),judge=judge)['reason'],'network_forbidden')
            judge.assert_not_called()

    def test_explicit_search_bypasses_negative_or_cold_classifier(self):
        for text in ['搜一下量子纠错进展','帮我搜最新公告','搜原神版本','请搜索最新模型']:
            judge=Mock(return_value={'search':False})
            value=decide(self.payload(text),Path('.'),judge=judge)
            self.assertTrue(value['search'],text);self.assertEqual(value['reason'],'explicit_search')
            judge.assert_not_called()

    def test_same_author_pending_split_request_carries_its_ban(self):
        payload=self.payload('今天Google有什么量子纠错新消息？')
        payload['recent_context_messages'].insert(0,{'session_key':'group:123','message_id':'1','user_id':'7','text':'@哈基米 不准搜'})
        payload['pending_explicit_question_ids']=['1','2']
        self.assertFalse(network_allowed(payload))
        payload['recent_context_messages'][0]['user_id']='8'
        self.assertTrue(network_allowed(payload))

    def test_quoted_own_instruction_binds_but_other_author_quote_does_not(self):
        payload=self.payload('回答这个')
        payload['recent_context_messages'][0]['reply_to']='1'
        payload['recent_context_messages'].insert(0,{'message_id':'1','user_id':'7','text':'离线回答这次问题'})
        self.assertFalse(network_allowed(payload))
        payload['recent_context_messages'][0]['user_id']='8'
        self.assertTrue(network_allowed(payload))


if __name__=='__main__':unittest.main()
