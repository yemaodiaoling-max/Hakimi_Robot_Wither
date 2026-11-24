import http.cookiejar
import unittest
from unittest.mock import patch
from bili_companion.platform.bilibili import BilibiliPlatform, PlatformError, comment_hints


class PlatformTests(unittest.TestCase):
    def test_authenticated_comment_read_keeps_fixed_bounded_identity(self):
        with patch.object(self.platform,'_json',return_value=dict(code=0,data=dict(root=dict(rpid=10),replies=[]))) as request:
            self.platform.comment_thread(2,10,authenticated=True)
            request.assert_called_once_with('api.bilibili.com','/x/v2/reply/reply',dict(type=1,oid=2,root=10,ps=20,pn=1))

    def test_comment_missing_business_code_is_preserved(self):
        with patch.object(self.platform,'_json',return_value=dict(code=12006,data=None)):
            with self.assertRaises(PlatformError) as caught:self.platform.comment_thread(2,10,authenticated=True)
        self.assertEqual(caught.exception.business_code,12006)

    def setUp(self):
        self.platform = BilibiliPlatform("unused", clock=lambda: 100)

    def test_recommendations_and_live_session(self):
        with patch.object(self.platform, "_json", return_value=dict(code=0,data=dict(item=[dict(bvid="BV17pHP68EP4", title="x",duration=10),dict(bvid="ad")]))):
            self.assertEqual(len(self.platform.get_recommendations()), 1)
        body=dict(code=0,data=dict(room_id=8729095131, live_status=1,live_time="2026-11-01 12:00:00"))
        with patch.object(self.platform, "_json", return_value=body):
            state=self.platform.live_status("8729095131")
            self.assertTrue(state["is_live"])
            self.assertIn("2026-11-01",state["live_session_id"])

    def test_public_popular_metadata_and_bound(self):
        rows=[dict(bvid="BV17pHP68EP4",title="x",tid=31,tname="cover",duration=10)]*20
        with patch.object(self.platform,"_json",return_value=dict(code=0,data=dict(list=rows))) as request:
            result=self.platform.get_popular(6)
        self.assertEqual(len(result),6)
        self.assertEqual(result[0]['category_id'],31)
        self.assertEqual(result[0]['source'],'popular')
        self.assertEqual(request.call_args.args[1],'/x/web-interface/popular')

    def test_public_chat_keeps_no_viewer_identity_and_rejects_old_or_contact_text(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 11, 6, 12, 0, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.platform.clock = lambda: now
        rows = [dict(text='节奏好听', timeline='2026-11-06 11:59:55', uid=123, nickname='private'),
                dict(text='旧消息', timeline='2026-11-06 11:58:00'),
                dict(text='联系12345678', timeline='2026-11-06 11:59:55'),
                dict(text='https://example.test', timeline='2026-11-06 11:59:55')]
        with patch.object(self.platform, '_json', return_value=dict(code=0, data=dict(room=rows))) as request:
            result = self.platform.recent_live_danmaku('8729095131')
        self.assertEqual(result, [dict(text='节奏好听', observed_at=now-5)])
        self.assertEqual(request.call_args.args[1], '/xlive/web-room/v1/dM/gethistory')
        self.assertNotIn('post', request.call_args.kwargs)

    def test_invalid_room_and_missing_live_session(self):
        with self.assertRaises(PlatformError):
            self.platform.live_status("https://evil.test")
        with patch.object(self.platform,"_json",return_value=dict(code=0,data=dict(live_status=1))):
            with self.assertRaises(PlatformError):
                self.platform.live_status("1")

    def test_separate_audio_resolution(self):
        with patch.object(self.platform,"_extract",return_value=(dict(id="cid",duration=50),dict(url="https://x.bilivideo.com/v"),dict(url="https://x.bilivideo.com/a"))), patch.object(self.platform,"_json",return_value=dict(code=0,data=dict(bvid="BV17pHP68EP4",owner=dict(mid=123)))):
            target=self.platform.resolve_video("BV17pHP68EP4")
        self.assertEqual(target.audio_url,"https://x.bilivideo.com/a")
        self.assertEqual(target.uploader_id,"123")
        self.assertNotIn("https",repr(target))

    def test_send_no_retry_and_explicit_outcomes(self):
        jar=http.cookiejar.CookieJar()
        jar.set_cookie(http.cookiejar.Cookie(0,"bili_jct","synthetic",None,False,".bilibili.com",True,True,"/",True,True,None,True,None,None,{}))
        with patch("bili_companion.platform.bilibili.load_cookie_jar",return_value=(jar,"internal")):
            for response,status in ((dict(code=0,message=""),"sent"),(dict(code=-1),"rejected"),(dict(code=0,message="blocked"),"unknown")):
                with patch.object(self.platform,"_json",return_value=response) as send:
                    self.assertEqual(self.platform.send_danmaku("8729095131","合成测试").status,status)
                    self.assertEqual(send.call_count,1)
            with patch.object(self.platform,"_json",side_effect=TimeoutError()) as send:
                self.assertEqual(self.platform.send_danmaku("8729095131","合成测试").status,"unknown")
                self.assertEqual(send.call_count,1)

    def test_video_comment_resolves_archive_aid_and_never_replies(self):
        jar=http.cookiejar.CookieJar()
        jar.set_cookie(http.cookiejar.Cookie(0,"bili_jct","synthetic",None,False,".bilibili.com",True,True,"/",True,True,None,True,None,None,{}))
        metadata=dict(code=0,data=dict(bvid="BV17pHP68EP4",aid=123,duration=90,title="合成"))
        with patch("bili_companion.platform.bilibili.load_cookie_jar",return_value=(jar,"internal")),patch.object(self.platform,"_json",side_effect=[metadata,dict(code=0,data=dict(rpid=456,reply=dict(root=0,parent=0)))]) as network:
            result=self.platform.send_video_comment("BV17pHP68EP4","只针对实际看到的片段")
        self.assertEqual(result.status,"sent")
        self.assertEqual((result.rpid,result.oid),(456,123))
        self.assertEqual(network.call_count,2)
        first,second=network.call_args_list
        self.assertEqual(first.args,("api.bilibili.com","/x/web-interface/view",{"bvid":"BV17pHP68EP4"}))
        self.assertEqual(second.args,("api.bilibili.com","/x/v2/reply/add"))
        form=second.kwargs["post"]
        self.assertEqual(set(form),{"type","oid","message","plat","csrf"})
        self.assertEqual(form["oid"],123)
        self.assertEqual(form["type"],1)

    def test_video_comment_invalid_identity_never_posts(self):
        with patch.object(self.platform,"_json",return_value=dict(code=0,data=dict(bvid="BVxxxxxxxxxx",aid=1))) as network:
            result=self.platform.send_video_comment("BV17pHP68EP4","测试")
        self.assertEqual(result.status,"rejected")
        self.assertEqual(network.call_count,1)
        with patch.object(self.platform,"_json") as network:
            for invalid in ("https://evil.test",123,"av1"):
                self.assertEqual(self.platform.send_video_comment(invalid,"测试").status,"rejected")
            self.assertEqual(self.platform.send_video_comment("BV17pHP68EP4","x"*121).status,"rejected")
            network.assert_not_called()

    def test_video_comment_timeout_unknown_no_retry(self):
        jar=http.cookiejar.CookieJar()
        jar.set_cookie(http.cookiejar.Cookie(0,"bili_jct","synthetic",None,False,".bilibili.com",True,True,"/",True,True,None,True,None,None,{}))
        with patch("bili_companion.platform.bilibili.load_cookie_jar",return_value=(jar,"internal")),patch.object(self.platform,"get_video_info",return_value=dict(aid=123)),patch.object(self.platform,"_json",side_effect=TimeoutError()) as post:
            self.assertEqual(self.platform.send_video_comment("BV17pHP68EP4","测试").status,"unknown")
            self.assertEqual(post.call_count,1)

    def test_video_comment_preflight_preemption_cancels_before_post(self):
        jar=http.cookiejar.CookieJar()
        jar.set_cookie(http.cookiejar.Cookie(0,"bili_jct","synthetic",None,False,".bilibili.com",True,True,"/",True,True,None,True,None,None,{}))
        active=[True]
        def metadata(*args,**kwargs):
            active[0]=False  # Priority live arrived during the slow GET.
            return dict(code=0,data=dict(bvid="BV17pHP68EP4",aid=123))
        with patch("bili_companion.platform.bilibili.load_cookie_jar",return_value=(jar,"internal")),patch.object(self.platform,"_json",side_effect=metadata) as network:
            result=self.platform.send_video_comment("BV17pHP68EP4","测试",before_post=lambda:active[0])
        self.assertEqual((result.status,result.code),("rejected","cancelled_before_post"))
        self.assertEqual(network.call_count,1)  # Only GET, no external comment POST.

    def test_video_comment_rejection_and_ambiguous_success(self):
        jar=http.cookiejar.CookieJar()
        jar.set_cookie(http.cookiejar.Cookie(0,"bili_jct","synthetic",None,False,".bilibili.com",True,True,"/",True,True,None,True,None,None,{}))
        with patch("bili_companion.platform.bilibili.load_cookie_jar",return_value=(jar,"internal")),patch.object(self.platform,"get_video_info",return_value=dict(aid=123)):
            for response,expected in ((dict(code=12015),"rejected"),(dict(code=0,data={}),"unknown"),(dict(code=0,data=dict(rpid=1,reply=dict(root=2))),"unknown")):
                with patch.object(self.platform,"_json",return_value=response):
                    self.assertEqual(self.platform.send_video_comment("BV17pHP68EP4","测试").status,expected)

    def test_numeric_review_states_are_diagnostics_without_visibility_claim(self):
        for state in (0,17,-1):
            self.assertEqual(comment_hints(dict(reply=dict(state=state))),('',state))
        for state in (True,'17',2**40):
            self.assertEqual(comment_hints(dict(reply=dict(state=state))),('',None))


if __name__ == "__main__":
    unittest.main()
