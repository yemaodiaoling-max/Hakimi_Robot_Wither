"""Identify media in acknowledged outgoing messages. Chat text never grants file access."""
import hashlib
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from hakimi.core.context_window import select_context
from hakimi.core.jsonio import tail_jsonl
from hakimi.core.runtime_io import read_derived_cached

_IMAGE = re.compile(r'\[CQ:image,file=(file:[^,\]]+),sub_type=([01])\]')
_IMAGE_QUESTION = re.compile(r'(?:你(?:刚才|刚刚|刚|上次)?发的|这张|那张|上张|上一张|你这|这个|是什么).{0,20}(?:图|表情|猫猫)|(?:这张|那张|上张|上一张).{0,8}(?:是什么|是谁|什么意思)')


def media_descriptor(segment, base, sticker_digest=None):
    """Describe the actual segment returned by transport, without interpreting its pixels."""
    segment = re.sub(r'^\[CQ:reply,id=-?\d+\]', '', segment)
    image = _IMAGE.fullmatch(segment)
    if image:
        if image[2] == '1':
            uri = urlsplit(image[1])
            name = unquote(uri.path)
            if re.match(r'^/[A-Za-z]:/', name):
                name = name[1:]
            path = Path(name)
            root = Path(base).absolute() / 'stickers'
            if not uri.netloc and path.is_relative_to(root) and '..' not in path.parts:
                relative = path.relative_to(root).as_posix()
                if len(relative) <= 250 and path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp', '.gif'}:
                    info = {'kind': 'sticker', 'asset': relative}
                    if re.fullmatch(r'[0-9a-f]{64}', sticker_digest or ''):
                        info['sha256'] = sticker_digest
                    return info
        return {'kind': 'image'}
    file = re.fullmatch(r'\[CQ:file,name=([^,\]]+),stamp=[^,\]]+,mode=(?:auto|file)\]', segment)
    if file:
        return {'kind': 'file', 'name': Path(unquote(file[1]).replace('\\', '/')).name[:120]}
    return None


def enrich_sent_rows(receipts, base):
    from hakimi.chat.legacy_sent_media import legacy_media_segments
    missing = any(not r.get('text') and not r.get('media') for r in receipts)
    legacy = legacy_media_segments(receipts, base) if missing else {}
    enriched = []
    for row in receipts:
        media = row.get('media')
        if not media:
            segment = legacy.get((row.get('session_key'), str(row.get('message_id'))))
            media = media_descriptor(segment, base) if segment else None
        if not isinstance(media, dict):
            enriched.append(row)
            continue
        label = {
            'image': '[我发出的图片]',
            'file': f"[我发出的附件：{media.get('name', '')}]",
            'sticker': f"[我发出的收藏表情，素材名：{Path(str(media.get('asset', ''))).name}]",
        }.get(media.get('kind'))
        text = (str(row.get('text') or '') + ' ' + label).strip() if label else None
        enriched.append(dict(row, media=media, text=text) if label else row)
    return enriched


def recalled_sticker(payload, base, now=None):
    """Resolve a real reference or an immediate image question to a delivered local sticker."""
    session = payload.get('session_key')
    if not session or payload.get('cold_topic_request') or payload.get('internal_review'):
        return None
    _, target = select_context(payload)
    ref = str(target.get('reply_to') or '')
    if not ref:
        question = str(target.get('text', ''))
        if target.get('image_paths') or '[群友发图' in question or '[CQ:image,' in question:
            return None
        if not _IMAGE_QUESTION.search(question):
            return None
    at = target.get('received_at', payload.get('created_at'))
    if type(at) not in (int, float):
        return None
    at = min(at, time.time() if now is None else now)
    path = Path(base) / 'state/sent_messages.jsonl'
    rows = read_derived_cached(path, lambda p: tail_jsonl(p, count=256, max_bytes=1048576), 'recent-own-replies-v1')
    max_age = 3600 if ref else 600
    receipts = [r for r in rows if r.get('session_key') == session and type(r.get('at')) in (int, float)
                and 0 <= at - r['at'] <= max_age]
    if not receipts:
        return None
    candidates = enrich_sent_rows(receipts, base)
    if ref:
        row = next((r for r in reversed(candidates) if str(r.get('message_id')) == ref), None)
    else:
        row = max(candidates, key=lambda r: r['at'])
    media = (row or {}).get('media') or {}
    if not isinstance(media, dict) or media.get('kind') != 'sticker':
        return None
    root = Path(base).absolute() / 'stickers'
    relative = Path(str(media.get('asset', '')))
    if relative.is_absolute() or '..' in relative.parts:
        return None
    asset = root / relative
    try:
        if asset.resolve() != asset or not asset.is_file() or asset.stat().st_size > 8 * 1024 * 1024:
            return None
        digest = media.get('sha256')
        if digest:
            if hashlib.sha256(asset.read_bytes()).hexdigest() != digest:
                return None
        elif asset.stat().st_mtime > row['at']:
            return None
        return str(row['message_id']), asset
    except (OSError, RuntimeError, ValueError):
        return None
