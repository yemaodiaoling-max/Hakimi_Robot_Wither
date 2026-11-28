"""Text-only continuation selection, separate from the reply model persona."""
import asyncio
import json
import logging
import re


def text_batch(rows, *, context=(), own_message_ids=(), focus_ids=(), include_self=False):
    self_ids={'SELF','9000000000000000001'} | {str(r['self_id']) for r in [*context,*rows] if r.get('self_id')}
    authors={str(r.get('message_id','')):str(r.get('user_id','')) for r in [*context,*rows]}
    authors.update((str(mid),'SELF') for mid in own_message_ids)
    result = []
    for row in rows:
        text = str(row.get("text", ""))
        text = re.sub(r"\[CQ:image,[^\]]*\]|\[(?:群友发图|表情包图片)[^\]]*\]", "", text).strip()
        if not text and focus_ids and (row.get('image_paths') or str(row.get('message_id')) in focus_ids):
            text='[图片]' if row.get('image_paths') else '[被@消息]'
        if not text or row.get("user_id") == "SELF" and not focus_ids and not include_self:
            continue
        if focus_ids:text=text[:1000]
        targets=row.get('direct_targets')
        if targets is None:targets=re.findall(r'\[CQ:at,qq=(\d+)\]',text)
        mentions=sorted({'SELF' if str(uid) in self_ids else str(uid) for uid in targets})
        reference=row.get('reply_to') or row.get('reply_to_message_id') or ''
        author=authors.get(str(reference)) or str(row.get('reply_user_id') or '')
        if author in self_ids:author='SELF'
        if mentions:audience='hakimi' if mentions==['SELF'] else 'mixed' if 'SELF' in mentions else 'other'
        else:audience='hakimi' if author=='SELF' else 'other' if author else 'unknown_reply' if reference else 'open'
        result.append({"message_id": str(row.get("message_id", "")),
                       "user_id": str(row.get("user_id", "")), "text": text,
                       "received_at": row.get("received_at"),
                       "name": str(row.get("nickname") or row.get("name") or ""),
                       "reply_to": str(reference), "reply_author": author, "mentions": mentions, "audience": audience})
    return result


COLUMNS = ('message_id', 'speaker', 'text', 'audience', 'reply_to', 'reply_author', 'mentions')
LEGEND = ('messages/previous_exchange每行按columns顺序排列，末尾空列已省略；speaker/reply_author/mentions用speakers别名'
          '（别名->[user_id,name]，SELF即哈基米），不在speakers中的保持原值')


def encode_messages(request):
    """Replace repeated per-row identity keys by one speakers table and columns+rows.

    Lossless: decode_messages() restores the original item dicts exactly. Message
    IDs (aliases such as '3' or 'h1') are untouched, so selections map back as before.
    """
    groups = [key for key in ('messages', 'previous_exchange') if request.get(key)]
    speakers, by_user = {}, {}
    for key in groups:
        for item in request[key]:
            uid, name = str(item.get('user_id', '')), str(item.get('name', ''))
            if (uid, name) in speakers or not (uid or name):
                continue
            alias = 'SELF' if uid == 'SELF' and 'SELF' not in speakers.values() else 'u' + str(len(speakers) + 1)
            speakers[(uid, name)] = alias
            by_user.setdefault(uid, alias)
    ref = lambda value: by_user.get(str(value), str(value))
    for key in groups:
        rows = []
        for item in request[key]:
            uid, name = str(item.get('user_id', '')), str(item.get('name', ''))
            cells = [item['message_id'], speakers.get((uid, name), ''), item['text'], item.get('audience', ''),
                     item.get('reply_to', ''), ref(item['reply_author']) if item.get('reply_author') else '',
                     [ref(value) for value in item.get('mentions', ())]]
            while cells and not cells[-1]:cells.pop()
            rows.append(cells)
        request[key] = rows
    if groups:
        request['speakers'] = {alias: [uid, name] if name else [uid] for (uid, name), alias in speakers.items()}
        request['columns'] = list(COLUMNS)
        request['format'] = LEGEND
    return request


