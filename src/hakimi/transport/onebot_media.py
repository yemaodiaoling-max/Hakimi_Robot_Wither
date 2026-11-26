"""Render inbound OneBot CQ messages and cache their QQ images."""

import html
import io
import json
import re
import urllib.request
from pathlib import Path

from PIL import Image

from hakimi.transport.forward_messages import expand_forward, message_text


def http_post(base_url: str, endpoint: str, payload: dict, timeout: float = 6.0) -> dict:
    try:
        req = urllib.request.Request(
            f"{base_url}/{endpoint}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def cache_image_to_jpg(file_param: str, url_param: str, tag_id: str, *, cache_dir: Path, post) -> str | None:
    """将 QQ 图片通过 NapCat get_image 或 URL 下载并转换为标准 RGB JPG 存入 state/img_cache/"""
    tag_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(tag_id))[:160]
    out_path = cache_dir / f"img_{tag_id}.jpg"
    if out_path.exists() and out_path.stat().st_size > 100:
        return out_path.as_posix()

    raw_bytes = None
    # 优先通过 NapCat get_image 获取本地原图路径
    if file_param:
        resp = post("get_image", {"file": file_param}, timeout=6.0)
        local_src = resp.get("data", {}).get("file")
        if local_src and Path(local_src).exists():
            try:
                raw_bytes = Path(local_src).read_bytes()
            except Exception:
                pass
        if not raw_bytes and resp.get("data", {}).get("url"):
            url_param = resp["data"]["url"]

    # 备选：直接从 URL 下载
    if not raw_bytes and url_param:
        clean_u = html.unescape(url_param)
        try:
            req = urllib.request.Request(clean_u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6.0) as r:
                raw_bytes = r.read()
        except Exception:
            pass

    if not raw_bytes:
        return None

    try:
        # 如果原图本身是 GIF 动图，同时保留原版 .gif 供 save_sticker 收藏原汁原味动图！
        if raw_bytes[:6] in (b"GIF87a", b"GIF89a"):
            gif_path = cache_dir / f"img_{tag_id}.gif"
            gif_path.write_bytes(raw_bytes)

        img = Image.open(io.BytesIO(raw_bytes))
        # 提取第一帧转 RGB 存入 .jpg，确保 view_file 100% 兼容可读
        if getattr(img, "is_animated", False):
            img.seek(0)
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[3])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        # 限制最大边长 1280 节省视觉 Token
        img.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
        img.save(out_path, "JPEG", quality=88)
        return out_path.as_posix()
    except Exception as e:
        print(f"   [图片缓存转换失败] {tag_id}: {e}")
        return None


