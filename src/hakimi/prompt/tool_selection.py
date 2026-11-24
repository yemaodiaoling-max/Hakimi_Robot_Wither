"""Stable event-based tool groups for direct sessions."""
import re

IMAGE = {'images.list', 'image.read', 'stickers.collect'}
VIDEO = {'video.watch', 'video.watch_miniapp'}
FILES = {'files.list', 'files.send', 'chat.export', 'diary.export'}
OPTIONAL = IMAGE | VIDEO | FILES | {'voice.speak','voice.read','voice.hum','video.watch_attachment'}


def select_names(payload, names, base=None):
    if isinstance(payload.get('internal_review'), dict):
        return set(names) & {'memory.read', 'memory.search', 'memory.graph', 'memory.social',
                             'memory.social_append', 'reply.skip', 'turn.failure', 'tools.list'}
    selected = set(names) - OPTIONAL
    # Only inspect transport/context markers, never infer a task from its topic.
    from hakimi.core.context_window import select_context
    rows, target = select_context(payload)
    from hakimi import paths
    from hakimi.voice.tts import offer_mode, offer_hum
    base = paths.base() if base is None else base
    if offer_mode(payload,base):
        selected.add('voice.speak')
    if offer_hum(payload,base):
        selected.add('voice.hum')
    if any(row.get('voice_ref') or 'voice.read message_id=' in str(row.get('text','')) for row in rows):
        selected.add('voice.read')
    elif target.get('reply_to') and (base/'state/voice.sqlite3').is_file():
        from hakimi.voice.store import preview
        if preview(base,payload.get('session_key'),str(target['reply_to'])).get('text'):
            selected.add('voice.read')
    text = '\n'.join(str(row.get('text', '')) for row in rows)
    if '[QQ视频 video.watch_attachment ref=' in text:
        selected.add('video.watch_attachment')
    sources = [payload, *rows]
    def has_value(*fields):
        return any(source.get(field) for source in sources for field in fields)
    if has_value('image', 'image_path', 'image_paths', 'images', 'trusted_image_paths', 'allowed_image_paths') or re.search(r'\[CQ:image,|\[(?:群友发图|表情包图片)\(', text):
        selected.update(IMAGE)
    from hakimi.tools.bilibili_video import BARE_BV
    if has_value('video', 'video_url') or BARE_BV.search(text) or re.search(r'https?://(?:www\.|m\.)?(?:bilibili\.com|b23\.tv)/|\[CQ:video,', text, re.I):
        selected.update(VIDEO)
    if has_value('available_files', 'file', 'file_id', 'file_name', 'attachments') or re.search(r'\[CQ:file,', text):
        selected.update(FILES)
    return selected
