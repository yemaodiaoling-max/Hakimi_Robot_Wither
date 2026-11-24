"""Small text conversion for OneBot forwards and official inline message nodes."""
import html
import json
import re
import time
from urllib.parse import parse_qs, urlsplit


QQ_RECORD_TYPES = (102, 103, '102', '103')
QQ_RECORD_HEADER = re.compile(r'^[ \t]*\[群聊的聊天记录\][ \t]*(?:\r?\n|$)', re.MULTILINE)
QQ_RECORD_ENTRY = re.compile(r'^=== 消息 ([0-9]+) ===[ \t]*(?:\r?\n|$)', re.MULTILINE)
QQ_RECORD_FIELD = re.compile(r'^\[(发送者|发送时间|消息时间|时间|消息类型|附件[0-9]+)\][ \t]?([^\r\n]*)$')
QQ_RECORD_BODY = re.compile(r'\A\[消息内容\][ \t]?')
QQ_QUOTE_TAIL = re.compile(r'\r?\n\[消息类型\][ \t]*引用消息[ \t]*(?:\r?\n\[关联消息\](?:\r?\n|[ \t]|$)|\Z)')
QQ_RELATED_RECORD = re.compile(
    r'\r?\n\[关联消息\][ \t]*\r?\n[ \t]*(?:--- 第[0-9]+条 ---|=== 消息 [0-9]+(?:\.[0-9]+)* ===)[ \t]*\r?\n')
QQ_ATTACHMENT_FIELDS = re.compile(r'类型[:：](.+?)[ \t]+文件名[:：](.+)')
QQ_ATTACHMENT_METADATA = re.compile(
    r'[ \t,，;；]+(?=(?:(?:大小|文件大小|宽度|高度|尺寸|文件地址|下载地址|链接|URL|SIZE|WIDTH|HEIGHT)[:：]|(?:https?:)?//))',
    re.IGNORECASE)
QQ_CARD_KEYS = ('ark_data', 'ark', 'miniapp', 'card', 'arkElement', 'miniAppElement', 'embed', 'embeds')
QQ_CARD_TEXT = {'title', 'desc', 'description', 'text'}
QQ_CARD_LINKS = {'jump_url', 'jumpurl', 'url', 'target_url', 'targeturl', 'href', 'link',
                 'qqdocurl', 'qqdoc_url', 'weburl'}
QQ_CARD_PAYLOADS = {'data', 'json', 'payload', 'content', 'bytesdata', 'bytes_data',
                    *(key.lower() for key in QQ_CARD_KEYS)}
QQ_CARD_CONTROL = {'prompt', 'preview', 'callbackdata', 'authtoken', 'commands', 'actions',
                   'buttons', 'interaction', 'messagescene', 'ext', 'icon', 'cover', 'thumbnail',
                   'thumb', 'avatar', 'sourcelogo', 'tagicon', 'templateid'}


def qq_record_attachment(value):
    attachment = QQ_ATTACHMENT_FIELDS.fullmatch(value)
    if attachment:
        filename = QQ_ATTACHMENT_METADATA.split(attachment[2], maxsplit=1)[0].strip()
        if filename and not filename.startswith(('https://', 'http://', '//')):
            return f'附件（{attachment[1]}）：{filename[:240]}'
    return ''


def _related_rows(content):
    indent = re.match(r'[ \t]+(?=\[)', content)
    if not indent:
        return []
    prefix = indent[0]
    lines = []
    for line in content.splitlines(keepends=True):
        if line.strip() and not line.startswith(prefix):
            break
        lines.append(line.removeprefix(prefix))
    return _record_rows('=== 消息 1 ===\n' + ''.join(lines).rstrip('\r\n'), nested=True) or []


