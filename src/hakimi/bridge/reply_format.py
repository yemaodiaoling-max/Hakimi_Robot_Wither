"""Flexible semantic QQ bubbles with exceptional size guards, never truncation.

Short chat usually needs one or two messages, but explicit paragraphs are kept.
Every real/literal newline is a message boundary, never an embedded newline.
"""
import bisect
import heapq
import math
import re

MAX_REPLY_SEGMENTS = 5
MAX_SEGMENT_CHARS = 4000
MAX_REPLY_TEXT_CHARS = 12000


def group_reply_rules(session, config):
    if not isinstance(session, str) or not session.startswith('group:'):
        return {}
    return config.get('group_reply_overrides', {}).get(session.split(':', 1)[1], {})


def limit_group_reply(segments, session, config):
    """Apply explicit group budgets after media staging; other sessions are untouched."""
    import html
    rules = group_reply_rules(session, config)
    if not any(key in rules for key in ('max_chars', 'max_messages')):
        return segments
    remaining = rules.get('max_chars', MAX_REPLY_TEXT_CHARS)
    count = rules.get('max_messages', MAX_REPLY_SEGMENTS)
    output = []
    for segment in segments:
        if len(output) >= count:
            break
        parts = []
        for part in re.split(r'(\[CQ:[^\]]*\])', segment):
            if part.startswith('[CQ:'):
                parts.append(part)
            else:
                text = html.unescape(part)[:remaining]
                remaining -= len(text)
                parts.append(_escape(text))
        value = ''.join(parts).strip()
        if re.sub(r'\[CQ:[^\]]*\]', '', value).strip() or re.search(r'\[CQ:(?:image|record|video|file),', value):
            output.append(value)
    return output


_STRONG_BREAKS = frozenset("。！？!?；;")
_SOFT_BREAKS = frozenset("，、,：:.…")


def _escaped_size(text):
    return len(text) + 4 * sum(text.count(char) for char in "&[]")


def _escape(text):
    return text.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")


def _split_part(text, target):
    """Balance a long paragraph, preferring punctuation near each soft target."""
    prefix = [0]
    for char in text:
        prefix.append(prefix[-1] + (5 if char in "&[]" else 1))
    count = max(1, math.ceil(prefix[-1] / target))
    if count == 1:
        return [text]
    chunks, start = [], 0
    for remaining in range(count, 1, -1):
        cost_left = prefix[-1] - prefix[start]
        # Leave enough capacity in the remaining bubbles. Escaped entities are
        # indivisible because cuts are made in the original Unicode text.
        minimum = max(1, cost_left - MAX_SEGMENT_CHARS * (remaining - 1))
        low = bisect.bisect_left(prefix, prefix[start] + minimum, start + 1)
        high = bisect.bisect_right(prefix, prefix[start] + MAX_SEGMENT_CHARS, start + 1) - 1
        high = min(high, len(text) - remaining + 1)
        if low > high:
            raise ValueError("转义后的正文无法完整放入安全消息上限，请按语义分次提交")
        goal = prefix[start] + math.ceil(cost_left / remaining)
        radius = max(30, math.ceil((goal - prefix[start]) * 0.25))
        near_low = max(low, bisect.bisect_left(prefix, goal - radius, start + 1))
        near_high = min(high, bisect.bisect_right(prefix, goal + radius, start + 1) - 1)
        candidates = range(near_low, near_high + 1)
        cut = None
        for match in (lambda c: c in _STRONG_BREAKS, lambda c: c in _SOFT_BREAKS, str.isspace):
            possible = [i for i in candidates if match(text[i - 1])]
            if possible:
                cut = min(possible, key=lambda i: abs(prefix[i] - goal))
                break
        if cut is None:
            cut = min(high, max(low, bisect.bisect_left(prefix, goal, start + 1)))
        chunks.append(text[start:cut])
        start = cut
    chunks.append(text[start:])
    if any(_escaped_size(chunk) > MAX_SEGMENT_CHARS for chunk in chunks):
        raise ValueError("转义后的正文超过安全消息上限，请按语义分次提交")
    return chunks


