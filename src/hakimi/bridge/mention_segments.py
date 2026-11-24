"""Join a resolved group recipient to formatted QQ text, after bubble escaping."""
import re

from hakimi.bridge.reply_format import MAX_SEGMENT_CHARS

_QUOTE = re.compile(r'^\[CQ:reply,id=-?\d+\]')
_BARE_LOCAL = re.compile(r'^@([1-9][0-9]{18})(?=\s|$)')
UNRESOLVED = 'mention_target_unresolved'
CLARIFY = '你想让我 @ 哪位群友？告诉我群里的昵称吧。'


def _quote_and_body(segment):
    quote = _QUOTE.match(segment)
    return (quote[0], segment[quote.end():].lstrip()) if quote else ('', segment)


def leading_local_id(segments):
    if segments:
        match = _BARE_LOCAL.match(_quote_and_body(segments[0])[1])
        if match:
            return match[1]
    return None


def attach_recipient(segments, uid):
    """Recognize old echoed prefixes only for the recipient selected for this turn."""
    tag = f'[CQ:at,qq={uid}]'
    prefixes = (tag, f'&#91;CQ:at,qq={uid}&#93;', f'&amp;#91;CQ:at,qq={uid}&amp;#93;')
    result = []
    attached = False
    for segment in segments:
        quote, body = _quote_and_body(segment)
        if body.startswith(('[CQ:image,', '[CQ:record,', '[CQ:file,', '[CQ:video,')):
            result.append(segment)
            continue
        # Older sessions may echo the tool's tag, with or without a quote or
        # an @local-id prefix. Never interpret pasted markup for a different user.
        while body:
            prefix = next((p for p in prefixes if body.startswith(p)), None)
            if prefix is None:
                bare = _BARE_LOCAL.match(body)
                if not bare or bare[1] != uid:
                    break
                prefix = bare[0]
            body = body[len(prefix):].lstrip()
        if not attached:
            body = tag + (' ' + body if body else '')
            attached = True
        if body:
            if len(quote) + len(body) > MAX_SEGMENT_CHARS:
                raise ValueError('mention_reply_too_long')
            result.append(quote + body)
    return result
