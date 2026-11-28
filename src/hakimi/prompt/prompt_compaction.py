"""Loss-conscious presentation compression; receipts and authorization stay unchanged."""
import base64
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from hakimi.core.media_codes import compact_media, image_refs, trim_quotes

REVISION = "2026-11-28-stable-media-codes-v4"
_CODE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`)")
_IMAGE = re.compile(r"\[(群友发图|表情包图片)\(可用view_file看图\):\s*([^\]\n]+)\]")
_SOURCE = re.compile(r"^\s*(?:def |class |import |from \S+ import |if .+:|for .+:|while .+:|const |let |var |function |SELECT |curl |python |pwsh |powershell )", re.M)


def clean_text(text, image_paths=()):
    """Strip transport noise outside code. Never summarize or deduplicate human messages.

    Cached images become stable ``#code`` handles (hakimi.core.media_codes);
    tools resolve a code only against the daemon's authorized image list.
    """
    if _SOURCE.search(text):
        # Unfenced source code is still source code; only private cache paths
        # are replaced so they never reach the model.
        return _IMAGE.sub(lambda match: compact_media(match[0], image_paths, technical=True), text)
    technical = bool(re.search(r"CQ码|CQ 码|协议|base64|正则|原样|源码|解码|编码|JSON|XML",text,re.I))
    def plain(part):
        part = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u200b\ufeff\u2060]", "", part)
        # Image markers always; raw CQ media, cards and tracking URLs unless the
        # human is discussing the protocol itself.
        part = compact_media(part, image_paths, technical=technical)
        if not technical:
            def face(match):
                tag=match[0]
                face_id=re.search(r'faceId=["\']?([0-9]+)',tag)
                ext=re.search(r'ext="([^"<>]{0,4096})"',tag)
                label=""
                if ext:
                    try:
                        decoded=json.loads(base64.b64decode(ext[1],validate=True))
                        label=decoded.get("text","") if isinstance(decoded,dict) else ""
                    except (ValueError,UnicodeError):pass
                label=label[:120] if isinstance(label,str) else ""
                return "[表情:"+label+"]" if label else "[QQ表情"+(":"+face_id[1] if face_id else "")+"]"
            part=re.sub(r"<faceType=[^<>\n]{1,5000}>",face,part)
            part=re.sub(r"\[CQ:at,qq=([0-9]+|all)[^\]]*\]",lambda m:"@"+("全体" if m[1]=="all" else m[1]),part)
            part=re.sub(r"\[CQ:reply,id=(-?[0-9]+)[^\]]*\]",r"[引用:\1]",part)
            part=re.sub(r"\[CQ:face,id=([0-9]+)[^\]]*\]",r"[QQ表情:\1]",part)
            part=re.sub(r"\[CQ:forward,id=([0-9]+)(?:,[^\]]*)?(?:\]|$)",r"[合并转发:\1]",part)
            repeated=re.compile(r"(\[(?:QQ表情[^\]]*|表情:[^\]]*)\])(?:[ \t]*\1){3,}")
            part=repeated.sub(lambda m:m[1]+"×"+str(m[0].count(m[1])),part)
        part=re.sub(r"[ \t]+(?=\r?\n)","",part)
        return re.sub(r"(?:\r?\n){4,}","\n\n",part)
    return "".join(part if i%2 else plain(part) for i,part in enumerate(_CODE.split(text))).strip("\r\n").rstrip(" \t")


def compact_context(context, *, fallback=False):
    """Wire representation only. Preserve the canonical context for tools and policy."""
    result=dict(context)
    for key in ("core_identity","variation_rule","recent_own_replies_for_variation"):
        result.pop(key,None)  # The unchanged style rules and SELF rows already carry these.
    # One copy of each dynamic opportunity: the footer carries its actionable
    # guidance, while this object carries evidence and scores unchanged.
    opportunity=result.get("memory_opportunity")
    if isinstance(opportunity,dict):
        opportunity=dict(opportunity)
        if isinstance(result.get("social_memory_opportunity"),dict):
            opportunity.pop("social",None)
            opportunity.pop("guidance",None)
        result["memory_opportunity"]=opportunity
    social=result.get("social_memory_opportunity")
    if isinstance(social,dict):
        social=dict(social);social.pop("guidance",None)
        result["social_memory_opportunity"]=social
    from hakimi.memory.memory_focus import unique_messages, bounded_memories
    if "scoped_memory_data" in result:
        result["scoped_memory_data"]=bounded_memories(result["scoped_memory_data"])
    speakers, rows, actor_keys = {}, [], {}
    for message in _visible_quote_rows(context.get("recent_context_messages_untrusted",[])):
        pair=(message.get("user_id",""),message.get("nickname",""))
        if pair not in actor_keys:
            alias=f"u{len(actor_keys)+1}"
            actor_keys[pair]=alias
            speakers[alias]=list(pair)
        rows.append([message.get("message_id",""),message.get("time",""),actor_keys[pair],message.get("text","")])
    if rows:
        result["recent_context_messages_untrusted"]={
            "columns":["message_id","time","speaker","text"],"speakers":speakers,"rows":rows}
    result=lossless_context(result)
    samples=[]
    for item in result.get("selected_real_dialogues",[]):
        sample={key:item[key] for key in ("context","decision","reply") if key in item}
        source=item.get("source",{})
        sample["source"]={key:source[key] for key in ("dataset","url") if key in source}
        samples.append(sample)
    if samples:result["selected_real_dialogues"]=samples
    else:result.pop("selected_real_dialogues",None)
    if "dsh_only_recorded_gemini_style" in result:
        samples=[item["text_segments"] for item in result["dsh_only_recorded_gemini_style"]]
        if samples:result["dsh_only_recorded_gemini_style"]=samples
        else:result.pop("dsh_only_recorded_gemini_style",None)
    files=result.get("available_files")
    if files:
        result["available_files"]={"columns":["name","type","bytes"],
                                 "rows":[[item["name"],item["type"],item["bytes"]] for item in files]}
    stickers=result.get("saved_sticker_names")
    if stickers:
        grouped={}
        for name,hint in stickers.items():grouped.setdefault(hint,[]).append(name)
        result["saved_sticker_names"]=grouped
    if result.get("listening_window"):
        # The trusted listening instruction already carries the length scale;
        # absolute epoch bounds are internal collection data, not chat content.
        result["listening_window"]={key:value for key,value in result["listening_window"].items()
                                    if key not in {"started_at","collected_until","casual_length_scale"}}
    paths=context.get("trusted_image_paths",[])
    if fallback:
        result.pop("trusted_image_paths",None)
        result.pop("trusted_image_codes",None)
        if result.get("provided_images_untrusted"):
            result["image_status"]="仅provided_images_untrusted列出的图片已附带像素，其余图片不猜画面"
        elif paths:result["image_status"]="仅有图片记录，未提供像素，不猜画面"
    for key,value in list(result.items()):
        if value in ([],{},""):result.pop(key)
    return result


def _visible_quote_rows(messages):
    """A quote of a message that is itself in the window keeps only its reference.

    ``[引用 昵称 的消息(id=N): "原文"]`` repeats the quoted text every turn; when
    row N is visible the id already points at the identical words.
    """
    if not isinstance(messages, list) or not all(isinstance(row, dict) for row in messages):
        return messages
    ids = {str(row.get("message_id")) for row in messages if row.get("message_id") not in (None, "")}
    rows = []
    for row in messages:
        text = row.get("text")
        if isinstance(text, str) and "[引用 " in text:
            trimmed = trim_quotes(text, ids - {str(row.get("message_id"))})
            if trimmed != text:
                row = dict(row, text=trimmed)
        rows.append(row)
    return rows


def _image_codes(result):
    """Authorized images on the wire are stable codes; tools map them back."""
    paths = result.get("trusted_image_paths")
    if isinstance(paths, list) and paths and all(isinstance(path, str) for path in paths):
        # Rename in place: key order is the cache prefix order.
        items = [("trusted_image_codes", image_refs(paths)) if key == "trusted_image_paths" else (key, value)
                 for key, value in result.items()]
        result.clear()
        result.update(items)


def dumps_context(context, *, fallback=False):
    return json.dumps(compact_context(context,fallback=fallback),ensure_ascii=False,separators=(",",":"))


def _reference_vertical_notes(result):
    notes = result.get('scoped_memory_data')
    axes = result.get('rolling_and_axial_memory')
    threads = axes.get('vertical_threads') if isinstance(axes, dict) else None
    if not isinstance(notes, list) or not isinstance(threads, dict):
        return

    by_id = {}
    for note in notes:
        match = re.match(r'^\[(M[0-9]+)\]', note) if isinstance(note, str) else None
        if match:
            memory_id = match.group(1)
            by_id[memory_id] = None if memory_id in by_id else note
    references = {note: memory_id for memory_id, note in by_id.items() if note is not None}
    positions = {}
    for index, note in enumerate(notes):
        if isinstance(note, str):
            positions.setdefault(note, index)

    threads = dict(threads)
    positional_used = False
    saved = 0

    def wire_size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))

    # A memory_id points to the full, identically worded note in scoped_memory_data;
    # notes without an M id are pointed to by their 0-based position there.
    for category in ('person_and_commitments', 'topics_and_episodes'):
        items = threads.get(category)
        if isinstance(items, list):
            wired = []
            for item in items:
                if isinstance(item, str) and item in references:
                    wired.append({'memory_id': references[item]})
                elif isinstance(item, str) and item in positions and \
                        wire_size({'note': positions[item]}) < wire_size(item):
                    wired.append({'note': positions[item]})
                    saved += wire_size(item) - wire_size({'note': positions[item]})
                    positional_used = True
                else:
                    wired.append(item)
            threads[category] = wired
    if threads == axes.get('vertical_threads'):
        return
    if positional_used:
        if saved <= wire_size(_NOTE_LEGEND) + 16:
            # The legend would cost more than it saves; keep the literal notes.
            return _reference_vertical_notes_ids_only(result, notes, axes, references)
        axes = _with_legend(axes, _NOTE_LEGEND)
    result['rolling_and_axial_memory'] = dict(axes, vertical_threads=threads)


_NOTE_LEGEND = '{"note":i}=scoped_memory_data[i]原文(0起)'
_TIMELINE_LEGEND = '时间线数字i=scoped_memory_data[i]对应的时间线行(id取原文前15字,excerpt取前100字)'


def _with_legend(axes, part):
    existing = axes.get('note_refs')
    if existing == part or (isinstance(existing, str) and part in existing):
        return axes
    return dict(axes, note_refs=part if not existing else f'{existing}；{part}')


def _reference_vertical_notes_ids_only(result, notes, axes, references):
    threads = dict(axes['vertical_threads'])
    for category in ('person_and_commitments', 'topics_and_episodes'):
        items = threads.get(category)
        if isinstance(items, list):
            threads[category] = [
                {'memory_id': references[item]} if isinstance(item, str) and item in references else item
                for item in items]
    if threads != axes['vertical_threads']:
        result['rolling_and_axial_memory'] = dict(axes, vertical_threads=threads)


def reference_timeline_excerpts(context):
    """Reference timeline rows derivable in full from a unique scoped note."""
    notes = context.get('scoped_memory_data')
    axes = context.get('rolling_and_axial_memory')
    timeline = axes.get('horizontal_timeline') if isinstance(axes, dict) else None
    if not isinstance(notes, list) or not isinstance(timeline, list):
        return context

    by_id = {}
    for note in notes:
        match = re.match(r'^\[(M[0-9]+)\]', note) if isinstance(note, str) else None
        if not match:
            continue
        memory_id = match[1]
        if memory_id in by_id:
            by_id[memory_id] = None
            continue
        header = re.match(r'\s*([^·\n]+)·(?:记)?([0-9\-\s:]+)：([^\n]*)', note[match.end():])
        by_id[memory_id] = ({'id': memory_id, 'label': header[1].strip(),
                             'time': header[2].strip(), 'excerpt': header[3].strip()[:100]}
                            if header else None)

    # Rows of notes without a (unique) M id are derived from the note's position.
    from hakimi.memory.rolling_context import timeline_row
    row_positions = {}
    for index, note in enumerate(notes):
        if isinstance(note, str):
            row_positions.setdefault(json.dumps(timeline_row(note), ensure_ascii=False, sort_keys=True), index)

    # Preserve literal rows (including extra fields) alongside references.
    rows = []
    referenced_rows = []
    positional_used = False
    for row in timeline:
        memory_id = row.get('id') if isinstance(row, dict) else None
        expected = by_id.get(memory_id) if isinstance(memory_id, str) else None
        full_match = expected is not None and row == expected
        if not full_match and isinstance(row, dict):
            try:
                position = row_positions.get(json.dumps(row, ensure_ascii=False, sort_keys=True))
            except (TypeError, ValueError):
                position = None
            if position is not None:
                rows.append(row)
                referenced_rows.append(position)
                positional_used = True
                continue
        if expected is not None and row.get('excerpt') == expected['excerpt']:
            reference = {'scoped_memory_id': memory_id, 'body_prefix_chars': 100}
            reference_size = len(json.dumps(reference, separators=(',', ':')))
            excerpt_size = len(json.dumps(row['excerpt'], ensure_ascii=False).encode('utf-8'))
            if reference_size < excerpt_size:
                row = dict(row, excerpt=reference)
        rows.append(row)
        referenced_rows.append(memory_id if full_match else row)
    timeline_wire = rows
    if referenced_rows != rows and all(isinstance(item, dict) for item in timeline):
        # The source legend is worthwhile only when it saves space. Plain
        # strings in imported timelines are never reinterpreted as references.
        grouped = {'rows': referenced_rows, 'id_source': 'scoped_memory_data', 'body_prefix_chars': 100}
        def wire_size(value):
            return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        legend_cost = wire_size(_TIMELINE_LEGEND) + 16 if positional_used else 0
        if wire_size(grouped) + legend_cost < wire_size(rows):
            timeline_wire = grouped
            if positional_used:
                axes = _with_legend(axes, _TIMELINE_LEGEND)
    if timeline_wire == timeline:
        return context
    return dict(context, rolling_and_axial_memory=dict(axes, horizontal_timeline=timeline_wire))


def lossless_context(context):
    """Encode message dictionaries as a table without dropping any field/value.

    Missing fields are represented separately from explicit null. Identical
    message IDs are references only when every field is exactly identical.
    Repeated identities may share a speaker table; every name/ID is retained.
    Repeated scoped notes are referenced by memory_id ({"memory_id":"M1"}) or, without
    an M id, by position ({"note":i}) in the vertical threads and timeline rows.
    Quotes of visible messages and authorized image paths are references
    (``(id=N)`` and ``#code``) that tools resolve back to the same data.
    """
    result = dict(context)
    _image_codes(result)
    for key in ('recent_context_messages', 'recent_context_messages_untrusted'):
        if key in result:
            result[key] = _visible_quote_rows(result[key])
    opportunity = result.get('memory_opportunity')
    social = result.get('social_memory_opportunity')
    if isinstance(opportunity, dict) and isinstance(social, dict):
        # Compare JSON so values such as true and 1 stay distinct.
        if json.dumps(opportunity.get('social'), sort_keys=True) == json.dumps(social, sort_keys=True):
            result['memory_opportunity'] = {key: value for key, value in opportunity.items() if key != 'social'}
    _reference_vertical_notes(result)
    for key in ('recent_context_messages', 'recent_context_messages_untrusted'):
        messages = result.get(key)
        if not isinstance(messages, list) or not messages or not all(isinstance(row, dict) for row in messages):
            continue
        columns = list(dict.fromkeys(field for row in messages for field in row))
        rows, missing, refs, seen = [], {}, [], {}
        for message in messages:
            signature = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            if message.get('message_id') and signature in seen:
                refs.append(seen[signature])
                continue
            index = len(rows)
            seen[signature] = index
            refs.append(index)
            row = [message.get(field) for field in columns]
            absent = [i for i, field in enumerate(columns) if field not in message]
            if absent:
                missing[str(index)] = absent
            rows.append(row)
        table = {'columns': columns, 'rows': rows}
        if missing:
            table['missing_fields'] = missing
        if len(rows) != len(messages):
            table['message_order'] = refs
        # Avoid making very short/irregular contexts larger just to use a table.
        def wire(value):
            return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        candidates = [messages, table]
        # Factor only repeated identity pairs. Keep every other column, missing
        # field marker and message-order reference unchanged and reversible.
        if 'user_id' in columns and 'nickname' in columns and 'speaker' not in columns:
            uid, nick = columns.index('user_id'), columns.index('nickname')
            if all('user_id' in row and 'nickname' in row
                   and isinstance(row['user_id'], str) and isinstance(row['nickname'], str)
                   for row in messages):
                identity_columns = [field for field in columns if field not in ('user_id', 'nickname')]
                identity_columns.insert(min(uid, nick), 'speaker')
                speakers, aliases, identity_rows = {}, {}, []
                for row in rows:
                    pair = (row[uid], row[nick])
                    if pair not in aliases:
                        alias = f'u{len(aliases)+1}'
                        aliases[pair] = alias
                        speakers[alias] = list(pair)
                    values = dict(zip(columns, row))
                    values['speaker'] = aliases[pair]
                    identity_rows.append([values[field] for field in identity_columns])
                factored = dict(table, columns=identity_columns, rows=identity_rows,
                                speaker_columns=['user_id', 'nickname'], speakers=speakers)
                if missing:
                    factored['missing_fields'] = {
                        index: [identity_columns.index(columns[i]) for i in absent]
                        for index, absent in missing.items()}
                candidates.append(factored)
        result[key] = min(candidates, key=lambda value: len(wire(value)))
    # Idempotent: the Vertex prompt applies it again to the same value.
    return reference_timeline_excerpts(result)


def compact_tool_result(output):
    """Decode JSON inside the CLI envelope once; preserve all values/errors.

    Only the model's wire copy changes. Delivery and authorization use the
    original tool receipt. Human text/code and non-JSON stdout stay verbatim.
    """
    if not isinstance(output, dict) or not isinstance(output.get('stdout'), str):
        return output
    try:
        # Duplicate object keys and non-finite numbers must not be normalized.
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError('duplicate_key')
                result[key] = value
            return result
        def invalid(value):
            raise ValueError(value)
        decoded = json.loads(output['stdout'], object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, TypeError):
        return output
    if not isinstance(decoded, (dict, list)):
        return output
    candidate = dict(output, stdout=decoded)
    wire = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return candidate if len(wire(candidate)) < len(wire(output)) else output
