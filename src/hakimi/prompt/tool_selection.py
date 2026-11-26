"""Select direct-session tools from the current event and its context."""
import re
from hakimi.tools.drawing_intent import TOOL as DRAW_TOOL, offered as offer_drawing

IMAGE = {'images.list', 'image.read', 'stickers.collect'}
VIDEO = {'video.watch', 'video.watch_miniapp'}
FILES = {'files.list', 'files.send', 'chat.export', 'diary.export'}
OPTIONAL = IMAGE | VIDEO | FILES | {'voice.speak', 'voice.read', 'voice.hum', 'video.watch_attachment', DRAW_TOOL}
ON_DEMAND = {'diary.read', 'memory.graph', 'memory.social', 'runtime.status'}
REVIEW = {'memory.read', 'memory.search', 'memory.graph', 'memory.social',
          'memory.social_append', 'reply.skip', 'turn.failure', 'tools.list'}
INTENT_ONLY = {DRAW_TOOL, 'voice.speak', 'voice.hum'}
DIARY = re.compile(r'日记|\b(?:diary|diaries)\b', re.I)
DIARY_EXPORT = re.compile(
    r'(?:想看|看看|看下|看一[下眼]|查看|发给我|给我发|发我|发下|发来|导出|下载).{0,16}日记'
    r'|日记.{0,12}(?:发.{0,2}我|发来|发一下|导出|下载|PDF|给我看看)'
    r'|\bdiary\.export\b|\b(?:show|send|export|download).{0,24}(?:diary|diaries)\b', re.I)
NO_DIARY_EXPORT = re.compile(r'(?:不要|不用|不想|别).{0,6}(?:看|发|传|导出).{0,8}日记')
GRAPH = re.compile(r'(?:记忆|关系|人物)图谱|人物关系|\bmemory\.graph\b', re.I)
SOCIAL = re.compile(r'人物小本子|人物画像|建档|重新.{0,8}(?:印象|画像)|\bmemory\.social\b', re.I)
RUNTIME = re.compile(
    r'\bruntime\.status\b|(?:工具|本轮|机器人|哈基米|发送|回复).{0,8}(?:异常|失败|报错|故障|状态)'
    r'|(?:排查|检查|诊断).{0,8}(?:工具|本轮|机器人|发送|回复|运行状态)', re.I)
FILE_REQUEST = re.compile(
    r'(?:生成|整理成|保存成|写成|导出|发给我|发我|发一下|发个|发送|下载).{0,14}'
    r'(?:文件|附件|文档|聊天记录|群聊记录|\b(?:txt|md|csv|markdown|pdf)\b)'
    r'|(?:文件|附件|文档|聊天记录|群聊记录).{0,8}(?:导出|发来|发给我|发我|下载)'
    r'|\b(?:files\.(?:list|send)|chat\.export)\b', re.I)


def context_tool_hints(context):
    """Use opportunities already identified during prompt assembly."""
    names = set()
    if context.get('own_diary_memory'):
        names.add('diary.read')
    recall = context.get('memory_recall_opportunity')
    if isinstance(recall, dict) and recall.get('tool') == 'memory.graph':
        names.add('memory.graph')
    if context.get('social_memory_opportunity'):
        names.add('memory.social')
    return names


def discoverable_names(payload, names, selected):
    if isinstance(payload.get('internal_review'), dict):
        return names & REVIEW
    return names - (INTENT_ONLY - selected)


def select_names(payload, names, base=None, *, hints=(), research=False):
    names = set(names)
    if isinstance(payload.get('internal_review'), dict):
        return names & REVIEW
    selected = names - OPTIONAL
    from hakimi.core.context_window import select_context
    rows, target = select_context(payload)
    from hakimi import paths
    from hakimi.voice.tts import offer_mode, offer_hum
    base = paths.base() if base is None else base
    if DRAW_TOOL in names and offer_drawing(payload, base):
        selected.add(DRAW_TOOL)
    if offer_mode(payload,base):
        selected.add('voice.speak')
    if offer_hum(payload,base):
        selected.add('voice.hum')
    if any(row.get('voice_ref') or 'voice.read message_id=' in str(row.get('text','')) for row in rows):
        selected.add('voice.read')
    elif target.get('reply_to') and (base/'state/voice.sqlite3').is_file():
        from hakimi.memory.voice_receipts import preview
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
    from hakimi.chat.turn_text import current_text
    from hakimi.memory.memory_lifecycle import social_signal
    from hakimi.memory.memory_recall import opportunity
    current = current_text(payload, base=base)
    if DIARY_EXPORT.search(current) and not NO_DIARY_EXPORT.search(current):
        selected.add('diary.export')
    if FILE_REQUEST.search(current):
        selected.update(FILES - {'diary.export'})
    if not research:
        selected.difference_update(ON_DEMAND)
        selected.update(ON_DEMAND.intersection(hints))
        if DIARY.search(current):
            selected.add('diary.read')
        recall = opportunity(dict(target, text=current), payload.get('session_key'))
        if GRAPH.search(current) or (recall and recall['tool'] == 'memory.graph'):
            selected.add('memory.graph')
        if SOCIAL.search(current) or social_signal(current):
            selected.add('memory.social')
        if RUNTIME.search(current):
            selected.add('runtime.status')
    return selected & names
