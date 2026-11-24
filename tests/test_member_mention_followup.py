import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from hakimi.transport import member_mentions as mentions
from hakimi.transport.official_routes import RouteStore


class MemberMentionFollowupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);(self.base/'state').mkdir()
        self.coordinator=Mock();self.coordinator.check_token.return_value=True
        self.coordinator.snapshot.return_value={'reply_id':None,'payload':{'session_key':'group:300','recent_context_messages':[]}}
        self.coordinator.select_mention.return_value={'accepted':True}
        self.coordinator.submit_reply.return_value={'accepted':True}
        self.member={'ok':True,'member':{'user_id':'1000000000000000007','nickname':'样例成员'}}

    def invoke(self,text=''):
        return mentions.mention(self.base,self.coordinator,'owner','gemini','样例成员',text)

    def test_lookup_selects_without_sending_or_prompting_model_to_copy_protocol(self):
        with patch.object(mentions,'resolve',return_value=self.member):result=self.invoke()
        self.coordinator.select_mention.assert_called_once_with('owner','gemini','1000000000000000007')
        self.coordinator.submit_reply.assert_not_called()
        self.assertTrue(result['ok']);self.assertFalse(result['sent'])
        self.assertEqual(result['user_id'],self.member['member']['user_id'])
        self.assertNotIn('mention_code',result)
        self.assertNotIn('[CQ:',result['hint']);self.assertNotIn(result['user_id'],result['hint'])
        self.assertEqual(mentions.mention_status(result),'selected')

    def test_complete_request_passes_target_separately_from_escaped_body(self):
        with patch.object(mentions,'resolve',return_value=self.member):result=self.invoke('你好 & [正文]')
        args=self.coordinator.submit_reply.call_args
        self.assertEqual(args.kwargs,{'mention_user':'1000000000000000007'})
        self.assertNotIn('[CQ:at',args.args[1][0])
        self.assertIn('&#91;正文&#93;',args.args[1][0])
        self.coordinator.select_mention.assert_not_called()
        self.assertTrue(result['sent']);self.assertEqual(mentions.mention_status(result),'queued')

    def test_unresolved_or_ambiguous_new_lookup_clears_old_target(self):
        for failed,status in [({'ok':False,'error':'本群尚未找到这个人'},'not_found'),({'ok':False,'error':'请选择成员编号','candidates':[{'user_id':'1000000000000000008'}]},'ambiguous')]:
            self.coordinator.select_mention.reset_mock()
            with patch.object(mentions,'resolve',return_value=failed):result=self.invoke()
            self.coordinator.select_mention.assert_called_once_with('owner','gemini',None)
            self.assertEqual(result,failed);self.assertEqual(mentions.mention_status(result),status)
            self.coordinator.submit_reply.assert_not_called()

    def test_rejected_owner_cannot_select_or_send(self):
        self.coordinator.check_token.return_value=False
        with patch.object(mentions,'resolve') as resolve:result=self.invoke()
        resolve.assert_not_called();self.coordinator.select_mention.assert_not_called()
        self.assertEqual(mentions.mention_status(result),'rejected')
        self.coordinator.check_token.return_value=True
        self.coordinator.select_mention.return_value={'accepted':False,'reason':'turn_expired'}
        with patch.object(mentions,'resolve',return_value=self.member):result=self.invoke()
        self.assertEqual(result,{'ok':False,'error':'turn_expired'})

    def test_known_id_is_exact_same_group_and_not_an_alias(self):
        p={'session_key':'group:300','recent_context_messages':[{'session_key':'group:300','user_id':'1000000000000000007','nickname':'1000000000000000008'}]}
        self.assertTrue(mentions.known_local_member(self.base,p,'1000000000000000007'))
        self.assertFalse(mentions.known_local_member(self.base,p,'1000000000000000008'))
        p['recent_context_messages'][0]['session_key']='group:301'
        self.assertFalse(mentions.known_local_member(self.base,p,'1000000000000000007'))
        self.assertFalse(mentions.known_local_member(self.base,p,'9000000000000000001'))
        self.assertFalse(mentions.known_local_member(self.base,{'session_key':'private:7'},'1000000000000000007'))

    def test_known_id_memory_and_routes_do_not_leak_global_actor_membership(self):
        p={'session_key':'group:300'};uid='1000000000000000007'
        with sqlite3.connect(self.base/'state/memory.sqlite3') as db:
            db.execute('CREATE TABLE records(session,user_id,kind,state)')
            db.execute('INSERT INTO records VALUES (?,?,?,?)',('group:301',uid,'chat','active'))
            db.execute('INSERT INTO records VALUES (?,?,?,?)',('group:300',uid,'person','active'))
        self.assertFalse(mentions.known_local_member(self.base,p,uid))
        with sqlite3.connect(self.base/'state/memory.sqlite3') as db:
            db.execute('INSERT INTO records VALUES (?,?,?,?)',('group:300',uid,'chat','active'))
        self.assertTrue(mentions.known_local_member(self.base,p,uid))
        with sqlite3.connect(self.base/'state/memory.sqlite3') as db:db.execute("UPDATE records SET state='deleted'")
        store=RouteStore(self.base/'state/official_routes.sqlite3',clock=lambda:2.)
        actor=store.actor('FIXTURE_MEMBER')
        store.set('group_routes',{'G':{'openid':'G','local_id':'300','active':True}})
        self.assertFalse(mentions.known_local_member(self.base,p,actor))
        store.remember('IN_OTHER',None,1,group='OTHER',user_id=actor)
        self.assertFalse(mentions.known_local_member(self.base,p,actor))
        store.remember('IN_GROUP',None,2,group='G',user_id=actor)
        self.assertTrue(mentions.known_local_member(self.base,p,actor))


if __name__=='__main__':unittest.main()
