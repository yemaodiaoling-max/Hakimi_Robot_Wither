"""Community website API adapter, not Bilibili's creator open-platform API.

Read paths and extractor formats follow yt-dlp's Bilibili extractor. Sending
uses the website's msg/send route; no retry, no public API stability promise.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import http.cookiejar
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import urllib.error
import urllib.parse
import urllib.request

from .auth import AuthError, _NoRedirect, load_cookie_jar


class PlatformError(Exception):
    def __init__(self, code, *, features=()):
        self.code = str(code)
        self.features = tuple(features)
        super().__init__(self.code)


def extractor_error_features(stderr):
    """Only fixed diagnostic flags leave this boundary; no upstream text or URLs."""
    text = stderr[:32768].decode('utf-8',errors='replace').lower()
    checks = (
        ('http_403',('http error 403','403 forbidden')),
        ('http_404',('http error 404','404 not found')),
        ('http_412',('http error 412','412 precondition')),
        ('risk_control',('-412','risk control','request is blocked')),
        ('login_required',('login required','log in','not logged in','premium-only','members-only')),
        ('video_unavailable',('video unavailable','video is unavailable','this video is not available','video has been deleted','视频不存在')),
        ('format_unavailable',('requested format is not available',)),
        ('impersonation_unavailable',('impersonate target','impersonation target','curl_cffi','impersonation is not available')),
        ('network_timeout',('timed out','timeout','connection reset','network is unreachable')),
        ('unsupported_url',('unsupported url','no suitable extractor')),
    )
    return tuple(name for name,needles in checks if any(needle in text for needle in needles)) or ('extractor_failed',)


@dataclass(frozen=True)
class MediaTarget:
    url: str = field(repr=False)
    headers: dict = field(repr=False)
    identity: str
    session_id: str
    is_live: bool
    duration: float | None = None
    start_offset: float = 0
    audio_url: str | None = field(default=None, repr=False)
    title: str = ""
    uploader_id: str = ""


@dataclass(frozen=True)
class SendResult:
    status: str  # sent / rejected / unknown
    code: str = ""
    rpid: int | None = None
    oid: int | None = None
    notice: str = ''
    review_state: int | None = None


def comment_hints(data):
    """Only known public UI toasts and numeric review state leave the API boundary."""
    notice=data.get('success_toast','')
    if notice not in {'发送成功','评论发送成功','评论已发送','评论成功','评论审核中','审核中'}:notice=''
    state=(data.get('reply') or {}).get('state',data.get('state'))
    return notice,state if type(state) is int and -(2**31)<=state<2**31 else None


def _room(value):
    value = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,15}", value):
        raise PlatformError("invalid_room")
    return value


def _media_url(value):
    # The fixed Bilibili extractor supplies CDN URLs. Its upstream can rotate
    # among CDN providers; user-facing video/room inputs remain Bilibili-only.
    parsed = urllib.parse.urlsplit(str(value))
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or not parsed.hostname):
        raise PlatformError("untrusted_media_host")
    return value


# "480p" bounds the short side, not always height: portrait 360x640 and
# 480x854 streams are normal low-resolution DASH video. Choose within that
# bound first, then the smallest available DASH stream before progressive-only.
VOD_FORMAT_SELECTOR = (
    'bv*[height<=480][vcodec^=avc]+ba/bv*[width<=480][vcodec^=avc]+ba/'
    'b[height<=480]/b[width<=480]/bv*[height<=480]+ba/bv*[width<=480]+ba/wv+ba/b'
)


class BilibiliPlatform:
    def __init__(self, auth_state_dir, *, python_executable=sys.executable, clock=time.time):
        self.auth_state_dir = Path(auth_state_dir)
        self.python = str(python_executable)
        self.clock = clock

    def _json(self, host, path, params=None, *, post=None):
        if host not in {"api.bilibili.com", "api.live.bilibili.com"} or not path.startswith("/"):
            raise PlatformError("invalid_endpoint")
        jar, _ = load_cookie_jar(self.auth_state_dir, include_csrf=post is not None)
        opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPCookieProcessor(jar))
        url = "https://" + host + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = None if post is None else urllib.parse.urlencode(post).encode()
        request = urllib.request.Request(url, data=body, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://live.bilibili.com/" if host.startswith("api.live") else "https://www.bilibili.com/"})
        try:
            with opener.open(request, timeout=12) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise PlatformError("response_too_large")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise PlatformError("invalid_response")
            return value
        except AuthError:
            raise
        except PlatformError:
            raise
        except Exception:
            raise PlatformError("network") from None

    def get_recommendations(self, limit=6):
        limit = max(1, min(20, int(limit)))
        data = self._json("api.bilibili.com", "/x/web-interface/index/top/rcmd", {"fresh_type": 3, "ps": limit})
        if data.get("code") != 0:
            raise PlatformError("recommendations_unavailable")
        result = []
        for item in (data.get("data", {}).get("item") or []):
            if re.fullmatch(r"BV[A-Za-z0-9]{10}", str(item.get("bvid", ""))):
                result.append(dict(bvid=item["bvid"], title=str(item.get("title", ""))[:200],
                                   duration=float(item.get("duration") or 0), source="recommendations",
                                   category=str(item.get("tname", ""))[:80], category_id=item.get("tid")))
            if len(result) >= limit:
                break
        return result

    def get_popular(self, limit=20, page=1):
        """Public, non-personalized pool; one bounded read request."""
        limit = max(1, min(20, int(limit)))
        data = self._json("api.bilibili.com", "/x/web-interface/popular", {"pn": max(1,min(3,int(page))), "ps": 20})
        if data.get("code") != 0:
            raise PlatformError("popular_unavailable")
        return [dict(bvid=i["bvid"], title=str(i.get("title", ""))[:200],
                     duration=float(i.get("duration") or 0), source="popular",
                     category=str(i.get("tname", ""))[:80], category_id=i.get("tid"))
                for i in (data.get("data", {}).get("list") or [])
                if re.fullmatch(r"BV[A-Za-z0-9]{10}", str(i.get("bvid", "")))][:limit]

    def recent_live_danmaku(self, room, *, limit=6):
        """One website history read; return recent text, never viewer identities.

        This is a public website route, not an official open-platform contract.
        History can span sessions: reject old/future or undated entries.
        """
        room = _room(room)
        body = self._json("api.live.bilibili.com", "/xlive/web-room/v1/dM/gethistory", {"roomid": room})
        data = body.get('data')
        if body.get('code') != 0 or type(data) is not dict or type(data.get('room')) is not list:
            raise PlatformError('live_chat_unavailable')
        now, rows, seen = self.clock(), [], set()
        for item in data['room'][-20:]:
            if type(item) is not dict or type(item.get('text')) is not str:
                continue
            text = item['text'].strip()
            # Drop contact details, links and mentions rather than carrying them
            # into prompts, retained evidence or public replies.
            if (not 1 <= len(text) <= 100 or any(ord(c) < 32 for c in text)
                    or re.search(r'https?://|www\.|@|[0-9]{5,}|[\w.+-]+@[\w.-]+', text, re.I)):
                continue
            try:
                at = datetime.strptime(item.get('timeline', ''), '%Y-%m-%d %H:%M:%S').replace(
                    tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
            except (ValueError, TypeError):
                continue
            key = (text, at)
            if not 0 <= now-at <= 45 or key in seen:
                continue
            seen.add(key)
            rows.append(dict(text=text, observed_at=at))
        return sorted(rows, key=lambda row: row['observed_at'])[-max(1, min(6, int(limit))):]

    def live_status(self, room):
        room = _room(room)
        body = self._json("api.live.bilibili.com", "/room/v1/Room/get_info", {"room_id": room})
        if body.get("code") != 0 or not isinstance(body.get("data"), dict):
            raise PlatformError("live_status_unavailable")
        data = body["data"]
        state = data.get("live_status")
        if state not in (0, 1, 2):
            raise PlatformError("live_status_unknown")
        canonical_room = _room(data.get("room_id") or room)
        start = str(data.get("live_time") or "")
        if state == 1 and start in {"", "0000-00-00 00:00:00", "0"}:
            raise PlatformError("live_session_unknown")
        return dict(room_id=canonical_room, is_live=state == 1, live_session_id=canonical_room+":"+start,
                    title=str(data.get("title", ""))[:200], checked_at=self.clock())

    @contextmanager
    def _cookie_file(self):
        jar, _ = load_cookie_jar(self.auth_state_dir)
        self.auth_state_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".extract-", suffix=".txt", dir=self.auth_state_dir)
        os.close(fd)
        try:
            saved = http.cookiejar.MozillaCookieJar(name)
            for cookie in jar:
                saved.set_cookie(cookie)
            saved.save(ignore_discard=True, ignore_expires=False)
            os.chmod(name, 0o600)
            yield name
        finally:
            Path(name).unlink(missing_ok=True)

    def _extract(self, url, *, live=False):
        selector = "worst[acodec!=none][vcodec!=none]/worst" if live else VOD_FORMAT_SELECTOR
        with self._cookie_file() as cookie_file:
            args = [self.python, "-m", "yt_dlp", "--dump-single-json", "--no-playlist", "--no-warnings",
                    "--quiet", "--socket-timeout", "8", "--retries", "0", "--cookies", cookie_file,
                    "--impersonate", "chrome", "-f", selector, url]
            try:
                proc = subprocess.run(args, capture_output=True, timeout=40,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, subprocess.TimeoutExpired):
                raise PlatformError("extractor_timeout") from None
        if proc.returncode or len(proc.stdout) > 8_000_000:
            raise PlatformError("media_unavailable",features=(('response_oversized',)
                if len(proc.stdout) > 8_000_000 else extractor_error_features(proc.stderr)))
        try:
            info = json.loads(proc.stdout)
            formats = info.get("requested_formats") or [info]
            video = next((f for f in formats if f.get("vcodec") != "none" and f.get("url")), None)
            audio = next((f for f in formats if f.get("vcodec") == "none" and f.get("url")), None)
            if video is None:
                raise PlatformError("video_unavailable")
            return info, video, audio
        except (ValueError, TypeError, StopIteration):
            raise PlatformError("invalid_media_metadata") from None

    def resolve_video(self, bvid, *, page=1):
        if not re.fullmatch(r"BV[A-Za-z0-9]{10}", str(bvid)):
            raise PlatformError("invalid_bvid")
        if type(page) is not int or not 1 <= page <= 1000:
            raise PlatformError("invalid_video_part")
        info, video, audio = self._extract("https://www.bilibili.com/video/"+bvid+("?p="+str(page) if page != 1 else ""))
        if info.get("is_live"):
            raise PlatformError("unexpected_live")
        uploader_id=''
        try:
            metadata=self._json('api.bilibili.com','/x/web-interface/view',{'bvid':bvid})
            if metadata.get('code')==0 and metadata.get('data',{}).get('bvid')==bvid:
                mid=(metadata.get('data',{}).get('owner') or {}).get('mid')
                if str(mid).isdigit() and int(mid)>0:uploader_id=str(mid)
        except (PlatformError,AuthError,AttributeError,TypeError,ValueError):
            pass
        headers = {"Referer": "https://www.bilibili.com/", "User-Agent": "Mozilla/5.0"}
        return MediaTarget(_media_url(video["url"]), headers, bvid, bvid+":"+str(info.get("id", "")), False,
                           float(info.get("duration") or 0), audio_url=_media_url(audio["url"]) if audio else None,
                           title=str(info.get("title", ""))[:200],uploader_id=uploader_id)

    def resolve_live(self, room):
        state = self.live_status(room)
        if not state["is_live"]:
            raise PlatformError("not_live")
        info, video, audio = self._extract("https://live.bilibili.com/"+state["room_id"], live=True)
        return MediaTarget(_media_url(video["url"]), {"Referer": "https://live.bilibili.com/", "User-Agent": "Mozilla/5.0"},
                           state["room_id"], state["live_session_id"], True, audio_url=_media_url(audio["url"]) if audio else None,
                           title=state["title"])

    def send_danmaku(self, room, text):
        room = _room(room)
        if not isinstance(text, str) or not text.strip() or len(text) > 40 or any(ord(c) < 32 for c in text):
            return SendResult("rejected", "invalid_text")
        try:
            jar, _ = load_cookie_jar(self.auth_state_dir, include_csrf=True)
            values = {c.value for c in jar if c.name == "bili_jct" and c.domain.lstrip(".") == "bilibili.com"}
            if len(values) != 1:
                return SendResult("rejected", "csrf_unavailable")
            csrf = next(iter(values))
            result = self._json("api.live.bilibili.com", "/msg/send", post=dict(
                roomid=room, msg=text, rnd=int(self.clock()), fontsize=25, color=16777215,
                mode=1, bubble=0, csrf=csrf, csrf_token=csrf))
            code = result.get("code")
            if code == 0 and not result.get("message"):
                return SendResult("sent", "accepted")
            if type(code) is int and code != 0:
                return SendResult("rejected", str(code))
            return SendResult("unknown", "ambiguous_response")
        except AuthError as exc:
            return SendResult("rejected", exc.code)
        except Exception:
            return SendResult("unknown", "transport_or_response")

    def get_video_info(self, bvid):
        """Resolve the video's own aid from fixed website metadata, never a model.

        reply/add's type=1 oid is the archive aid, not the current part's cid.
        Community source: biliup/biliup crates/biliup/src/uploader/bilibili.rs.
        """
        if type(bvid) is not str or not re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid):
            raise PlatformError("invalid_bvid")
        body = self._json("api.bilibili.com", "/x/web-interface/view", {"bvid": bvid})
        data = body.get("data")
        if body.get("code") != 0 or type(data) is not dict:
            raise PlatformError("video_metadata_unavailable")
        aid = data.get("aid")
        if data.get("bvid") != bvid or type(aid) is not int or not 0 < aid <= 2**53:
            raise PlatformError("video_identity_mismatch")
        return dict(bvid=bvid, aid=aid, title=str(data.get("title", ""))[:200],
                    duration=data.get("duration"))

    def authenticated_uid(self):
        body=self._json('api.bilibili.com','/x/web-interface/nav')
        data=body.get('data') or {}
        if body.get('code')!=0 or data.get('isLogin') is not True or type(data.get('mid')) is not int:
            raise PlatformError('account_identity_unavailable')
        return str(data['mid'])

    def comment_thread(self, oid, root, *, authenticated=False):
        """Anonymous, first-page-only public read; never inherits login cookies.

        Interface used by bilibili-api's comment.get_sub_comments. A missing
        row is inconclusive (moderation, deletion and pagination all exist).
        """
        if any(type(v) is not int or v <= 0 for v in (oid, root)):
            raise PlatformError('invalid_comment_identity')
        params=dict(type=1,oid=oid,root=root,ps=20,pn=1)
        if authenticated:
            value=self._json('api.bilibili.com','/x/v2/reply/reply',params)
        else:
            url='https://api.bilibili.com/x/v2/reply/reply?'+urllib.parse.urlencode(params)
            request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.bilibili.com/'})
            with urllib.request.build_opener(_NoRedirect()).open(request,timeout=15) as response:
                body=response.read(262145)
            if len(body)>262144:raise PlatformError('comment_response_oversized')
            value=json.loads(body)
        if value.get('code')!=0 or type(value.get('data')) is not dict:
            failure=PlatformError('comment_read_unavailable')
            failure.business_code=value.get('code') if type(value.get('code')) is int else None
            raise failure
        data=value['data']
        if type(data.get('root')) is not dict:
            raise PlatformError('comment_root_unavailable')
        return dict(root=data['root'],replies=(data.get('replies') or [])[:20])

    def send_comment_reply(self, oid, root, parent, text, *, before_post=None):
        """Single POST; caller must validate a tracked own chain before invocation."""
        if any(type(v) is not int or v<=0 for v in (oid,root,parent)):
            return SendResult('rejected','invalid_comment_identity')
        if type(text) is not str or not 1<=len(text.strip())<=120 or any(ord(c)<32 for c in text):
            return SendResult('rejected','invalid_text')
        try:
            jar,_=load_cookie_jar(self.auth_state_dir,include_csrf=True)
            csrf={c.value for c in jar if c.name=='bili_jct' and c.domain.lstrip('.')=='bilibili.com'}
            if len(csrf)!=1:return SendResult('rejected','csrf_unavailable')
            if before_post is None or before_post() is not True:
                return SendResult('rejected','cancelled_before_post')
            value=self._json('api.bilibili.com','/x/v2/reply/add',post=dict(
                type=1,oid=oid,root=root,parent=parent,message=text,plat=1,csrf=next(iter(csrf))))
            if type(value.get('code')) is int and value['code']!=0:
                return SendResult('rejected',str(value['code']))
            data=value.get('data') or {};reply=data.get('reply') or {};rpid=data.get('rpid') or reply.get('rpid')
            if any(source.get(key,expected)!=expected for source in (data,reply)
                for key,expected in (('root',root),('parent',parent))):
                return SendResult('unknown','unexpected_reply_receipt')
            if value.get('code')==0 and type(rpid) is int and rpid>0:
                return SendResult('sent','accepted',rpid,oid,*comment_hints(data))
            return SendResult('unknown','ambiguous_response')
        except Exception:return SendResult('unknown','transport_or_response')

    def send_video_comment(self, bvid, text, *, before_post=None):
        """One top-level video comment attempt; never replies to another user.

        Website API, not an official creator open-platform guarantee. A successful
        response means accepted by the server, not guaranteed public visibility.
        The caller persists intent before invocation and freezes unknown results.
        """
        if type(bvid) is not str or not re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid):
            return SendResult("rejected", "invalid_bvid")
        if type(text) is not str or not text.strip() or len(text) > 120 or any(ord(c) < 32 for c in text):
            return SendResult("rejected", "invalid_text")
        try:
            metadata = self.get_video_info(bvid)
            jar, _ = load_cookie_jar(self.auth_state_dir, include_csrf=True)
            values = {c.value for c in jar if c.name == "bili_jct" and c.domain.lstrip(".") == "bilibili.com"}
            if len(values) != 1:
                return SendResult("rejected", "csrf_unavailable")
        except AuthError as exc:
            return SendResult("rejected", exc.code)
        except Exception:
            # No POST has been attempted, so this is an explicit preflight failure.
            return SendResult("rejected", "video_preflight_unavailable")
        # Metadata lookup may block while the watcher switches to priority live.
        # Recheck at the last controllable point; already-in-flight POSTs cannot
        # be revoked and this does not claim an atomic network/state boundary.
        if before_post is not None:
            try:
                if before_post() is not True:
                    return SendResult("rejected", "cancelled_before_post")
            except Exception:
                return SendResult("rejected", "cancelled_before_post")
        try:
            # Exact form allowlist. root/parent/at-member fields are never sent.
            result = self._json("api.bilibili.com", "/x/v2/reply/add", post=dict(
                type=1, oid=metadata["aid"], message=text, plat=1, csrf=next(iter(values))))
            code = result.get("code")
            if type(code) is int and code != 0:
                return SendResult("rejected", str(code))
            data = result.get("data")
            if type(code) is int and code == 0 and type(data) is dict:
                rpid = data.get("rpid")
                if data.get('root',0)!=0 or data.get('parent',0)!=0:
                    return SendResult('unknown','unexpected_reply_receipt')
                reply = data.get("reply")
                if type(reply) is dict:
                    rpid = rpid or reply.get("rpid")
                    if reply.get("root", 0) != 0 or reply.get("parent", 0) != 0:
                        return SendResult("unknown", "unexpected_reply_receipt")
                if type(rpid) is int and rpid > 0:
                    return SendResult("sent", "accepted", rpid, metadata['aid'],*comment_hints(data))
            return SendResult("unknown", "ambiguous_response")
        except AuthError as exc:
            return SendResult("rejected", exc.code)
        except Exception:
            return SendResult("unknown", "transport_or_response")
