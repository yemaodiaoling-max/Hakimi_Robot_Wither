"""Identify requests to create an image from the active QQ conversation."""

import hashlib
import re

from hakimi.core.context_window import chronological_rows, select_context

TOOL = 'image.draw'
BRIEF_LIMIT = 280
INSTRUCTION = '按对方想画的主题，自己选场景、构图和一个贴合对方的有趣细节，用一句话构思并发图。'
COOLDOWN_NOTE = '绘图正在冷却。请用自己的口吻接对方的话，本轮无需再画或报恢复时间。'

_TEXT_MARKUP = re.compile(r'\[引用[^\n]*?"\]|\[[^\]\n]*\]|<[^>\n]*>|https?://\S+|`[^`\n]*`')
_QUOTED = re.compile(r'“[^”\n]*”|「[^」\n]*」|『[^』\n]*』|‘[^’\n]*’|"[^"\n]*"')
_CANCEL = re.compile(
    r'(?:别|不要|不用|不必|无需|不需要|不想要|不想看|取消|先不|算了.{0,3}不)'
    r'(?:帮我|你|再|用AI|给我|急着|让你|真的|画图工具|做)?\s*(?:画(?!面|风|家|室|成|得|框|笔)|绘|出图|作画|生图|生成|重画|发图)'
    r'|(?:只|就|还是)(?:用|说|回|聊|发)?(?:文字|打字|聊画画)|(?:只要|只想要)提示词'
    r"|\b(?:do not|don't|no need to)\s+(?:draw|generate|illustrate|make an image)", re.I)
_REPORTED = re.compile(r'^(?:我|他|她|它|别人|群友)'
                       r'(?:刚才|之前|昨天|说|在说|准备|试过|喜欢|自己|要学|正在|画过|画了|画的)|'
                       r'^(?:[\w]{1,12})(?:说过|问过|说了|写道|让我|叫我)[：:，,]|'
                       r'据说|举例|例如|比如说|原话是|假如|如果有人|有人(?:说|问|让|叫)|别人(?:说|问|让|叫)')
_DISCUSSION = re.compile(
    r'(?:怎么|如何|为啥|为什么|教我|教程|学会|学一下|学习|讨论|介绍|评价|点评|看看|看下)\s*(?:用AI|你|我|他|她|别人|自己|人家|先)?(?:是)?(?:怎么|如何)?(?:画|绘|出图|生图|生成图)'
    r'|(?:很|太|挺|真)(?:难|好看|有趣|有意思|厉害)(?:啊|吧|呢|了|呀)?[!?。！?？]?$'
    r'|^(?:画画|绘画|绘图|生图|AI绘画|画图)(?:是|这种|本来|也|有点|挺|真|很|太|的|时|会|需要|好玩|好难|怎么样|$)'
    r'|的(?:教程|方法|步骤|技巧|提示词|prompt)(?:$|有哪些|有吗|是什么|怎么|怎样|如何|是啥|推荐|在哪|哪里)'
    r'|(?:会|能|可以|支持)(?:不可以|不能|不会|不会用)?(?:画图|画画|绘画|绘图|生图|生成图片|出图)(?:吗|么|嘛|不|功能)?[。!?！？]*$'
    r'|(?:只想看|发我|给我发|找|搜索)(?:现成的|你画的|你以前画的|之前的)', re.I)
_VERB = (r'(?:重新画|重画|画(?!蛇添足|饼|了|过|间|风|面|家|质|廊|室|展|集|法|作|师|的|得|外|图软件)'
         r'|绘制|绘个|绘一|生(?:成)?图|(?:生成|出|做|整|弄|搓|搞)(?:出)?(?:\s*[一两几]?[张幅]|图)'
         r'|(?:生成|制作|设计|出|做|整|弄|搓|搞)(?=.{0,18}(?:图片|插图|插画|海报|头像|壁纸|封面|图标|logo|表情包)))')
_ASK = re.compile(
    rf'^(?:(?:哈基米|你|那就|那|来|麻烦|劳驾|请|请你|帮忙|帮我|给我|替我|能不能|可不可以|可以|'
    rf'能否|能|拜托|能帮我|想看你|我想看你|我想要你|我想看哈基米|我想让你|要不|要不要|试试|再|自己|来试试)[\s，,:：]*){{0,8}}'
    rf'{_VERB}(?:\s*点|\s*一下)?(?P<subject>[^。！？\n]*)', re.I)
_ENGLISH_ASK = re.compile(
    r'^(?:(?:please|can you|could you|would you|i want you to)\s+)*'
    r'(?:draw|paint|sketch|illustrate|redraw|generate\s+(?:an?\s+)?(?:image|picture)(?:\s+of)?)\s+\S', re.I)
