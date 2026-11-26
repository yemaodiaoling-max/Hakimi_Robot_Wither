"""QQ official transport. No model tools, no implicit retries of message sends.

Normal operation automatically registers official groups in one bridge process.
OpenIDs and reply authorities stay broker-side; prompts see numeric local aliases.
"""
import asyncio
from datetime import datetime
import html
import json
import re
import time
from urllib.parse import unquote, urlsplit
from pathlib import Path

from hakimi.transport.forward_messages import (
    QQ_CARD_KEYS, QQ_RECORD_HEADER, QQ_RECORD_TYPES, qq_card_text, qq_element_content, qq_record_text,
    split_at_context,
)
import websockets
from hakimi.chat.official_base import API_ROOT, GROUP_EVENTS, MAX_IMAGE_BYTES, MESSAGE_EVENTS, NoRedirect, OfficialError, PRIVATE_EVENTS, SELF_ALIAS, alias, cq_escape, fixed_json  # noqa: F401 (re-export)
from hakimi.transport.official_routes import RouteStore  # noqa: F401 (re-export)
from hakimi.transport.official_api import OfficialAPI, download_image  # noqa: F401 (re-export)


class OfficialAdapter:
    def __init__(self, base, cfg, api, store, clock=time.time):
        self.base, self.cfg, self.api, self.store, self.clock = Path(base), cfg, api, store, clock
        self.self_remote = ""
        self._plain_send_permissions = {}
        self.routes = store.get("group_routes", {})
        self.private_routes = store.get("private_routes", {})
        self.auto_groups = cfg.get("operation_mode") == "normal" and cfg.get("auto_adapt_groups", True)
        self._legacy_route()

    def _legacy_route(self):
        group = self.store.get("group_openid")
        if group and group not in self.routes:
            local = self.cfg["test_group_number"]
            self.routes[group] = {"openid": group, "local_id": local, "legacy_group": local, "active": True, "name": ""}
            self.store.set("group_routes", self.routes)

    def route_for_session(self, session):
        private=isinstance(session,str) and session.startswith('private:')
        routes=self.private_routes if private else self.routes
        prefix='private:' if private else 'group:'
        return next((r for r in routes.values() if r.get('active',True)
                     and session==prefix+r['local_id']),None)

    def ensure_private(self, openid):
        # Only an actual C2C event registers a private destination, never a group mention.
        if not isinstance(openid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',openid):return None
        route=self.private_routes.get(openid)
        if not route or not route.get('active',True):
            route={'openid':openid,'local_id':self.store.actor(openid),'kind':'private',
                   'scope':'private:'+openid,'active':True}
            self.private_routes[openid]=route
            self.store.set('private_routes',self.private_routes)
        return route

    async def can_send_plain(self, group, batch_key):
        """Normal sends avoid passive-reply mentions; honor QQ's per-group opt-in."""
        if self.cfg.get("allow_proactive") is not True:
            return False
        cached = self._plain_send_permissions.get(group)
        if cached and self.clock() - cached[0] < 60:
            return cached[1]
        try:
            state = await self.api.call("GET", f"/v2/groups/{group}/bot_state")
            allowed = state.get("allow_proactive_msg") is True
        except OfficialError:
            allowed = False
        self._plain_send_permissions[group] = (self.clock(), allowed)
        route = self.routes.get(group)
        if route:
            from hakimi.transport.group_capabilities import update
            update(self.base, 'group:' + route['local_id'], allow_proactive=allowed,
                   checked_at=self.clock())
        return allowed

    async def refresh_group_permissions(self):
        for group, route in list(self.routes.items()):
            if route.get('active', True):
                if self.cfg.get('allow_proactive') is not True:
                    from hakimi.transport.group_capabilities import update
                    update(self.base, 'group:' + route['local_id'], allow_proactive=False,
                           checked_at=self.clock())
                else:
                    await self.can_send_plain(group, 'permission-poll')

    async def permission_event(self, kind, data):
        group = data.get('group_openid') if isinstance(data, dict) else None
        route = self.routes.get(group)
        if not route:
            return
        self._plain_send_permissions.pop(group, None)
        if kind in {'GROUP_MSG_REJECT', 'GROUP_DEL_ROBOT'}:
            from hakimi.transport.group_capabilities import update
            update(self.base, 'group:' + route['local_id'], allow_proactive=False,
                   checked_at=self.clock(), source=kind)
        else:
            await self.can_send_plain(group, 'permission-event')

    async def ensure_group(self, group):
        if not isinstance(group, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", group):
            return None
        self._legacy_route()
        if group in self.routes and self.routes[group].get("active", True):
            return self.routes[group]
        if not self.auto_groups:
            return None
        from hakimi.chat.official_groups import adapt_group, known_groups, style_template
        info = {}
        try:
            response = await self.api.call("GET", f"/v2/groups/{group}/info")
            if isinstance(response, dict):
                info = response.get("group_info", response)
                if not isinstance(info, dict):
                    info = {}
        except OfficialError:
            pass  # Some applications cannot read group info; chat still works.
        legacy = next((str(info[k]) for k in ("group_id", "group_code")
                       if k in info and str(info[k]) in known_groups(self.base)), None)
        if any(r.get("legacy_group") == legacy for r in self.routes.values()):
            legacy = None
        route = self.routes.get(group, {"openid": group, "local_id": alias("group", group), "legacy_group": legacy})
        route.update(active=True, name=str(info.get("group_name") or info.get("name") or route.get("name", ""))[:100])
        route["style_source"] = style_template(self.base, route["name"])
        adapt_group(self.base, route)
        self.routes[group] = route
        self.store.set("group_routes", self.routes)
        return route

    async def control_command(self, route, author, content, local):
        command = re.fullmatch(r"\s*/(关联旧群|群风格)\s+(\S+)\s*", re.sub(r"<@!?[^>]+>", "", content))
        if not command:
            return False
        from hakimi.chat.official_groups import adapt_group, associate_group
        if author.get("member_role") not in {"owner", "admin"}:
            reply = "这项群配置请由群主或管理员设置"
        else:
            try:
                if command[1] == "关联旧群":
                    if any(r.get("legacy_group") == command[2] and r["local_id"] != route["local_id"] for r in self.routes.values()):
                        raise OfficialError("LEGACY_GROUP_ALREADY_LINKED")
                    associate_group(self.base, route, command[2])
                    reply = "已接上这个旧群的语气、规则和可用笔记，之后按这里的话题接话"
                else:
                    styles = {"学习": "study_calm", "原神剧情": "lore_club", "技术": "tech_banter",
                              "日常": "gentle_daily", "二次元": "playful_acg"}
                    if command[2] not in styles:
                        raise OfficialError("UNKNOWN_GROUP_STYLE")
                    route["profile"] = styles[command[2]]
                    adapt_group(self.base, route)
                    reply = "这个群的语气已更新，会按场合自然接话"
                self.store.set("group_routes", self.routes)
            except OfficialError as exc:
                reply = {"UNKNOWN_LEGACY_GROUP": "旧配置里没有这个群号，先按新群正常聊天",
                         "GROUP_ALREADY_ASSOCIATED": "这个群已经关联过旧群，换关联需要在本机调整",
                         "LEGACY_GROUP_ALREADY_LINKED": "这个旧群已经有关联，先核对一下群号",
                         "UNKNOWN_GROUP_STYLE": "可选风格：学习、原神剧情、技术、日常、二次元"}.get(exc.code, "这次群配置没有完成")
        try:
            plain = await self.can_send_plain(route["openid"], "group-config:"+local)
            authority = self.store.reserve("group-config:"+local, None if plain else self.store.message(local), plain)
            result = await self.api.call("POST", f"/v2/groups/{route['openid']}/messages", {"msg_type": 0, "content": reply, **authority})
            if isinstance(result.get("id"), str):
                self.store.remember(result["id"], None, self.clock(), "out", route["openid"])
        except OfficialError as exc:
            self.store.set("last_send_error", {"code": exc.code, "at": self.clock()})
        return True

    def self_mentions(self, data):
        # READY user.id and a group's member OpenID are different namespaces.
        # Only platform is_you=true is identity evidence; names/bot flags are not.
        known = set(self.store.get("group_self_remote_ids", []))
        mentions = data.get("mentions")
        directed = False
        for mention in mentions[:64] if isinstance(mentions, list) else []:
            if not isinstance(mention, dict) or mention.get("is_you") is not True:
                continue
            directed = True
            for key in ("id", "member_openid", "openid", "user_openid"):
                value = mention.get(key)
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                    known.add(value)
        if directed:
            self.store.set("group_self_remote_ids", sorted(known)[:64])
        return known | {self.self_remote}, directed

    async def translate(self, event_type, data):
        if event_type not in MESSAGE_EVENTS or not isinstance(data, dict):
            return None
        author = data.get("author", {})
        if not isinstance(author, dict):
            return None
        private=event_type in PRIVATE_EVENTS
        if private and self.cfg.get("allow_private_chat") is not True:return None
        content = data.get("content", "")
        content = content[:12000] if isinstance(content, str) else ""
        paired = False if private else self.store.bind(data, re.sub(r"<@!?[^>]+>", "", content))
        group = data.get("group_openid")
        remote = data.get("id")
        member = author.get("user_openid") if private else author.get("member_openid") or author.get("id")
        if (not isinstance(remote, str) or not 1 <= len(remote) <= 512
                or not isinstance(member, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", member)):
            return None
        try:
            stamp = datetime.fromisoformat(str(data["timestamp"]).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                return None
            at = stamp.timestamp()
            if not self.clock() - 600 <= at <= self.clock() + 30:
                return None
        except (KeyError, ValueError, TypeError):
            return None
        route = self.ensure_private(member) if private else await self.ensure_group(group)
        if not route:
            return None
        group=route.get("scope",route["openid"])
        self_ids, directed = (set(),False) if private else self.self_mentions(data)
        if member in self_ids or member == self.self_remote:
            return None
        if paired:
            return None  # Pairing learns identity but never generates a QQ message.
        scene = data.get("message_scene")
        raw_ext = scene.get("ext", []) if isinstance(scene, dict) else []
        ext = dict(item.split("=", 1) for item in raw_ext if isinstance(item, str) and len(item) <= 600
                   and item.startswith(("msg_idx=", "ref_msg_idx="))) if isinstance(raw_ext, list) else {}
        local_user = self.store.actor(member)
        local, fresh = self.store.remember(remote, ext.get("msg_idx"), min(at, self.clock()), group=group, user_id=local_user)
        if not fresh:
            return None
        if not private and author.get('bot') is True:
            from hakimi.chat.bot_registry import platform
            platform(self.base,local_user,str(author.get('username',''))[:80],'group:'+route['local_id'])
        if not private and await self.control_command(route, author, content, local):
            return None
        message_type = data.get("message_type")
        record_type = message_type if message_type in QQ_RECORD_TYPES else None
        elements = data.get("msg_elements")
        prior_messages = []
        if event_type == "GROUP_AT_MESSAGE_CREATE" and record_type in (103, '103'):
            prior_messages, elements = split_at_context(elements)
        record = None
        if record_type and not prior_messages:
            header = QQ_RECORD_HEADER.search(content)
            start = header.start() if header else 0
            record = qq_record_text(content[start:], quote=record_type in (103, '103'), typed=True)
            if record is not None:
                content = content[:start]
        nested_text, nested_media = qq_element_content(elements, record_type=record_type)
        cards = [qq_card_text(data[key], content) for key in QQ_CARD_KEYS if data.get(key)]
        clean = cq_escape(content)
        if not private:
            clean = re.sub(r"<@!?([A-Za-z0-9_-]+)>", lambda m:
                f"[CQ:at,qq={SELF_ALIAS if m[1] in self_ids else self.store.actor(m[1])}]", clean)
        for text in dict.fromkeys([record, *cards, nested_text]):
            if text:
                if not clean.strip():
                    clean = ''
                elif not clean.endswith('\n'):
                    clean += '\n'
                clean += cq_escape(text)
        has_text = bool(clean.strip())
        if (event_type == "GROUP_AT_MESSAGE_CREATE" or directed) and f"[CQ:at,qq={SELF_ALIAS}]" not in clean:
            clean = f"[CQ:at,qq={SELF_ALIAS}] " + clean
        if private:
            self.store.set('last_private_received_at',self.clock())
        else:
            self.store.set("last_mention_check", {"at": self.clock(), "event_type": event_type,
                "platform_self_mention": directed, "normalized_self_mention": f"[CQ:at,qq={SELF_ALIAS}]" in clean})
        ref = self.store.ref_local(ext.get("ref_msg_idx", ""), group)
        if not ref and ext.get('ref_msg_idx'):
            from hakimi.memory.voice_receipts import resolve_reference
            ref=resolve_reference(self.base,('private:' if private else 'group:')+route['local_id'],ext['ref_msg_idx'])
        if ref:
            clean = f"[CQ:reply,id={ref}] " + clean
        attachments = data.get("attachments")
        attachments = attachments[:8] if isinstance(attachments, list) else []
        media = attachments + [item for item in nested_media if item not in attachments]
        from hakimi.tools.qq_video import register
        video_note = register(self.base, ('private:' if private else 'group:')+route['local_id'], local, media)
        if video_note:
            clean = (clean+' '+video_note).strip()
            has_text = True
        for index, attachment in enumerate(media[:4]):
            if not isinstance(attachment, dict):
                continue
            mime = attachment.get("content_type", "")
            if mime in ("file", "application/octet-stream") and isinstance(attachment.get("filename"), str):
                clean += " " + cq_escape("[群文件：" + attachment["filename"][:240] + "]")
                has_text = True
            if not isinstance(mime, str) or not mime.startswith("image/"):
                continue
            # Existing image authority requires the daemon-owned session prefix.
            # Match it so the same bounded lease can authorize viewing/collection.
            path = self.base / "state/img_cache" / f"img_{'private' if private else 'group'}_{route['local_id']}_{local}_{index}.jpg"
            has_text = True
            try:
                url = attachment.get("url")
                if not isinstance(url, str):
                    raise OfficialError("IMAGE_URL_MISSING")
                await asyncio.to_thread(download_image, url, path)
                clean += f" [CQ:image,file={path.as_posix()}]"
            except OfficialError:
                clean += " [群友发图，未取得图片内容]"
        event={"post_type":"message","message_type":"private" if private else "group","self_id":int(SELF_ALIAS),
               "user_id":int(local_user),"message_id":int(local),"raw_message":clean,
               "sender":{"nickname":str(author.get('username') or ('朋友' if private else '群友'))[:80]}}
        if not private:event['group_id']=int(route['local_id'])
        if prior_messages:
            from hakimi.chat.self_reply_context import recent_sent_receipts
            try:
                receipts = recent_sent_receipts(self.base, 'group:' + route['local_id'], at)
            except OSError:
                receipts = []
            sent = {row['text'].strip() for row in receipts
                    if isinstance(row.get('text'), str) and row['text'].strip()}
            for row in prior_messages:
                if row['speaker'] == 'QQ未标注' and row.get('text') in sent and not row.get('attachments'):
                    row['same_text_as'] = '哈基米在本群的已发送回执'
            event['qq_prior_messages'] = prior_messages
        from hakimi.voice.audio import approved
        voices=[]
        for attachment in attachments[:3]:
            if not isinstance(attachment,dict):continue
            mime=str(attachment.get('content_type','')).lower()
            name=str(attachment.get('filename',''))
            if not (mime.startswith('audio/') or mime=='application/octet-stream' and re.search(r'\.(?:silk|amr|ogg|opus|wav|mp3)$',name,re.I)):continue
            url=attachment.get('url','')
            if approved(url):voices.append({'url':url,'content_type':mime,'size':attachment.get('size')})
        if not has_text and not voices and not prior_messages:
            if record_type in (103, '103') and ext.get('ref_msg_idx'):
                if not ref:
                    clean += ' 引用了一条消息'
            elif not ((event_type == 'GROUP_AT_MESSAGE_CREATE' or directed)
                      and message_type in (None, 0, '0')
                      and isinstance(data.get('content', ''), str)
                      and not any(data.get(key) for key in (*QQ_CARD_KEYS, 'msg_elements', 'attachments',
                                                            'prompt', 'callback_data', 'interaction'))):
                return None
        if voices:
            event['_voice_attachments']=voices
            event['_voice_received_at']=min(at,self.clock())
            event['_voice_platform_ref']=ext.get('msg_idx','')
        return event

    async def send(self, session, text, timeout=12):
        phase = {}
        if text.startswith(("[CQ:file,", "[CQ:image,")):
            from hakimi.chat.file_delivery import settings
            timeout = max(timeout, float(settings(self.base)["upload_timeout_seconds"]))
        try:
            async with asyncio.timeout(timeout):
                return await self._send_once(session, text, phase)
        except TimeoutError:
            from hakimi.chat.message_lifecycle import record
            record(self.base,dict(session_key=session,reply_target_message_id=phase.get('source_id','')),
                   'send_error',trigger_id=phase.get('trigger_id',''),reason='SEND_TIMEOUT_UNKNOWN')
            self.store.set("last_send_error", {"code": "SEND_TIMEOUT_UNKNOWN", "at": self.clock()})
            return {"status": "file_error" if text.startswith(("[CQ:file,", "[CQ:image,")) and not phase.get("posting") else "unknown"}

    async def _send_once(self, session, text, phase=None):
        phase = {} if phase is None else phase
        mention_message = "[CQ:at," in text or "<qqbot-at-user" in html.unescape(text)
        file_message = text.startswith("[CQ:file,")
        image_message = text.startswith("[CQ:image,")
        route = self.route_for_session(session)
        if not route:
            return {"status": "rejected"}
        group = route["openid"]
        private=route.get("kind")=="private"
        scope=route.get("scope",group)
        prefix=f"/v2/{'users' if private else 'groups'}/{group}"
        try:
            pending = json.loads((self.base / "state/delivery_state.json").read_text(encoding="utf-8"))["pending"]
            if pending.get("session_key") != session or pending["segments"][pending["next_segment"]] != text:
                raise OfficialError("DELIVERY_CONTEXT_MISMATCH")
            phase.update(trigger_id=pending.get('trigger_id',''),source_id=pending.get('reply_target_id',''),original_segment=text)
            sent_text = text
            sticker_digest = None
            source = self.store.source(pending["trigger_id"])
            if source and source["group_openid"] != scope:
                raise OfficialError("REPLY_GROUP_MISMATCH")
            quote = re.match(r"^\[CQ:reply,id=(-?\d+)\]", text)
            target_id = pending.get('reply_target_id') or (source or {}).get('local_id')
            if quote:
                target_id = quote[1]
                text = text[quote.end():]
                file_message = text.startswith('[CQ:file,')
                image_message = text.startswith('[CQ:image,')
            media = re.fullmatch(r"\[CQ:image,file=(file:[^,\]]+),sub_type=([01])\]", text)
            body = {"msg_type": 0, "content": html.unescape(text)}
            if media:
                parsed = urlsplit(media[1])
                if parsed.netloc:
                    raise OfficialError("NONLOCAL_STICKER")
                name = unquote(parsed.path)
                if re.match(r"^/[A-Za-z]:/", name):
                    name = name[1:]
                path = Path(name).resolve()
                if media[2] == "1":
                    root = self.base / "stickers"
                    if root.resolve() != root.absolute() or not path.is_relative_to(root) or not path.is_file():
                        raise OfficialError("UNAPPROVED_STICKER")
                    from hakimi.tools.sticker_catalog import require_allowed
                    sticker_digest = require_allowed(path.read_bytes(), path.name, self.base)["sha256"]
                else:
                    from hakimi.chat.image_permissions import allowed_images_for_session
                    if str(path) not in allowed_images_for_session(session, self.base):
                        raise OfficialError("UNAPPROVED_IMAGE")
                info = await self.api.upload_image(group,path,private=True) if private else await self.api.upload_image(group,path)
                body = {"msg_type": 7, "media": {"file_info": info}}
            elif file_message:
                from hakimi.chat.file_delivery import from_segment
                try:path, kind = from_segment(text, session, self.base)
                except ValueError as exc:raise OfficialError("INVALID_OUTGOING_FILE") from exc
                phase["stage"]="upload"
                if kind==3:
                    from hakimi.voice.tts import fallback as voice_fallback
                    phase['voice_fallback_text']=voice_fallback(self.base,session,path.name)
                try:
                    info = await self.api.upload_file(group,path,kind,private=True) if private else await self.api.upload_file(group,path,kind)
                except OfficialError as exc:
                    if (exc.ambiguous or exc.code not in {'HTTP_40093003','API_40093003'}
                            or not re.fullmatch(r'diary_export_[0-9a-f]{32}\.pdf',path.name)):
                        raise
                    from hakimi.features.diary_store import latest_image
                    image=await asyncio.to_thread(latest_image,self.base,session)
                    info=await self.api.upload_image(group,image,private=True) if private else await self.api.upload_image(group,image)
                    sent_text=f'[CQ:image,file={image.as_uri()},sub_type=0]'
                body = {"msg_type": 7, "media": {"file_info": info}}
            elif '[CQ:at,' in text and not private:
                from hakimi.chat.member_mentions import wire_text
                try:body={'msg_type':2,'markdown':{'content':wire_text(text,self.store)}}
                except ValueError as exc:raise OfficialError('INVALID_MENTION') from exc
            elif "<qqbot-at-user" in html.unescape(text) and not private:
                body={"msg_type":2,"markdown":{"content":html.unescape(text)}}
            elif "[CQ:" in text:
                raise OfficialError("UNSUPPORTED_MESSAGE_SEGMENT")
            # Preserve the model's decision/content. Only change wire delivery so
            # ordinary bubbles don't all become QQ's passive @-replies.
            plain = False if private else await self.can_send_plain(group, pending["reply_id"])
            if (pending.get('rendered_math') and not plain and source
                    and len(pending['segments'])-pending['next_segment']>5-source['seq']):
                self.store.set('last_send_error',{'code':'MATH_BATCH_NEEDS_PROACTIVE_PERMISSION','at':self.clock()})
                return {'status':'file_error'}
            authority = self.store.reserve(pending["reply_id"] + ":" + str(pending["next_segment"]),
                                           None if plain else source, plain)
            body.update(authority)
            if not private and pending['next_segment'] == 0 and not pending.get('is_proactive'):
                reference = self.store.quote_reference(target_id, group)
                if reference:
                    body['message_reference'] = reference
            if private and body.get('msg_type')==7:body['content']=' '
            phase.update(posting=True,stage="message",mode="proactive" if plain else "passive")
            result = await self.api.call("POST", prefix+"/messages", body)
            if not isinstance(result.get("id"), str) or not result["id"]:
                raise OfficialError("SEND_ACK_MISSING", ambiguous=True)
            local, _ = self.store.remember(result["id"], result.get("ext_info", {}).get("ref_idx"), self.clock(), "out", scope)
            receipt = {"status": "success", "message_id": local, "sent_text": sent_text}
            if sticker_digest:
                receipt["sticker_digest"] = sticker_digest
            return receipt
        except OfficialError as exc:
            if phase.get('voice_fallback_text') and (not phase.get('posting') or not exc.ambiguous):
                return await self._voice_fallback(session,phase,known_rejected=not exc.ambiguous)
            from hakimi.features.codex_help import report_fault
            report_fault(self.base, "official_send", code=exc.code)
            error={"code":exc.code,"at":self.clock(),"stage":phase.get('stage','prepare'),
                   "endpoint":getattr(exc,'endpoint',''),"detail":getattr(exc,'detail',''),
                   "mode":phase.get('mode',''),"session_key":session,"reply_id":pending.get('reply_id','')}
            from hakimi.chat.message_lifecycle import record
            record(self.base,dict(session_key=session,reply_target_message_id=pending.get('reply_target_id','')),
                   'send_error',trigger_id=pending.get('trigger_id',''),reason=exc.code)
            self.store.set("last_send_error",error)
            if file_message:
                with (self.base/'state/file_send_errors.jsonl').open('a',encoding='utf-8') as log:
                    log.write(json.dumps(error,ensure_ascii=False)+'\n')
            if (file_message or image_message or mention_message) and (not phase.get("posting") or not exc.ambiguous):return {"status": "file_error"}
            return {"status": "unknown" if exc.ambiguous else "rejected"}
        except Exception:
            return {"status": "file_error" if (file_message or image_message) and not phase.get("posting") else "unknown"}

    async def _voice_fallback(self,session,phase,*,known_rejected=False):
        if phase.get('fallback_attempted') or phase.get('posting') and not known_rejected:return {'status':'unknown'}
        phase['fallback_attempted']=True
        try:
            pending=json.loads((self.base/'state/delivery_state.json').read_text())['pending']
            if pending.get('session_key')!=session or pending.get('trigger_id')!=phase.get('trigger_id') or pending['segments'][pending['next_segment']]!=phase.get('original_segment'):return {'status':'file_error'}
            route=self.route_for_session(session)
            if not route:return {'status':'file_error'}
            private=route.get('kind')=='private';group=route['openid'];scope=route.get('scope',group)
            source=self.store.source(pending['trigger_id'])
            if source and source['group_openid']!=scope:return {'status':'file_error'}
            plain=False if private else await self.can_send_plain(group,pending['reply_id'])
            authority=self.store.reserve(pending['reply_id']+':'+str(pending['next_segment'])+':voice-text',None if plain else source,plain)
            text='这次语音没发成功，改用文字：'+phase['voice_fallback_text']
            body={'msg_type':0,'content':text,**authority}
            reference=self.store.quote_reference(pending.get('reply_target_id') or (source or {}).get('local_id'),scope)
            if reference and not private:body['message_reference']=reference
            phase['posting']=True
            result=await self.api.call('POST',f"/v2/{'users' if private else 'groups'}/{group}/messages",body)
            if not isinstance(result.get('id'),str) or not result['id']:return {'status':'unknown'}
            local,_=self.store.remember(result['id'],None,self.clock(),'out',scope)
            return {'status':'success','message_id':local,'sent_text':cq_escape(text)}
        except OfficialError as exc:return {'status':'unknown' if exc.ambiguous else 'file_error'}
        except Exception:return {'status':'unknown' if phase.get('posting') else 'file_error'}

    async def gateway(self, queue, status):
        session, seq, delay = None, None, 2
        while True:
            try:
                token = await self.api.token()
                result = await self.api.call("GET", "/gateway")
                url = urlsplit(result.get("url", ""))
                if url.scheme != "wss" or url.hostname not in {"api.bot.qq.com", "api.sgroup.qq.com"}:
                    raise OfficialError("INVALID_GATEWAY")
                async with websockets.connect(url.geturl(), max_size=2 * 1024 * 1024, open_timeout=10) as ws:
                    hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    interval = float(hello.get("d", {}).get("heartbeat_interval", 0)) / 1000
                    if hello.get("op") != 10 or not 1 <= interval <= 120:
                        raise OfficialError("INVALID_GATEWAY_HELLO")
                    identify = {"token": "QQBot " + token, "intents": 1 << 25, "shard": [0, 1]}
                    await ws.send(json.dumps({"op": 6 if session and seq is not None else 2,
                        "d": {"token": "QQBot " + token, "session_id": session, "seq": seq} if session and seq is not None else identify}))
                    acknowledged = True
                    next_beat = time.monotonic() + interval
                    while True:
                        if time.monotonic() >= next_beat:
                            if not acknowledged:
                                raise OfficialError("HEARTBEAT_ACK_MISSING")
                            await ws.send(json.dumps({"op": 1, "d": seq}))
                            acknowledged = False
                            next_beat = time.monotonic() + interval
                            if self.api.clock() >= self.api._expires - 45:
                                break
                        try:
                            frame = json.loads(await asyncio.wait_for(ws.recv(), max(.01, next_beat - time.monotonic())))
                        except asyncio.TimeoutError:
                            continue
                        op, kind = frame.get("op"), frame.get("t")
                        if op == 11:
                            acknowledged = True
                        elif op == 7:
                            break
                        elif op == 9:
                            session, seq = None, None
                            break
                        elif op == 0:
                            if kind == "READY":
                                session = frame["d"]["session_id"]
                                self.self_remote = str(frame["d"]["user"]["id"])
                                self.store.set("self_remote", self.self_remote)
                                status(True, None)
                                delay = 2
                            elif kind == "RESUMED":
                                status(True, None)
                                delay = 2
                            elif kind in MESSAGE_EVENTS:
                                self.store.set("last_event", {"type": kind, "at": self.clock()})
                                event = await self.translate(kind, frame.get("d"))
                                if event:
                                    if kind == 'GROUP_MESSAGE_CREATE':
                                        route = self.routes.get(frame.get('d', {}).get('group_openid'))
                                        if route:
                                            from hakimi.transport.group_capabilities import update
                                            update(self.base, 'group:' + route['local_id'], receive_all=True,
                                                   source='GROUP_MESSAGE_CREATE')
                                    await queue.put(event)
                            elif kind in {'GROUP_MSG_REJECT', 'GROUP_MSG_RECEIVE'}:
                                await self.permission_event(kind, frame.get('d', {}))
                            elif kind == "GROUP_ADD_ROBOT" and self.auto_groups:
                                await self.ensure_group(frame.get("d", {}).get("group_openid"))
                            elif kind == "GROUP_DEL_ROBOT" and self.auto_groups:
                                await self.permission_event(kind, frame.get('d', {}))
                                route = self.routes.get(frame.get("d", {}).get("group_openid"))
                                if route:
                                    from hakimi.chat.official_groups import adapt_group
                                    route["active"] = False
                                    adapt_group(self.base, route)
                                    self.store.set("group_routes", self.routes)
                            if type(frame.get("s")) is int:
                                seq = frame["s"]
            except asyncio.CancelledError:
                raise
            except websockets.ConnectionClosed as exc:
                code = getattr(exc.rcvd, "code", None)
                status(False, "GATEWAY_" + str(code))
                if code in {4001, 4002, 4010, 4011, 4012, 4013, 4014, 4914, 4915}:
                    raise OfficialError("GATEWAY_REQUIRES_CONFIGURATION_" + str(code)) from None
                if code in {4006, 4007} or code in range(4900, 4914):
                    session, seq = None, None
            except Exception as exc:
                status(False, exc.code if isinstance(exc, OfficialError) else type(exc).__name__)
            finally:
                status(False, None)
            await asyncio.sleep(delay)
            delay = min(60, delay * 2)