def decode_messages(request, key='messages'):
    """Inverse of encode_messages for one group; used for verification and tests."""
    table = {alias: (value[0], value[1] if len(value) > 1 else '') for alias, value in request.get('speakers', {}).items()}
    users = {}
    for alias, (uid, _) in table.items():users.setdefault(uid, alias)
    originals = {alias: uid for uid, alias in users.items()}
    items = []
    for cells in request.get(key, ()):
        cell = dict(zip(request['columns'], cells))
        uid, name = table.get(cell.get('speaker', ''), ('', ''))
        item = {'user_id': uid, 'name': name, 'text': cell['text'], 'audience': cell.get('audience', ''),
                'message_id': cell['message_id']}
        if cell.get('reply_to'):item['reply_to'] = cell['reply_to']
        if cell.get('reply_author'):item['reply_author'] = originals.get(cell['reply_author'], cell['reply_author'])
        if cell.get('mentions'):item['mentions'] = [originals.get(v, v) for v in cell['mentions']]
        items.append({k: v for k, v in item.items() if v or k == 'message_id'})
    return items


def select_messages(rows, previous_reply, base, runner=None, *, context=(), own_message_ids=(), focus_ids=(), limit=None, timeout_seconds=30, history=()):
    batch = text_batch(rows,context=context,own_message_ids=own_message_ids,focus_ids=focus_ids)
    if not batch:
        return []
    # Short local message references remove repeated long platform IDs. Names and
    # message text stay intact; callers still receive the real platform IDs.
    aliases = {row['message_id']: str(index+1) for index, row in enumerate(batch)}
    originals = {short: mid for mid, short in aliases.items()}
    compact = []
    for row in batch:
        item = {key: row[key] for key in ('user_id', 'name', 'text', 'audience') if row.get(key)}
        item['message_id'] = aliases[row['message_id']]
        if row.get('reply_to'):item['reply_to'] = aliases.get(row['reply_to'], row['reply_to'])
        if row.get('reply_author'):item['reply_author'] = row['reply_author']
        if row.get('mentions'):item['mentions'] = row['mentions']
        compact.append(item)
    task = json.dumps({"task": "为哈基米挑选值得合并接话的消息。先确认对话对象，再判断话题相关性；SELF代表哈基米，明确@对象优先于引用作者。@其他人、引用回复其他人的消息通常属于群友之间交流，仅在内容同时明确邀请哈基米接话时选入。优先选择回应她上一条发言、向她提问或自然接她话的消息；同一话题或同一发言人继续说话只作辅助依据。mixed和unknown_reply结合正文、引用内容与称呼判断对象。返回JSON {selected_message_ids: [message_id, ...]}，没有合适消息时返回空列表。",
                       "previous_reply": str(previous_reply), "messages": compact}, ensure_ascii=False,separators=(',',':'))
    if focus_ids:
        task=json.dumps({'task':'为哈基米整理本轮被@提问的上下文。focus_message_ids是本轮明确提问，保留这些问题；其余选择能解释问题、补充条件、呈现提问人后续补充或还原引用与图片指代的消息，必要时选入SELF历史发言。候选按时间排列。给其他人的对话只在提供本题背景时选入。按相关性列出selected_message_ids，总计最多'+str(limit)+'条，相关内容较少时只选实际需要的条数。',
            'focus_message_ids':[aliases.get(str(mid),str(mid)) for mid in focus_ids],'previous_reply':str(previous_reply),'messages':compact},ensure_ascii=False,separators=(',',':'))
    request = json.loads(task)
    history_aliases = set()
    if history and not focus_ids:
        candidate_ids = {row['message_id'] for row in batch}
        background = text_batch([row for row in history if str(row.get('message_id', '')) not in candidate_ids],
                                context=[*context, *history], own_message_ids=own_message_ids, include_self=True)
        history_ids = {row['message_id']: f'h{index+1}' for index, row in enumerate(background)}
        history_aliases = set(history_ids.values())
        for item in request['messages']:
            if item.get('reply_to') in history_ids:
                item['reply_to'] = history_ids[item['reply_to']]
        exchange = []
        for row in background:
            item = {key: row[key] for key in ('user_id', 'name', 'text', 'audience') if row.get(key)}
            item['message_id'] = history_ids[row['message_id']]
            if row.get('reply_to'):
                item['reply_to'] = history_ids.get(row['reply_to'], aliases.get(row['reply_to'], row['reply_to']))
            if row.get('reply_author'):item['reply_author'] = row['reply_author']
            if row.get('mentions'):item['mentions'] = row['mentions']
            exchange.append(item)
        if exchange:
            request['previous_exchange'] = exchange
            if any(row['user_id'] == 'SELF' for row in background):
                request.pop('previous_reply', None)  # Actual delivered segments already carry the answer.
    request['output_format'] = {'selected_message_ids': ['message_id']}
    request['task'] += ' 只返回符合output_format的JSON对象，selected_message_ids是消息编号字符串数组；不要返回正文或其他字段。'
    task = json.dumps(encode_messages(request), ensure_ascii=False, separators=(',', ':'))
    if runner is None:
        runner = lambda task, base: run_judge(task, base, timeout_seconds=timeout_seconds,
                                             thinking="off" if focus_ids else "low")
    raw = runner(task, base)
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
        raw = json.loads(raw)
    chosen = raw.get("selected_message_ids") if isinstance(raw, dict) else None
    if not isinstance(chosen, list):
        raise ValueError("JUDGE_INVALID_SELECTION")
    ranked=list(dict.fromkeys(originals.get(str(mid), str(mid)) for mid in chosen if str(mid) not in history_aliases))
    ids = set(ranked)
    allowed = {row["message_id"] for row in batch}
    if not ids <= allowed:
        raise ValueError("JUDGE_UNKNOWN_MESSAGE")
    if limit is not None:
        ids=set(list(dict.fromkeys([str(mid) for mid in focus_ids]+ranked))[:limit])
    return [str(row.get("message_id", "")) for row in rows if str(row.get("message_id", "")) in ids]