def _record_rows(content, *, quote=False, typed=False, nested=False):
    """Keep a numbered entry's speaker separate from the speaker in its quoted reply."""
    header = QQ_RECORD_HEADER.search(content)
    if header and content[:header.start()].strip():
        header = None
    start = header.end() if header else 0
    start = len(content) - len(content[start:].lstrip(' \t\r\n'))
    first = QQ_RECORD_ENTRY.match(content, start)
    if not first or first[1] != '1':
        if not header:
            return None
        return [{'text': '[部分 QQ 聊天记录未能读取]'}] if content[start:].strip() else []
    entries = [first]
    for entry in QQ_RECORD_ENTRY.finditer(content, first.end()):
        if entry[1] == str(len(entries) + 1):
            entries.append(entry)
    extracted = []
    unreadable = False
    recognized = typed or header is not None
    for index, entry in enumerate(entries):
        following = entries[index + 1] if index + 1 < len(entries) else None
        body = content[entry.end():following.start() if following else len(content)]
        if following:
            body = re.sub(r'(?:\r?\n){1,2}\Z', '', body)
        tail = QQ_QUOTE_TAIL.search(body) if quote else None
        related = QQ_RELATED_RECORD.search(body)
        if tail:
            body = body[:tail.start()]
        elif related:
            body = body[:related.start()]
        lines = body.splitlines(keepends=True)
        name, attachments, end_attachments = '', [], []
        first_line, end_line = 0, len(lines)
        while first_line < end_line:
            field = QQ_RECORD_FIELD.fullmatch(lines[first_line].rstrip('\r\n'))
            if not field:
                break
            if field[1] == '发送者':
                name = field[2]
            elif field[1].startswith('附件'):
                attachment = qq_record_attachment(field[2])
                if attachment:
                    attachments.append(attachment)
                else:
                    unreadable = True
            first_line += 1
        marker = QQ_RECORD_BODY.match(lines[first_line]) if first_line < end_line else None
        if marker:
            recognized = True
            while end_line > first_line + 1:
                field = QQ_RECORD_FIELD.fullmatch(lines[end_line - 1].rstrip('\r\n'))
                if not field:
                    break
                if field[1] == '发送者' and not name:
                    name = field[2]
                elif field[1].startswith('附件'):
                    attachment = qq_record_attachment(field[2])
                    if attachment:
                        end_attachments.append(attachment)
                    else:
                        unreadable = True
                elif field[1] == '消息类型' and (nested or related or field[2].strip() in ('引用消息', '聊天记录', '普通文本')):
                    pass
                else:
                    break
                end_line -= 1
            body = ''.join(lines[first_line:end_line])[marker.end():]
            if end_line < len(lines):
                body = re.sub(r'\r?\n\Z', '', body)
        elif first_line == end_line:
            body = ''
        else:
            unreadable = True
            continue
        if attachments:
            recognized = True
        row = {'text': body} if body.strip() else {}
        if related:
            related_text = content[entry.end() + related.end():following.start() if following else len(content)]
            row['quotes'] = [] if nested else _related_rows(related_text)
        if attachments or end_attachments:
            row['attachments'] = attachments + list(reversed(end_attachments))
        if row:
            if name:
                row['speaker'] = name
            extracted.append(row)
    if not recognized:
        return None
    if unreadable:
        extracted.append({'text': '[部分 QQ 聊天记录未能读取]'})
    return extracted


def _record_line(row):
    parts = [row['text']] if row.get('text') else []
    if 'quotes' in row:
        quoted = '\n\n'.join(_record_line(item) for item in row['quotes'])
        parts.append('引用：' + quoted if quoted else '引用了另一条消息')
    parts.extend(row.get('attachments', ()))
    body = '\n'.join(parts)
    return f"{row['speaker']}：{body}" if row.get('speaker') else body


def qq_record_text(content, *, quote=False, typed=False, nested=False):
    rows = _record_rows(content, quote=quote, typed=typed, nested=nested)
    return None if rows is None else '\n\n'.join(_record_line(row) for row in rows)


def _context_entry(row, *, quoted=False):
    entry = {'speaker': (row.get('speaker') or '').strip()[:80] or 'QQ未标注'}
    if row.get('text'):
        text = re.sub(r'<faceType=[^<>\r\n]{1,512}>', '[QQ表情]', row['text']).strip()
        limit = 400 if quoted else 1200
        entry['text'] = text if len(text) <= limit else text[:limit-1] + '…'
    if row.get('attachments'):
        entry['attachments'] = [label[:160] for label in row['attachments'][:3]]
    if 'quotes' in row:
        entry['quotes'] = [_context_entry(item, quoted=True) for item in row['quotes'][:4]] or [{'text': '引用内容未提供'}]
    return entry


def split_at_context(elements):
    """QQ sends the mention in d.content and the numbered preceding messages in a text envelope."""
    if not isinstance(elements, list):
        return [], elements
    history, remaining = [], []
    for element in elements[:64]:
        raw = element.get('content') if isinstance(element, dict) else None
        if isinstance(raw, str) and element.keys() == {'content'}:
            rows = _record_rows(raw[:32000], quote=True)
            if rows is not None:
                history.extend(_context_entry(row) for row in rows[-10:])
                continue
        remaining.append(element)
    # Leave room for the live question in both prompt formats. Keep the nearest messages first.
    recent, size = [], 0
    for entry in reversed(history[-10:]):
        size += len(json.dumps(entry, ensure_ascii=False).encode('utf-8')) + 2
        if recent and size > 8192:
            break
        recent.append(entry)
    return recent[::-1], remaining


