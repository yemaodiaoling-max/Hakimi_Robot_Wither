"""Only synthetic actors and an AsyncMock API; never sends a QQ message."""
import html
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock,Mock,patch
from datetime import datetime,timezone
from hakimi.chat.member_mentions import wire_text
from hakimi.transport.official_transport import OfficialAdapter
from hakimi.transport.official_routes import RouteStore
from hakimi.transport.official_api import OfficialAPI
from hakimi.chat.reply_format import text_bubbles
from test_delivery_guard import OfflineEventLoop


class OfficialMentionContractTests(unittest.IsolatedAsyncioTestCase):
    loop_factory=OfflineEventLoop
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);(self.base/'state').mkdir()
        self.now=time.time()
        self.store=RouteStore(self.base/'state/official_routes.sqlite3',clock=lambda:self.now)
        self.store.set('group_openid','FIXTURE_GROUP')
        self.api=AsyncMock();self.api.call.return_value={'id':'FIXTURE_ACK'}
        self.adapter=OfficialAdapter(self.base,{'test_group_number':'300','allow_proactive':False},self.api,self.store,clock=lambda:self.now)
        self.adapter.self_remote='SELF_FIXTURE'

    def queue(self,text):
        source,_=self.store.remember('FIXTURE_SOURCE','REF',self.now,group='FIXTURE_GROUP',user_id='1000000000000000009')
        self.store.attach_turn('turn',source)
        pending={'trigger_id':'turn','reply_id':'reply','session_key':'group:300','next_segment':0,'segments':[text]}
        (self.base/'state/delivery_state.json').write_text(json.dumps({'pending':pending}),encoding='utf-8')

    async def test_canonical_local_mention_maps_human_or_bot_and_keeps_unicode(self):
        for index,remote in enumerate(['HUMAN_FIXTURE_OPENID','BOT_FIXTURE_OPENID']):
            uid=self.store.actor(remote)
            body_text=text_bubbles('你好，包裹到了 & [备注]')[0]
            segment=f'[CQ:at,qq={uid}] '+body_text
            self.queue(segment)
            # Each fixture iteration is an independent delivery authority.
            state=json.loads((self.base/'state/delivery_state.json').read_text())
            state['pending']['reply_id']='reply'+str(index)
            (self.base/'state/delivery_state.json').write_text(json.dumps(state),encoding='utf-8')
            result=await self.adapter.send('group:300',segment)
            self.assertEqual(result['status'],'success')
            body=self.api.call.call_args.args[2]
            self.assertEqual(body['msg_type'],2)
            self.assertEqual(body['markdown']['content'],f'<qqbot-at-user id="{remote}" /> 你好，包裹到了 & [备注]')
            self.assertNotIn(uid,body['markdown']['content'])

    async def test_verified_native_official_tag_remains_compatible(self):
        text='<qqbot-at-user id="HUMAN_FIXTURE_OPENID" /> 你好'
        self.queue(text)
        self.assertEqual((await self.adapter.send('group:300',text))['status'],'success')
        self.assertEqual(self.api.call.call_args.args[2]['markdown']['content'],text)

    async def test_literal_escaped_cq_stays_plain_and_never_activates_an_at(self):
        uid=self.store.actor('HUMAN_FIXTURE_OPENID')
        text=text_bubbles(f'[CQ:at,qq={uid}] 这是格式示例')[0]
        self.queue(text)
        self.assertEqual((await self.adapter.send('group:300',text))['status'],'success')
        body=self.api.call.call_args.args[2]
        self.assertEqual(body['msg_type'],0)
        self.assertEqual(body['content'],html.unescape(text))
        self.assertNotIn('markdown',body)

    async def test_unknown_alias_does_not_post_or_guess_a_remote_id(self):
        text='[CQ:at,qq=1000000000000000001] 你好'
        self.queue(text)
        with patch('hakimi.features.codex_help.report_fault'):
            self.assertNotEqual((await self.adapter.send('group:300',text))['status'],'success')
        self.api.call.assert_not_called()

    async def test_human_and_bot_events_prefer_member_openid_over_other_id(self):
        for index,is_bot in enumerate([False,True]):
            member='BOT_MEMBER_FIXTURE' if is_bot else 'HUMAN_MEMBER_FIXTURE'
            data={'id':'INPUT'+str(index),'group_openid':'FIXTURE_GROUP','content':'hello',
                  'author':{'id':'DIFFERENT_ACCOUNT_'+str(index),'member_openid':member,'bot':is_bot,'username':'样例成员'},
                  'timestamp':datetime.fromtimestamp(self.now,timezone.utc).isoformat()}
            with patch('hakimi.chat.bot_registry.platform'):
                event=await self.adapter.translate('GROUP_MESSAGE_CREATE',data)
            self.assertEqual(str(event['user_id']),self.store.actor(member))
            self.assertNotEqual(str(event['user_id']),self.store.actor(data['author']['id']))
            self.assertIn(member,wire_text(f"[CQ:at,qq={event['user_id']}]",self.store))


class OfficialJsonEncodingTests(unittest.TestCase):
    def test_unicode_payload_is_lossless_json_bytes(self):
        api=OfficialAPI('fixture-app','fixture-secret')
        response=Mock();response.status=200;response.read.return_value=b'{"id":"ACK"}'
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        api.opener=Mock();api.opener.open.return_value=response
        body={'msg_type':2,'markdown':{'content':'<qqbot-at-user id="HUMAN_FIXTURE_OPENID" /> 星光与包裹 & [备注]'}}
        self.assertEqual(api._request('POST','/v2/groups/FIXTURE/messages',body),{'id':'ACK'})
        request=api.opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data.decode('utf-8')),body)


if __name__=='__main__':unittest.main()