_review_task = None
MENTION_TIMEOUT_SECONDS = 30
CONTINUATION_TIMEOUT_SECONDS = 30


def _report_failure(base, operation, exc):
    try:
        from hakimi.features.codex_help import report_fault
    except ImportError:
        logging.getLogger(__name__).warning("%s: %s", operation, exc)
    else:
        report_fault(base, operation, exc)


def _consume_task_error(task):
    # Entries may disappear while their worker finishes. Retrieve every exception.
    if not task.cancelled():
        task.exception()


def _start_review(*args, **kwargs):
    global _review_task
    if _review_task is not None and not _review_task.done():
        return None
    task = asyncio.create_task(asyncio.to_thread(select_messages, *args, **kwargs))
    task.add_done_callback(_consume_task_error)
    _review_task = task
    return task


def _mention_fallback(rows, anchors):
    focus = {str(r['message_id']) for r in anchors}
    authors = {str(r.get('user_id')) for r in anchors}
    references = {str(r.get('reply_to') or r.get('reply_to_message_id') or '') for r in anchors}
    times = [float(r.get('received_at') or 0) for r in anchors]
    nearby = [r for r in rows if str(r.get('message_id')) not in focus and
              (str(r.get('message_id')) in references or
               (str(r.get('user_id')) in authors and
                any(abs(float(r.get('received_at') or 0)-at) <= 120 for at in times)))]
    chosen = focus | {str(r.get('message_id')) for r in nearby[-3:]}
    return [r for r in rows if str(r.get('message_id')) in chosen]


def _continuation_fallback(rows, *, own_message_ids=()):
    batch = text_batch(rows, own_message_ids=own_message_ids)
    return [row['message_id'] for row in batch if row.get('audience') == 'hakimi']


def mention_context(entry,rows,anchors,base,*,previous_reply='',own_message_ids=(),on_ready=None):
    """Poll a globally serialized review with an bounded subprocess budget."""
    from hakimi.models import aux_config
    if aux_config.has_config(base) and aux_config.context_judge_config(base).get('mode') == 'heuristic':
        from hakimi.core.context_window import chronological_rows
        by_id = {str(r['message_id']): dict(r) for r in [*rows[-80:], *anchors]}
        candidates = chronological_rows(list(by_id.values()))
        return _mention_fallback(candidates, anchors)
    focus=[str(r['message_id']) for r in anchors]
    review=entry.get('mention_review')
    if review is None:
        from hakimi.core.context_window import chronological_rows
        by_id={str(r['message_id']):dict(r) for r in [*rows[-80:],*anchors]}
        candidates=chronological_rows(list(by_id.values()))
        task=_start_review(candidates,previous_reply,base,
            context=candidates,own_message_ids=own_message_ids,focus_ids=focus,limit=12,
            timeout_seconds=MENTION_TIMEOUT_SECONDS)
        if task is None:return None
        if on_ready is not None:task.add_done_callback(lambda _: on_ready())
        entry['mention_review']={'task':task,'focus_ids':focus,'rows':candidates,
            'through':max((float(r.get('received_at',0)) for r in candidates),default=0)}
        return None
    if not review['task'].done():return None
    # A new explicit question arriving during refinement is preserved immediately.
    candidates = {str(r.get('message_id')):r for r in [*review['rows'], *anchors]}
    current_rows = sorted(candidates.values(), key=lambda r: float(r.get('received_at',0)))
    try:selected=review['task'].result()
    except Exception as exc:
        _report_failure(base,'mention_context_review',exc)
        return _mention_fallback(current_rows,anchors)
    chosen=set(selected) | set(focus)
    return [r for r in current_rows if str(r.get('message_id','')) in chosen]