def qq_card_text(card, caption=''):
    """Read display and destination fields of an official card, including mini-app links."""
    visible = []
    links = []
    remaining = 128

    def read(value, level, display_fields=False):
        nonlocal remaining
        remaining -= 1
        if level > 8 or remaining < 0:
            return None
        if isinstance(value, str):
            try:
                value = json.loads(value[:32000])
            except ValueError:
                return None
        if isinstance(value, list):
            return [read(item, level + 1, display_fields) for item in value[:64]]
        if not isinstance(value, dict):
            return None
        safe = {}
        for key, field in list(value.items())[:64]:
            lower = key.lower()
            if lower == 'value' and isinstance(value.get('key'), str):
                lower = value['key'].strip('#').lower()
            if lower.replace('_', '').replace('-', '') in QQ_CARD_CONTROL:
                continue
            if display_fields and lower in ('name', 'value') and isinstance(field, str) and field.strip():
                visible.append(field[:1200])
                continue
            if isinstance(field, str) and field.strip() and lower in QQ_CARD_TEXT | QQ_CARD_LINKS:
                safe[key] = field[:32000]
                if lower in QQ_CARD_TEXT:
                    visible.append(field[:1200])
                else:
                    try:
                        url = urlsplit(field[:32000])
                        params = {name.lower() for name in parse_qs(url.query, keep_blank_values=True)}
                        if (url.scheme in ('http', 'https') and url.hostname and not url.username
                                and not params & {'rkey', 'auth_token', 'token', 'sign', 'signature'}):
                            links.append(field[:1200])
                    except ValueError:
                        pass
            elif lower in {'key', 'path', 'bvid', 'bv_id', 'appid', 'app_id', 'app', 'source'}:
                if isinstance(field, (str, int)):
                    safe[key] = field
            elif lower in QQ_CARD_PAYLOADS or isinstance(field, (dict, list)):
                safe[key] = read(field, level + 1, lower == 'fields' and isinstance(field, list))
        return safe

    from hakimi.tools.bilibili_video import miniapp_text
    safe = read(card, 0)
    try:
        video = miniapp_text(safe) if safe else ''
    except ValueError:
        video = ''
    return '\n'.join(dict.fromkeys(text for text in (*visible, *links, video) if text and text != caption))[:4000]


def qq_element_content(elements, *, record_type=None):
    """Read official elements through their message fields, with attachments kept local."""
    lines, attachments = [], []
    remaining = 64

    def visit(items, parent_type, depth):
        nonlocal remaining
        if depth > 2 or not isinstance(items, list):
            return
        for element in items[:remaining]:
            if not remaining:
                break
            remaining -= 1
            if not isinstance(element, dict):
                continue
            kind = element.get('type')
            message_type = element.get('message_type')
            if kind not in (None, 'text', 'node', 'forward', 'json', 'ark', 'miniapp'):
                continue
            if message_type not in (None, 0, '0', 3, '3', 101, '101', *QQ_RECORD_TYPES):
                continue
            item = element.get('data', element) if kind in ('node', 'forward') else element
            if not isinstance(item, dict):
                continue
            sender = item.get('sender', item.get('author'))
            sender = sender if isinstance(sender, dict) else {}
            name = (sender.get('card') or sender.get('member_nickname') or sender.get('nickname')
                    or sender.get('username') or item.get('nickname') or item.get('name'))
            record = message_type if message_type in QQ_RECORD_TYPES else parent_type
            body = item.get('content', item.get('message', item.get('text', '')))
            raw = body[:12000] if isinstance(body, str) else ''
            envelope = None
            if message_type in QQ_RECORD_TYPES or message_type is None and not (kind == 'node' or sender or name):
                envelope = qq_record_text(raw, quote=record in (103, '103'), typed=record in QQ_RECORD_TYPES)
            text = envelope if envelope is not None else (
                raw if sender or isinstance(name, str) or message_type in (0, '0') else '')
            if kind == 'node' and isinstance(body, list):
                text = ''.join(part['data']['text'] for part in body[:64] if isinstance(part, dict)
                               and part.get('type') == 'text' and isinstance(part.get('data'), dict)
                               and isinstance(part['data'].get('text'), str))
            if text.strip():
                if isinstance(name, str) and name.strip() and envelope is None:
                    text = f'{name.splitlines()[0][:80]}：{text}'
                lines.append(text)
            cards = [item[key] for key in QQ_CARD_KEYS if key in item]
            if kind in ('json', 'ark', 'miniapp'):
                cards.append(element)
            lines.extend(text for card in cards[:4] if (text := qq_card_text(card, raw)))
            media = item.get('attachments')
            if isinstance(media, list) and (sender or record in QQ_RECORD_TYPES or envelope is not None
                                            or message_type in (0, '0', 101, '101')):
                attachments.extend(a for a in media[:8-len(attachments)] if isinstance(a, dict))
            nested = item.get('msg_elements')
            if not isinstance(nested, list) and kind in ('node', 'forward'):
                container = body if isinstance(body, dict) else item
                nested = container.get('messages', container.get('nodes'))
            if isinstance(nested, list):
                visit(nested, record, depth+1)

    visit(elements, record_type, 0)
    return '\n'.join(lines)[:8000], attachments