def format_clean_message(
    raw_text: str, self_id: str, msg_id: str | int, msg_lookup: dict[str, dict],
    image_paths: list[str] | None = None, session_key: str | None = None,
    _forward_depth=0, _media_budget=None, *, post, cache_image, register_image, normalize_cards,
) -> str:
    """将含 CQ 码的原始消息转换为带本地图片缓存路径与引用内容的清晰文本"""
    text = re.sub(rf"\[CQ:at,qq={self_id}[^\]]*\]\s*", "@哈基米 ", normalize_cards(raw_text))
    if _media_budget is None:
        _media_budget = {"images": 2, "fetches": 2}

    # 1. 简化 QQ 小黄豆表情 [CQ:face,id=...,raw={...}]
    def _repl_face(m):
        raw_part = m.group(0)
        ft = re.search(r'faceText"\s*:\s*"([^"]+)"', html.unescape(raw_part))
        return f"[表情:{ft.group(1)}]" if ft else "[QQ表情]"

    text = re.sub(r"\[CQ:face,[^\]]+\]", _repl_face, text)

    # 2. 解析并缓存 [CQ:image,...]
    img_idx = 0

    def _repl_image(m):
        nonlocal img_idx
        if _forward_depth:
            if not _media_budget["images"]:
                return "[转发内图片，未加载像素]"
            _media_budget["images"] -= 1
        full_cq = m.group(0)
        img_idx += 1
        f_match = re.search(r"file=([^,\]]+)", full_cq)
        u_match = re.search(r"url=([^,\]]+)", full_cq)
        is_meme = "动画表情" in html.unescape(full_cq) or "sub_type=1" in full_cq
        file_val = f_match.group(1) if f_match else ""
        url_val = u_match.group(1) if u_match else ""
        local_jpg = cache_image(file_val, url_val, f"{session_key or 'unscoped'}_{msg_id}_{img_idx}")
        kind_label = "表情包图片" if is_meme else "群友发图"
        if local_jpg:
            if session_key:
                register_image(session_key, local_jpg)
                cached_gif = Path(local_jpg).with_suffix(".gif")
                if cached_gif.is_file():
                    register_image(session_key, cached_gif)
            if image_paths is not None:
                image_paths.append(local_jpg)
                gif_path = Path(local_jpg).with_suffix(".gif")
                if gif_path.is_file():
                    image_paths.append(gif_path.as_posix())
            return f"[{kind_label}(可用view_file看图): {local_jpg}]"
        return f"[{kind_label}]"

    text = re.sub(r"\[CQ:image,[^\]]+\]", _repl_image, text)

    # 3. 解析 [CQ:reply,id=...] 引用内容（让哈基米知道群友在针对哪句话或哪张图说话！）
    def _repl_reply(m):
        rep_id = m.group(1)
        if rep_id in msg_lookup and session_key and msg_lookup[rep_id].get("session_key") == session_key:
            ref = msg_lookup[rep_id]
            if image_paths is not None:
                image_paths.extend(ref.get("image_paths", []))
            return f"[引用 {ref['nickname']} 的消息(id={rep_id}): \"{ref['text']}\"] "
        # 尝试通过 NapCat get_msg 查询被引用的历史消息
        resp = post("get_msg", {"message_id": int(rep_id)}, timeout=4.0)
        data = resp.get("data", {})
        same_session = False
        if isinstance(data, dict) and session_key:
            kind, target_id = session_key.split(":", 1)
            if kind == "group":
                same_session = str(data.get("group_id", "")) == target_id
            elif kind == "private" and data.get("message_type") == "private":
                peer_id = data.get("user_id") or data.get("sender", {}).get("user_id")
                same_session = str(peer_id) == target_id
        if data and same_session:
            ref_sender = data.get("sender", {})
            ref_nick = ref_sender.get("card") or ref_sender.get("nickname") or str(data.get("user_id", ""))
            ref_raw = data.get("raw_message", "")
            # 递归解析被引用消息里的图片（不递归引用避免死循环）
            ref_clean = format_clean_message(
                re.sub(r"\[CQ:reply,id=-?\d+\]", "", ref_raw),
                self_id,
                rep_id,
                {},
                image_paths,
                session_key,
                post=post, cache_image=cache_image, register_image=register_image,
                normalize_cards=normalize_cards,
            )
            return f"[引用 {ref_nick} 的消息(id={rep_id}): \"{ref_clean}\"] "
        return f"[引用消息(id={rep_id})] "

    text = re.sub(r"\[CQ:reply,id=(-?\d+)\]\s*", _repl_reply, text)
    if "[CQ:forward," in text or "[CQ:json," in text or "[CQ:xml," in text:
        def fetch_forward(forward_id):
            if _forward_depth >= 2 or not _media_budget["fetches"]:
                return {}
            _media_budget["fetches"] -= 1
            return post("get_forward_msg", {"message_id": forward_id, "id": forward_id}, timeout=4.0)
        def render_content(content):
            raw = re.sub(r"\[CQ:reply,id=-?\d+\]", "[转发内引用]", message_text(content))
            return format_clean_message(
                raw, self_id, f"{msg_id}_f{_media_budget['images']}_{_media_budget['fetches']}",
                {}, image_paths, session_key, _forward_depth + 1, _media_budget,
                post=post, cache_image=cache_image, register_image=register_image,
                normalize_cards=normalize_cards,
            )
        text = expand_forward(text, fetch_forward, render_content)
    return text.strip()
