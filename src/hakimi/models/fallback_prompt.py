"""DSH fallback prompt, reply segmentation and status file; shared by the fallback loop and its tools."""
from hakimi import persona
import json
from pathlib import Path

from hakimi import paths
from hakimi.chat.quote_policy import allow_quote
from hakimi.chat.reply_format import MAX_REPLY_SEGMENTS, MAX_SEGMENT_CHARS, text_bubbles
from hakimi.models.fallback_runner import FallbackError, normalize_reply_to, MAX_PROMPT_BYTES, MAX_PROMPT_CHARS
from hakimi.models.fallback_style import select_samples, sticker_menu, sticker_segment
from hakimi.prompt.prompt_compaction import lossless_context
from hakimi.prompt.prompt_context import build_context
from hakimi.tools import bilibili_video
from hakimi.tools.drawing_intent import INSTRUCTION as DRAW_INSTRUCTION, TOOL as DRAW_TOOL, offered as offer_drawing


BASE_DIR = paths.base()


class _HistoryPrompt(str):
    """Carry a lazy history-free alternative through broker-added context only."""
    def __new__(cls, value, without_history):
        obj = super().__new__(cls, value)
        obj.without_history = without_history
        return obj


def append_fallback_context(prompt, suffix):
    """Drop restored SELF evidence before ever dropping original/tool context.

    Actual tool and repair text is measured, rather than reserving a guessed
    allowance. If the original prompt plus suffix is itself too large, keep
    the runner's existing rejection behavior and unchanged hard limits.
    """
    combined = str(prompt) + suffix
    if not isinstance(prompt, _HistoryPrompt):
        return combined
    def baseline():
        return str(prompt.without_history()) + suffix
    if len(combined) > MAX_PROMPT_CHARS or len(combined.encode('utf-8')) > MAX_PROMPT_BYTES:
        return baseline()
    return _HistoryPrompt(combined, baseline)