def message_text(content, depth=0):
    if depth > 2:
        return "[嵌套转发未展开]"
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return message_text(content.get("text", content.get("content", content.get("message", ""))))
    if not isinstance(content, list):
        return ""
    result = []
    for part in content[:64]:
        if not isinstance(part, dict):
            result.append(str(part) if isinstance(part, str) else "")
            continue
        kind, data = part.get("type", "text"), part.get("data", part)
        if not isinstance(data, dict):
            continue
        if kind == "text":
            # Literal control-looking text stays text, not an image/file request.
            result.append(str(data.get("text", data.get("content", ""))).replace("[CQ:", "［CQ:"))
        elif kind in {"image", "forward", "face"}:
            keys = ("file", "url", "sub_type") if kind == "image" else ("id",)
            fields = ",".join(f"{key}={str(data[key]).replace(',', '&#44;').replace(']', '&#93;')}"
                              for key in keys if key in data)
            result.append(f"[CQ:{kind},{fields}]")
        elif kind == "at":
            result.append("＠" + str(data.get("qq", "群友")))
        elif kind == "reply":
            result.append("[转发内引用:" + str(data.get("id", "")) + "]")
        elif kind == "node":
            result.append(render_forward([part], lambda body: message_text(body, depth+1), depth=depth+1))
        elif kind in {"json", "ark", "miniapp"}:
            from hakimi.tools.bilibili_video import miniapp_text
            result.append(miniapp_text(data) or "[小程序卡片，未提供可观看的B站链接]")
        else:
            result.append(message_text(data))
    return "".join(result)


def render_forward(value, render_content=message_text, *, depth=0):
    if depth > 2:
        return "[嵌套转发未展开]"
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value[:3500]
    if isinstance(value, dict):
        value = value.get("messages", value.get("nodes", value.get("message", value.get("content", []))))
    if not isinstance(value, list) or not value:
        return "[合并转发：平台未提供正文]"
    lines, remaining, clipped = [], 3400, len(value) > 24
    for node in value[:24]:
        if not isinstance(node, dict):
            continue
        item = node.get("data", node) if node.get("type") == "node" else node
        if not isinstance(item, dict):
            continue
        sender = item.get("sender", item.get("author", {}))
        sender = sender if isinstance(sender, dict) else {}
        name = str(sender.get("card") or sender.get("nickname") or sender.get("username")
                   or item.get("nickname") or item.get("name") or "原作者").replace("\n", " ")[:40]
        stamp = item.get("time", item.get("timestamp", ""))
        if type(stamp) in (int, float) and 0 < stamp < 32503680000:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(stamp))
        body = item.get("message", item.get("content", item.get("text", "")))
        if isinstance(body, dict) and any(k in body for k in ("messages", "nodes")):
            text = render_forward(body, render_content, depth=depth+1)
        else:
            text = render_content(body)
        if not text:
            continue
        line = f"{name} {str(stamp)[:25]}: {text}".replace("@哈基米", "＠哈基米")
        if len(line) > remaining:
            lines.append(line[:remaining])
            clipped = True
            break
        lines.append(line)
        remaining -= len(line)+1
    if not lines:
        return "[合并转发：平台未提供正文]"
    return "[合并转发记录；原作者转述，非本群新发言]\n" + "\n".join(lines) + (
        "\n[转发过长，以上为节选]" if clipped else "\n[转发记录结束]")


def expand_forward(raw, fetch, render_content):
    def replace(match):
        kind, params = match.group(1), match.group(2)
        if kind == "forward":
            found = re.search(r"(?:^|,)id=([^,]+)", params)
            forward_id = html.unescape(found[1]) if found else ""
        elif kind == "json":
            try:
                card = json.loads(html.unescape(params.removeprefix("data=")))
                if not isinstance(card, dict) or card.get("app") != "com.tencent.multimsg":
                    return match[0]
                forward_id = card.get("meta", {}).get("detail", {}).get("resid", "")
            except (ValueError, AttributeError):
                return match[0]
        else:
            found = re.search(r'(?:m_resid|resid)=[\"\x27]([^\"\x27]+)', html.unescape(params))
            if not found:
                return match[0]
            forward_id = found[1]
        response = fetch(str(forward_id)) if forward_id else {}
        data = response.get("data", {}) if isinstance(response, dict) else {}
        return render_forward(data, render_content)
    return re.sub(r"\[CQ:(forward|json|xml),([^\]]+)\]", replace, raw)