_ADDITIONAL_ASK = re.compile(
    r'^(?:(?:哈基米|你|请|帮我|麻烦|给我|能不能|可以|可不可以)[\s：:]*){0,6}'
    r'(?:把.{1,26}画成.{1,45}|用.{1,18}(?:画|绘制).{1,45}'
    r'|给.{1,20}(?:设计|做|制作|画)(?:一?[个张幅]|些)?(?:图片|插画|海报|头像|壁纸|封面|图标|logo|表情包))'
    r'|^(?:我)?想(?:看|要)(?:一)?(?:张|幅)(?:(?:你|哈基米)(?:自己)?画的|原创的?).{1,40}', re.I)
_FOLLOWUP = re.compile(
    r'^(?:(?:哈基米|请|麻烦|那就|那|行|好|可以)[\s，,]*){0,8}'
    r'(?:(?:再来|来|继续|重来|再整|再出|再画)(?:一)?(?:张|幅)(?:图|画)?'
    r'|(?:换|改|调整)(?:成|为|个|种|一下).{1,45}|按这个(?:风格|构图)?(?:来|做|画)|就(?:画)?这个|自己画)'
    r'(?:吧|呀|呢|行吗|可以吗)?[。!！?？~～\s]*$')
_DRAWING_NAME = re.compile(r'_drawing_([a-f0-9]{64})\.(?:png|jpg|webp)')
_ART_QUESTION = re.compile(r'(?:想要|想画|怎么画|什么|哪种|哪个).{0,12}(?:画|风格|配色|构图|场景|色调|主题)')
_ART_SUBJECT = re.compile(r'^(?:哈基米\s*)?你(?:会|能)(?:画|绘制).{1,30}(?:吗|嘛|么)[！。!?？]*$')
_OFFLINE = re.compile(r'(?:别|不要|不用|禁止|不许|无需|勿)[^，,。！？!?；;\n]{0,5}(?:联网|上网)'
                      r'|(?:不联网|离线)(?:画|创作|生成)|\boffline only\b'
                      r"|\b(?:do not|don't|no need to)\s+(?:use|go on|connect to)\s+(?:the\s+)?(?:internet|online)", re.I)
_DRAW_INSTEAD = re.compile(r'(?:别|不要|不用)(?:搜|找).{0,8}(?:图|照片)[，,。！!\s]*(?:你)?(?:自己|直接)画')
_MERGED_CANCEL = re.compile(
    r'^(?:这(?:张|幅)(?:图|画)\s*)?(?:不画了|不出图了|不生成(?:图片|图)了|取消(?:了|吧)?|不用了)[。！!\s]*$')


def _plain(text):
    text = _TEXT_MARKUP.sub(' ', str(text)[:2400])
    text = re.sub(r'^[\s@＠]+(?=哈基米)', '', text)
    return text.strip()


def _kind(text):
    # Quoted material can be a subject (e.g. draw “a valley”), but a quote
    # by itself is never an instruction.
    text = _QUOTED.sub('某主题', _plain(text))
    if _CANCEL.search(text):
        return 'cancel'
    if not _REPORTED.search(text) and _DRAW_INSTEAD.search(text):
        return 'request'
    for sentence in re.split(r'[。！？!?；;\n]+', text):
        sentence = sentence.strip()
        if not sentence or _REPORTED.search(sentence):
            continue
        for clause in re.split(r'[，,]+', sentence):
            clause = clause.strip(' ：:')
            if not clause or _DISCUSSION.search(clause):
                continue
            if _FOLLOWUP.fullmatch(clause):
                return 'followup'
            match = _ASK.match(clause)
            if match and (match['subject'].strip() or re.search(r'请|帮|给我|能不能|画画|画图|出图|生图', clause)):
                return 'request'
            if _ENGLISH_ASK.match(clause) or _ADDITIONAL_ASK.match(clause):
                return 'request'
    return None


def _nearby(earlier, later):
    first, second = earlier.get('received_at'), later.get('received_at')
    return not (isinstance(first, (float, int)) and isinstance(second, (float, int))) or 0 <= second - first <= 120


def request_id(session, row):
    return hashlib.sha256('\0'.join((session, str(row['user_id']), str(row['message_id']))).encode()).hexdigest()


def _previous_request(rows, index, user, target, session):
    previous = rows[index - 1] if index else None
    if not previous or not _nearby(previous, target):
        return False
    drawing = None
    if previous.get('user_id') == 'SELF':
        text = str(previous.get('text', ''))
        media = previous.get('media')
        drawing = (_DRAWING_NAME.search(text) if text.startswith('[CQ:file,name=_drawing_') else None)
        if not drawing and isinstance(media, dict) and media.get('kind') == 'file':
            drawing = _DRAWING_NAME.fullmatch(str(media.get('name', '')))
        if not (index >= 2 and (drawing or _ART_QUESTION.search(text))):
            return False
        previous = rows[index - 2]
    if str(previous.get('user_id')) != user or not _nearby(previous, target):
        return False
    if drawing:
        return bool(previous.get('message_id') and drawing[1] == request_id(session, previous))
    return _kind(previous.get('text')) == 'request' or bool(_ART_SUBJECT.fullmatch(_plain(previous.get('text'))))