def previous_exchange(base, session, source_ids, trigger_id, context=()):
    """Resolve only the preceding offered messages and confirmed sent reply.

    The checkpoint stores IDs; original text stays in the existing chat logs.
    Rejected batches therefore cannot erase the preceding exchange.
    """
    if not session or not (source_ids or trigger_id):
        return []
    from pathlib import Path
    from hakimi.core.jsonio import tail_jsonl
    wanted = {str(mid) for mid in source_ids}
    history = {}
    rows = [*tail_jsonl(Path(base)/'state/chat_history.jsonl', count=1536, max_bytes=1048576), *context]
    for row in rows:
        mid = str(row.get('message_id', ''))
        if row.get('session_key') == session and mid in wanted and row.get('user_id') != 'SELF':
            history[mid] = dict(row)
    for index, row in enumerate(tail_jsonl(Path(base)/'state/sent_messages.jsonl', count=256, max_bytes=1048576)):
        if trigger_id and row.get('session_key') == session and row.get('trigger_id') == trigger_id:
            mid = str(row.get('message_id') or f'sent-{index}')
            history[mid] = dict(row, message_id=mid, user_id='SELF', nickname='哈基米',
                                received_at=row.get('at', 0))
    return sorted(history.values(), key=lambda row: float(row.get('received_at') or 0))


def continuation_context(entry,rows,previous_reply,base,*,context=(),own_message_ids=(),source_message_ids=(),reply_trigger_id="",session_key=""):
    """Return None while busy, then selected IDs; failures return an empty list.

    Call only once the listening window closes. The first call freezes candidates;
    subsequent polls must use the same entry until its result is handled.
    """
    from hakimi.models import aux_config
    if aux_config.has_config(base) and aux_config.context_judge_config(base).get('mode') == 'heuristic':
        return _continuation_fallback(rows, own_message_ids=own_message_ids)
    review=entry.get('continuation_review')
    if review is None:
        candidates=[dict(r) for r in rows]
        history = previous_exchange(base, session_key, source_message_ids, reply_trigger_id, context)
        task=_start_review(candidates,previous_reply,base,context=context,
            own_message_ids=own_message_ids,timeout_seconds=CONTINUATION_TIMEOUT_SECONDS,history=history)
        if task is None:return None
        entry['continuation_review']={'task':task,'rows':candidates}
        return None
    if not review['task'].done():return None
    try:return review['task'].result()
    except Exception as exc:
        review['failed'] = True
        _report_failure(base,'continuation_context_review',exc)
        return []


def run_judge(task, base, timeout_seconds=30, *, thinking="low"):
    from hakimi.models import aux_config
    if aux_config.has_config(base):
        cfg = aux_config.context_judge_config(base)
        mode = cfg.get('mode')
        if mode == 'main_gateway':
            from hakimi.models import gateway_text
            from hakimi.models.dsh_text_judge import parse_selection
            res = gateway_text.complete_via_gateway(
                base, task, tier=cfg.get('model') or 'gemini-4-hydrogen',
                json_mode=True, max_tokens=2048, timeout=timeout_seconds,
            )
            return parse_selection(res['text'])
        if mode == 'custom_api':
            from hakimi.models import gateway_text
            from hakimi.models.dsh_text_judge import parse_selection
            res = gateway_text.complete_via_http_api(
                cfg, [{'role': 'user', 'content': task}],
                json_mode=True, max_tokens=2048, timeout=timeout_seconds,
            )
            return parse_selection(res['text'])
    from hakimi.models.dsh_text_judge import run_judge as invoke
    return invoke(task, base, timeout_seconds=timeout_seconds, thinking=thinking)