def _chunks(parts, target):
    return [(chunk, index, index) for index, part in enumerate(parts)
            for chunk in _split_part(part, target)]


def _merge_to_guard(chunks):
    """Merge only adjacent complete chunks; preserve order and all content."""
    if len(chunks) <= MAX_REPLY_SEGMENTS:
        return list(chunks)
    nodes = [{"chunk": chunk, "size": _escaped_size(chunk[0]), "prev": i - 1,
              "next": i + 1 if i + 1 < len(chunks) else -1, "alive": True, "version": 0}
             for i, chunk in enumerate(chunks)]
    candidates = []

    def offer(left_index, right_index):
        if left_index < 0 or right_index < 0:
            return
        left, right = nodes[left_index], nodes[right_index]
        size = left["size"] + right["size"] + (left["chunk"][2] != right["chunk"][1])
        if size <= MAX_SEGMENT_CHARS:
            heapq.heappush(candidates, (size, left_index, right_index, left["version"], right["version"]))

    for i in range(len(nodes) - 1):
        offer(i, i + 1)
    count = len(nodes)
    while count > MAX_REPLY_SEGMENTS and candidates:
        size, left_index, right_index, left_version, right_version = heapq.heappop(candidates)
        left, right = nodes[left_index], nodes[right_index]
        if (not left["alive"] or not right["alive"] or left["next"] != right_index
                or left["version"] != left_version or right["version"] != right_version):
            continue
        l_chunk, r_chunk = left["chunk"], right["chunk"]
        separator = "" if l_chunk[2] == r_chunk[1] else " "
        left.update(chunk=(l_chunk[0] + separator + r_chunk[0], l_chunk[1], r_chunk[2]),
                    size=size, next=right["next"], version=left["version"] + 1)
        right["alive"] = False
        if left["next"] >= 0:
            nodes[left["next"]]["prev"] = left_index
        count -= 1
        offer(left["prev"], left_index)
        offer(left_index, left["next"])
    return [node["chunk"] for node in nodes if node["alive"]]


def clean_qq_markdown(text: str) -> str:
    """Strip QQ-incompatible Markdown and LaTeX syntax into clean plain text."""
    if not isinstance(text, str) or not text:
        return text
    # 1. Strip code fence lines (e.g. ```python or ```)
    text = re.sub(r'```[a-zA-Z0-9_-]*\n?', '', text)
    text = text.replace('```', '')
    # 2. Strip inline code backticks `code` -> code
    text = re.sub(r'`([^`\n]+)`', r'\1', text)
    # 3. Strip markdown headers (# Title -> Title)
    text = re.sub(r'(?m)^[ \t]*#{1,6}[ \t]+', '', text)
    # 4. Strip bold / italic markers (***bold*** -> bold, **bold** -> bold, *italic* -> italic)
    text = re.sub(r'\*\*\*([^\*\n]+)\*\*\*', r'\1', text)
    text = re.sub(r'\*\*([^\*\n]+)\*\*', r'\1', text)
    text = re.sub(r'(?<!\*)\*([^\*\n]+)\*(?!\*)', r'\1', text)
    text = re.sub(r'__([^_ \n][^_\n]*[^_ \n]|[^_ \n])__', r'\1', text)
    # 5. Clean list markers at start of lines (- item, * item, + item)
    text = re.sub(r'(?m)^[ \t]*[-*+][ \t]+', '', text)
    # 6. Clean blockquotes (> text)
    text = re.sub(r'(?m)^[ \t]*>[ \t]?', '', text)
    # 7. Strip bare LaTeX dollar delimiters ($E=mc^2$ -> E=mc^2, $$E=mc^2$$ -> E=mc^2)
    text = re.sub(r'\$\$([^\$]+)\$\$', r'\1', text)
    text = re.sub(r'\$([^\$\n]+)\$', r'\1', text)
    # 8. Clean trailing/multiple blank lines and spaces
    text = re.sub(r'[ \t]+$', '', text, flags=re.MULTILINE)
    return text.strip()


