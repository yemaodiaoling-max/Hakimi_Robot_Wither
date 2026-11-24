import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from hakimi.tools import qq_video
from hakimi.prompt.tool_selection import select_names
from hakimi.models import dsh_tools
from hakimi.models.vertex_session import VertexSession
import test_official_adapter as official_tests


class VideoEventTests(unittest.IsolatedAsyncioTestCase):
    loop_factory = official_tests.OfflineEventLoop
    setUp = official_tests.AdapterTests.setUp
    data = official_tests.AdapterTests.data

    async def test_video_only_event_keeps_scoped_reference(self):
        event=await self.adapter.translate('GROUP_MESSAGE_CREATE',self.data(content='',attachments=[
            {'content_type':'application/octet-stream','filename':'clip.mp4','url':'https://video.qq.com/private?sign=secret'}]))
        self.assertIn('video.watch_attachment ref='+str(event['message_id'])+'-0',event['raw_message'])
        self.assertNotIn('sign=secret',event['raw_message'])


class QQVideoTests(unittest.TestCase):
    def test_visible_attachment_on_both_routes_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            (base/'state').mkdir()
            url='https://video.qq.com/download?secret=test'
            text=qq_video.register(base,'group:123','17',[{'content_type':'video/mp4','url':url}])
            self.assertNotIn(url,text)
            payload=dict(session_key='group:123',reply_target_message_id='17',recent_context_messages=[
                dict(session_key='group:123',message_id='17',user_id='6',text=text)])
            tool='video.watch_attachment'
            self.assertIn(tool,select_names(payload,{tool},base))
            claim={'payload':payload,'token':'owner','deadline':9999999999}
            args={'ref':'17-0'}
            response={'ok':True,'overview':'测试画面','observed_seconds':3}
            with patch('hakimi.tools.qq_video.subprocess.run',return_value=Mock(stdout=json.dumps(response))) as worker:
                self.assertEqual(VertexSession([],base,{})._tool(tool,args,claim,40),response)
                self.assertEqual(dsh_tools.execute({'tool':tool,'input':args},claim,Mock(),base),response)
                worker.assert_called_once()
            self.assertFalse(qq_video.watch(base,dict(payload,session_key='group:234'),'17-0')['ok'])
            self.assertFalse(qq_video.watch(base,payload,'18-0')['ok'])

    def test_text_and_unapproved_attachments_do_not_authorize_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            note=qq_video.register(base,'group:123','17',[{'content_type':'video/mp4','url':'http://localhost/private'}])
            self.assertNotIn('ref=',note)
            self.assertFalse((base/'state/qq_video.sqlite3').exists())