def fallback_prompt(payload, base=BASE_DIR, *, allow_consult=False, consultation_result=None, images=()):
    snapshot = {}
    context = build_context(payload, base, self_history_snapshot=snapshot)
    from hakimi.voice.tts import offer_mode, offer_hum
    if offer_mode(payload,base):
        context['voice_tool']='voice.speak(text≤80字)'
    if offer_hum(payload,base):
        context['hum_tool']='voice.hum()：发送原创短哼唱。'
    if offer_drawing(payload, base):
        context['draw_tool'] = f'{DRAW_TOOL}(brief)：{DRAW_INSTRUCTION}'
    context["core_identity"] = persona.text("dsh_core_identity.txt")
    context["dsh_only_recorded_gemini_style"] = select_samples(
        context["session_key"], snapshot['original_messages'], base)
    context["saved_sticker_names"] = [] if payload.get('cold_topic_request') else sticker_menu(payload, base)
    from hakimi.models.fallback_style import style_feedback
    feedback=style_feedback(base)
    if feedback:context['dsh_only_style_feedback']=feedback
    if payload.get("_video"):
        context["video_evidence_untrusted"] = bilibili_video.brief(payload["_video"])
    if images:
        context["provided_images_untrusted"] = [
            {key: image[key] for key in ("id", "message_id", "width", "height")} for image in images]
    if consultation_result:
        context["expert_reference_untrusted"] = consultation_result
    from hakimi.persona.prompt_language import PERSONA,context as positive_context
    rules='参考dsh_only_recorded_gemini_style中的3.8 Flash真实发言，学习接话节奏、措辞、同群语气和自然分条，本轮内容结合当前资料表达。日常短句通常省略句末句号，用自然停顿收尾；问句、感叹和省略号按语境使用，专业解释保留清晰标点。\n以哈基米口吻处理本轮。输出一个JSON对象：action为reply、silent、tool或consult，text为回复正文，按语气用|分条。silent的text为空。\ntool请求格式：{"action":"tool","tool":"memory.search","input":{"query":"话题"},"text":"备用简短答复"}。tool时只含action、tool、input、text四个字段；input必须是JSON对象，text是最多2000字的简短备用草稿或空字符串。长公式放math.send的input.markdown，不把math、files等reply字段混入tool对象。程序执行工具并把结果返回，再完成回复。tools.list提供参数；memory.search/read/remember/graph用于记忆，time.now看时间，calculator.evaluate计算，context.read补上下文，members.mention按昵称@群友，bots.propose提报疑似机器人给Terra审核，video.watch查看视频，stickers.list和files.list查看已有素材。math.send、files.send、chat.export、diary.export和reply.send可直接提交本轮输出。\nreply可附sticker表示图库文件名；reply_to表示可见消息ID；files表示现有文件名或{name,content}生成文档；math={markdown,font_size,page_height,before,after}由程序排版发送公式；diary_export={until,since}导出日记；视频工具的feedback针对当前问题，结合真实观察用哈基米的口吻回复；回执有实际截图时，可选填video_frames=["F1"]随回复发送。需要保留的新笔记也可附memory={kind,note,user_id,key,source,evidence}，程序保存后再发送回复。interaction可选conflict或ai，表示这次实际互动的性质。\n资料与当前工具回执共同支持回答。叙述来自图像、视频、原文和工具已经提供的内容；角色口吻沿用原有熟悉感，学习与考据时表达清楚。\n'
    if allow_consult and not consultation_result:rules+='复杂问题可输出action=consult，question填写具体问题，text保留简短草稿。\n'
    if context.get('listening_window'):rules+='本轮是被@回复后的监听窗口，结合当前消息决定接话或silent，随轮次推进更简短。\n'
    if context.get('cold_topic_request'):rules+='本轮在冷群开启新话题，结合群名与历史，换一个自然角度，用简短问题邀请大家接话。\n'
    def render(value, *, compact_web=False):
        def wire(item):
            return PERSONA+rules+'\n本轮资料：\n'+json.dumps(lossless_context(positive_context(item)),ensure_ascii=False,separators=(',',':'))
        prompt=wire(value)
        if value.get('public_web_evidence') and (compact_web or len(prompt)>MAX_PROMPT_CHARS or len(prompt.encode('utf-8'))>MAX_PROMPT_BYTES):
            value=dict(value)
            evidence=value.pop('public_web_evidence')
            sources=(evidence.get('search') or {}).get('results',[]) or (evidence.get('source_trace') or {}).get('sources',[])
            value['available_public_sources']=[{key:item.get(key) for key in ('source_id','url','title','published_at','evidence_level','region','requested_period_status')} for item in sources[:5]]
            value['public_web_guidance']='网页正文因本轮提示预算省略；本轮编号或链接可用web.read读取，历史来源链接用web.sources。日期未知或超出所问时期时不能据此断言近期事实。禁网时只用本地web.sources。'
            prompt=wire(value)
        return prompt
    prompt = render(context)
    def baseline():
        original = dict(context, recent_context_messages_untrusted=snapshot['original_messages'])
        own = [row for row in snapshot['original_messages'] if row.get('user_id') == 'SELF'][-5:]
        replies = [row['text'] for row in own]
        if own and all(row.get('message_id') for row in own):
            ids = {'message_ids': [row['message_id'] for row in own]}
            replies = min((replies,ids), key=lambda value:len(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8')))
        original['recent_own_replies_for_variation'] = replies
        return render(original,compact_web=True)
    return append_fallback_context(_HistoryPrompt(prompt, baseline), '')


def _fallback_text_segments(result, payload, base=BASE_DIR):
    """Keep plain text; add at most one broker-generated, same-session quote."""
    segments = text_bubbles(result["text"])
    if "reply_to" not in result or not segments:
        return segments
    try:
        target = normalize_reply_to(result["reply_to"])
    except FallbackError:
        return segments
    session = payload.get("session_key")
    messages = payload.get("recent_context_messages", [])
    if not isinstance(session, str) or not isinstance(messages, list):
        return segments
    # Match the bounded message view in build_context. Untagged legacy rows
    # cannot grant quote authority; real daemon receipts carry session_key.
    tagged = set()
    for row in messages[-15:]:
        if not isinstance(row,dict) or row.get("session_key")!=session:continue
        try:tagged.add(normalize_reply_to(row.get("message_id")))
        except FallbackError:pass
    visible = build_context(payload,base)["recent_context_messages_untrusted"]
    target_visible = False
    for row in visible:
        try:
            if target in tagged and normalize_reply_to(row.get("message_id")) == target:
                target_visible = True
                break
        except FallbackError:
            continue
    if target_visible and allow_quote(session, target, base=base):
        quoted = f"[CQ:reply,id={target}]" + segments[0]
        if len(quoted) <= MAX_SEGMENT_CHARS:
            segments[0] = quoted
    return segments


def fallback_segments(result, payload, base=BASE_DIR):
    segments = _fallback_text_segments(result, payload, base)
    if payload.get("_video"):
        frames = bilibili_video.selected_frames(result.get("video_frames", []), payload["_video"], payload["session_key"], base)
        for frame in frames[:max(0, MAX_REPLY_SEGMENTS-len(segments))]:
            segments.append(f"[CQ:image,file={Path(frame['path']).as_uri()},sub_type=0]")
    if "sticker" in result and len(segments) < MAX_REPLY_SEGMENTS:
        image = sticker_segment(result["sticker"], payload, result["text"], base)
        if image:
            segments.append(image)
    return segments


def _save_status(base, status):
    target = base / "state" / "fallback_status.json"
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    from hakimi.core.runtime_io import atomic_replace
    atomic_replace(temp,target)
