"""Build and validate QQ synthesis from time-linked video observations."""
import json
import math
from pathlib import Path
import re
import subprocess

from .live import _json_response


SUMMARY_ERROR_CODES = frozenset({'summary_response_invalid','invalid_summary_sources',
    'invalid_overview','overview_missing_timeline','invalid_chapters','invalid_chapter',
    'summary_input_too_large','summary_deadline','summary_call_failed',
    'hydrogen_primary_model_mismatch','hydrogen_primary_content_invalid'})


class SummaryValidationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def validate_summary(response, segments, coverage_status):
    try:
        value = _json_response(response)
    except (ValueError,TypeError,KeyError,AttributeError):
        raise SummaryValidationError('summary_response_invalid') from None
    overview=value.get('overview');refs=value.get('segment_indices');chapters=value.get('chapters')
    def referenced(indices):
        if type(indices) is not list or not indices or any(type(i) is not int or not 0<=i<len(segments) for i in indices):
            raise SummaryValidationError('invalid_summary_sources')
        return list(dict.fromkeys(indices))
    refs=referenced(refs)
    if type(overview) is not str or not overview.strip() or len(overview)>1500:
        raise SummaryValidationError('invalid_overview')
    if coverage_status=='complete' and len(segments)>=3:
        if 0 not in refs or len(segments)-1 not in refs or not any(len(segments)//3<=i<=2*len(segments)//3 for i in refs):
            raise SummaryValidationError('overview_missing_timeline')
    if type(chapters) is not list or not 1<=len(chapters)<=5:
        raise SummaryValidationError('invalid_chapters')
    output=[]
    for chapter in chapters:
        if type(chapter) is not dict:raise SummaryValidationError('invalid_chapter')
        indices=referenced(chapter.get('segment_indices'))
        text=chapter.get('summary')
        if type(text) is not str or not text.strip() or len(text)>200:
            raise SummaryValidationError('invalid_chapter')
        output.append({'start':min(segments[i]['start'] for i in indices),
            'end':max(segments[i]['end'] for i in indices),'summary':text,
            'evidence_ids':list(dict.fromkeys(e for i in indices for e in segments[i]['evidence_ids']))})
    evidence_ids=list(dict.fromkeys(e for i in refs for e in segments[i]['evidence_ids']))
    return dict(overview=overview.strip(),overview_evidence_ids=evidence_ids,chapters=output,synthesis_status='completed')


FEEDBACK_REVISION = 'qq-contextual-video-notes-v5'
MAX_FRAME_BYTES = 384 * 1024
FEEDBACK_INSTRUCTION = (
    '你为 QQ 哈基米写视频摘记。读 question 和当轮对话，理解他们指的对象、关心的细节。'
    '把和当轮话题相关的画面、台词与声音整理成供哈基米参考的 video_notes；复杂内容可按时间分段。'
    '有证据的关键步骤、数字、例子及结果都应保留，简单片段可简写。串起分散的观察，例如谁在做什么、某句话出现时画面如何变化、声音在动作之前还是之后。'
    '只写与话题有关且 observations 支持的内容，必要时写大致时间。聊天与标题只用于理解话题。'
    '群友要判断或建议时，写下能帮助哈基米判断的视频事实，由她决定怎样接话。'
    '摘记使用客观叙述，不称呼群友、不代写答复或给建议。'
    'context 的 sequence 是发言顺序，reply_to 指向引用；current_question 和 video_source 标出提问和分享。'
    'qq_prior_messages 是 QQ 随提及附带的前文，quotes 是引用，未标明的作者保持未知。'
    'native-video-audio 是直接听到的原声；local-wenet-transcript 只支持识别出的台词。'
    '画面和聊天不能证实声音。结合 missing_modalities、unobserved_intervals 与实际观察交代重要缺口。'
    '只返回 JSON：{"status":"extracted","video_notes":"摘记正文","evidence_indices":[0],"screenshot":null}。'
    'evidence_indices 引用支撑摘记或截图的观察，至多八条。只有部分依据时 status 填 insufficient_evidence；'
    '完全没有相关依据时，使用该状态、空编号和空正文。'
    '通常只回文字。若某一帧能补充文字或群友想看具体画面，且 allow_screenshot 为真，'
    'screenshot 可填 {"evidence_index":0,"timestamp":12.5}，取已引用画面时段内有代表性的一刻。'
)


def _feedback_excerpt(row, *, quoted=False):
    item = {}
    for key, size in (('speaker', 60), ('text', 120 if quoted else 240)):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            item[key] = value.strip()[:size]
    if row.get('same_text_as') == '哈基米在本群的已发送回执':
        item['same_text_as'] = '哈基米在本群的已发送回执'
    attachments = row.get('attachments')
    if isinstance(attachments, list):
        item['attachments'] = [label.strip()[:100] for label in attachments[:2]
                               if isinstance(label, str) and label.strip()]
    quotes = row.get('quotes')
    if not quoted and isinstance(quotes, list):
        item['quotes'] = [_feedback_excerpt(quote, quoted=True) for quote in quotes[:2] if isinstance(quote, dict)]
    return item


def normalize_feedback(value):
    if not isinstance(value, dict):
        return None
    question = value.get('question')
    if not isinstance(question, str) or not question.strip():
        return None
    request = {'question': question.strip()[:1200], 'context': []}
    context = value.get('context')
    if isinstance(context, list):
        for row in context[:12]:
            if isinstance(row, str) and row.strip() and len(request['context']) < 3:
                request['context'].append(row.strip()[:350])
                continue
            if not isinstance(row, dict):
                continue
            position = row.get('sequence')
            if type(position) is not int or not 1 <= position <= 12:
                continue
            item = dict(sequence=position)
            for key, limit in (('speaker', 80), ('text', 280)):
                text = row.get(key)
                if isinstance(text, str) and text.strip():
                    item[key] = text.strip()[:limit]
            for key in ('current_question', 'video_source'):
                if row.get(key) is True:
                    item[key] = True
            referred = row.get('reply_to')
            if type(referred) is int and 1 <= referred < position:
                item['reply_to'] = referred
            if len(item) > 1:
                request['context'].append(item)
    excerpt = value.get('qq_prior_messages')
    if isinstance(excerpt, list) and excerpt:
        request['qq_prior_messages'] = [_feedback_excerpt(row) for row in excerpt[-10:] if isinstance(row, dict)]
    return request


def feedback_data(report, request):
    evidence = [item for item in report['evidence'] if isinstance(item, dict)]
    data = dict(request, title=str(report.get('title', ''))[:250], duration=report['duration'],
                coverage_status=report['coverage_status'],
                missing_modalities=[kind for kind in ('audio', 'video', 'subtitle')
                                    if kind in (report.get('missing_modalities') or ())],
                unobserved_intervals=report.get('unobserved_intervals', [])[:100],
                summaries=[dict(start=s['start'], end=s['end'], summary=s['text'][:400])
                           for s in report.get('summaries', [])[:100]], observations=[])
    def select(rows, maximum):
        if len(rows) <= maximum:
            return rows
        return [rows[round(i * (len(rows) - 1) / (maximum - 1))] for i in range(maximum)]

    def observations(selected, chars):
        result = []
        for index, item in enumerate(selected):
            observation = dict(index=index, start=item['media_start'], end=item['media_end'],
                               modality=item['modality'], text=item['text'][:chars])
            version = str(item.get('version', ''))
            for source in ('local-timestamped-frames', 'static-.2fps-low', 'local-wenet-transcript',
                           'native-video-audio', 'native-video-frames'):
                if f':{source}:' in version:
                    observation['source'] = source
                    break
            result.append(observation)
        return result

    def fits():
        return len(json.dumps(data, ensure_ascii=False).encode()) <= 22000

    # Keep observations distributed across the whole timeline as the request approaches its cap.
    selected = select(evidence, 70)
    data['observations'] = observations(selected, 180)
    if fits():
        return data, selected
    for summary in data['summaries']:
        summary['summary'] = summary['summary'][:100]
    for maximum in (180, 120, 60, 30):
        data['observations'] = observations(selected, maximum)
        if fits():
            return data, selected
    data['summaries'] = []
    if fits():
        return data, selected
    if len(data['unobserved_intervals']) > 30:
        data['unobserved_intervals'] = select(data['unobserved_intervals'], 30)
        data['unobserved_intervals_truncated'] = True
    for maximum in (70, 50, 35, 20):
        selected = select(evidence, maximum)
        data['observations'] = observations(selected, 30)
        if fits():
            return data, selected
    raise SummaryValidationError('summary_input_too_large')


def validate_feedback(response, evidence, *, allow_screenshot):
    try:
        value = _json_response(response)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise SummaryValidationError('summary_response_invalid') from None
    status = value.get('status')
    indices = value.get('evidence_indices')
    notes = value.get('video_notes')
    if (set(value) - {'status', 'video_notes', 'evidence_indices', 'screenshot'}
            or status not in ('extracted', 'insufficient_evidence')
            or not isinstance(notes, str) or len(notes) > 1200):
        raise SummaryValidationError('invalid_feedback')
    notes = notes.strip()
    if (not isinstance(indices, list) or len(indices) > 8 or (status == 'extracted' and not notes)
            or bool(notes) != bool(indices)
            or any(type(i) is not int or not 0 <= i < len(evidence) for i in indices)):
        raise SummaryValidationError('invalid_summary_sources')
    feedback = dict(status=status, video_notes=notes, evidence_ids=list(dict.fromkeys(evidence[i]['id'] for i in indices)))
    shot = value.get('screenshot')
    if allow_screenshot and isinstance(shot, dict):
        index, timestamp = shot.get('evidence_index'), shot.get('timestamp')
        if type(index) is int and index in indices and type(timestamp) in (int, float):
            item = evidence[index]
            if item['modality'] in ('video', 'mixed') and item['media_start'] <= timestamp <= item['media_end']:
                feedback['screenshot'] = dict(id='F1', timestamp=round(timestamp, 3), evidence_id=item['id'])
    return feedback


def extract_feedback_frame(source, input_options, shot, evidence, destination, *, ffmpeg='ffmpeg', timeout=8):
    """Decode the first actual source frame at/after the requested timestamp."""
    timestamp = shot['timestamp']
    fact = next(item for item in evidence if item['id'] == shot['evidence_id'])
    result = subprocess.run(
        [str(ffmpeg), '-nostdin', '-hide_banner', '-loglevel', 'info', *input_options,
         '-ss', str(timestamp), '-i', str(source), '-map', '0:v:0', '-an', '-sn',
         '-vf', 'showinfo,scale=640:640:force_original_aspect_ratio=decrease:force_divisible_by=2',
         '-frames:v', '1', '-threads', '1', '-c:v', 'mjpeg', '-q:v', '4', '-f', 'image2pipe', 'pipe:1'],
        capture_output=True, timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    frame = result.stdout
    match = re.search(rb'\bn:\s*0\s+pts:.*?\bpts_time:([0-9.eE+\-]+)', result.stderr)
    if (result.returncode or not match or not 16 <= len(frame) <= MAX_FRAME_BYTES
            or not frame.startswith(b'\xff\xd8') or not frame.endswith(b'\xff\xd9')):
        return None
    actual = timestamp + float(match[1])
    if not math.isfinite(actual) or not fact['media_start'] <= actual <= fact['media_end']:
        return None
    with Path(destination).open('xb') as stream:
        stream.write(frame)
    return dict(shot, timestamp=round(actual, 3))