_AI_TAIL_BUBBLE_PATTERNS = (
    re.compile(r'^(?:不过|说起来|话说|而且|再说).*?(?:这句[“"「『].+?[”"」』]|确实真实|太理直气壮)'),
    re.compile(r'(?:本猫|本喵).*?(?:赛博营业|赛博打工|吃上热乎的|挣罐头|骗罐头|混口饭|赛博搬砖)'),
    re.compile(r'^(?:本猫|本喵)?(?:差点(?:没接住|就被.{1,8}带偏了)|耳朵(?:都跟着动了动|一下就支起来了|都差点没转过来))[。！!～~]*$'),
)

_AI_TRAILING_CLAUSE_PATTERNS = (
    re.compile(r'[，,]\s*(?:本猫|本喵)?(?:要是能吃上热乎的)?也不至于天天赛博(?:营业|打工)[^，,。！!？?]*[。！!～~]*$'),
    re.compile(r'[，,]\s*听得(?:我|本猫)耳朵都跟着[^，,。！!？?]*[。！!～~]*$'),
)

_AI_LEADING_ECHO_RE = re.compile(r'^(?:行|好)[，,]\s*那我[^，,]{2,12}[，,]\s*(.{4,})$')


def clean_ai_cliche_bubbles(parts: list[str]) -> list[str]:
    """Drop redundant AI-flavored tail bubbles and formulaic echo prefixes."""
    kept = list(parts)
    if len(kept) >= 2:
        tail = kept[-1].strip()
        if any(pattern.search(tail) for pattern in _AI_TAIL_BUBBLE_PATTERNS):
            kept.pop()
    cleaned = []
    for part in kept:
        updated = part
        for pattern in _AI_TRAILING_CLAUSE_PATTERNS:
            candidate = pattern.sub('', updated).strip()
            if candidate:
                updated = candidate
        # Strip repetitive '行，那我<echo>，<punchline>' prefixes when a full punchline follows.
        lead_match = _AI_LEADING_ECHO_RE.match(updated)
        if lead_match:
            updated = lead_match.group(1).strip()
        cleaned.append(updated)
    return cleaned


def text_bubbles(text):
    if not isinstance(text, str) or len(text) > MAX_REPLY_TEXT_CHARS:
        raise ValueError(f"正文超过异常保护上限 {MAX_REPLY_TEXT_CHARS} 字或不是文本，请分次提交")
    text = clean_qq_markdown(text)
    parts = [part.strip() for part in re.split(r"[|｜]|\r\n?|\n|\\n", text) if part.strip()]
    parts = clean_ai_cliche_bubbles(parts)
    if not parts:
        return []
    total = sum(_escaped_size(part) for part in parts)
    if total > MAX_REPLY_SEGMENTS * MAX_SEGMENT_CHARS:
        raise ValueError("转义后的完整正文超过安全容量，请按语义分次提交")
    # Also split prose when the model forgets its own message boundaries. The
    # target grows for long explanations so all content still fits, intact.
    soft_target = max(180, math.ceil(total / MAX_REPLY_SEGMENTS))
    chunks = _merge_to_guard(_chunks(parts, soft_target))
    if len(chunks) > MAX_REPLY_SEGMENTS:
        # Only relax the soft reading target when the exceptional hard limit
        # requires it. Never silently discard paragraphs or escaped characters.
        chunks = _merge_to_guard(_chunks(parts, MAX_SEGMENT_CHARS))
    if len(chunks) > MAX_REPLY_SEGMENTS:
        raise ValueError(f"这些语义段无法完整容纳在 {MAX_REPLY_SEGMENTS} 条消息内，请合并重点或分次提交")
    escaped = [_escape(chunk[0]) for chunk in chunks]
    if any(len(part) > MAX_SEGMENT_CHARS for part in escaped):
        raise ValueError("转义后的消息超过异常保护上限，请分次提交")
    return escaped