def _merged_sources(payload, session):
    """Use only the explicit source chain stored on a bridge follow-up."""
    ids = payload.get('pending_explicit_question_ids')
    context = payload.get('recent_context_messages')
    if (payload.get('wake_mode') != '即时唤醒(被@/私聊)' or payload.get('listening_window')
            or not isinstance(ids, list) or not 2 <= len(ids) <= 12
            or not isinstance(context, list) or not isinstance(session, str)):
        return []
    ids = [str(mid) for mid in ids]
    if (ids[-1] != str(payload.get('reply_target_message_id')) or len(set(ids)) != len(ids)
            or any(not re.fullmatch(r'-?[0-9]{1,20}', mid) for mid in ids)):
        return []
    selected = set(ids)
    sources = {}
    for row in context:
        if not isinstance(row, dict) or row.get('session_key') != session:
            continue
        mid = str(row.get('message_id'))
        if mid not in selected:
            continue
        if mid in sources:
            return []
        sources[mid] = row
    if set(sources) != selected:
        return []
    rows = [sources[mid] for mid in ids]
    author = str(rows[-1].get('user_id', ''))
    if (not re.fullmatch(r'[1-9][0-9]{0,19}', author)
            or (session.startswith('private:') and session != f'private:{author}')
            or any(str(row.get('user_id')) != author or str(row.get('self_id')) == author for row in rows)):
        return []
    return rows


def _merged_request(rows):
    request = None
    for row in rows:
        text = row.get('text')
        kind = _kind(text)
        if kind == 'cancel' or _MERGED_CANCEL.fullmatch(_plain(text)):
            request = None
        elif request is None and (
            kind == 'request' or (
                kind == 'followup' and row.get('image_paths')
                and re.search(r'(?:就画这个|按这个(?:风格|构图)?画)', _plain(text))
            )
        ):
            # Keep the first request ID until cancellation so retries and
            # overlapping workers share the same durable drawing receipt.
            request = row
    return request


def requested(payload):
    """Return the authoritative request row, or None. History grants no authority."""
    if (isinstance(payload.get('internal_review'), dict) or payload.get('internal_review')
            or payload.get('cold_topic_request') or payload.get('is_proactive')
            or payload.get('wake_mode') == '冷场主动抛话题'):
        return None
    session = payload.get('session_key')
    if not isinstance(session, str) or not re.fullmatch(r'(?:group|private):[1-9][0-9]{0,19}', session):
        return None
    if payload.get('merged_followup') is True:
        return _merged_request(_merged_sources(payload, session))
    _, target = select_context(payload)
    user = str(target.get('user_id', ''))
    message = str(target.get('message_id', ''))
    if not user or user == 'SELF' or user == str(target.get('self_id', '')) or not re.fullmatch(r'-?[0-9]{1,20}', message):
        return None
    if payload.get('reply_target_message_id') and not payload.get('listening_window'):
        if str(payload['reply_target_message_id']) != message:
            return None
    rows = chronological_rows([row for row in payload.get('recent_context_messages', [])
                               if isinstance(row, dict) and row.get('session_key', session) == session])
    index = next((index for index, row in enumerate(rows) if str(row.get('message_id', '')) == message), None)
    if index is None:
        return None
    kind = _kind(target.get('text'))
    attached = bool(target.get('image_paths') and re.search(r'(?:就画这个|按这个(?:风格|构图)?画)', _plain(target.get('text'))))
    if kind == 'request' or kind == 'followup' and (attached or _previous_request(rows, index, user, target, session)):
        # A direct cancellation appended by the requester wins over the anchor.
        for row in rows[index + 1:index + 4]:
            if str(row.get('user_id', '')) != user or not _nearby(target, row):
                break
            if _kind(row.get('text')) == 'cancel':
                return None
        return target
    return None


def availability(payload, base):
    target = requested(payload)
    if target is None or not allows_generation(payload, target):
        return None
    from hakimi.tools.drawing import DrawingError, public_settings
    from hakimi.tools.drawing_accounting import cooling_down
    try:
        config = public_settings(base)
    except DrawingError:
        return None
    if config.get('enabled') and config.get('has_api_key'):
        return 'cooldown' if cooling_down(base) else 'ready'
    return None


def offered(payload, base):
    return availability(payload, base) == 'ready'


def allows_generation(payload, target):
    if payload.get('allow_network') is False or payload.get('network_allowed') is False:
        return False
    if payload.get('merged_followup') is True:
        rows = _merged_sources(payload, payload.get('session_key', ''))
        return bool(rows) and not any(
            _OFFLINE.search(_QUOTED.sub(' ', _plain(row.get('text')))) for row in rows)
    return not _OFFLINE.search(_QUOTED.sub(' ', _plain(target.get('text'))))
