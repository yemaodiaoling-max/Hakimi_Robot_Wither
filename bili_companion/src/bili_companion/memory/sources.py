"""Derive public media locators only from native scope identities."""
import re


def media_refs(scope, session_id=None):
    if scope.platform != 'bilibili' or scope.kind not in {'video', 'live_room'}:
        return []
    kind = 'video' if scope.kind == 'video' else 'live'
    ref = 'bilibili:' + kind + ':' + scope.identity
    refs = [ref + (':session:' + session_id if session_id else '')]
    if kind == 'video' and re.fullmatch(r'BV[0-9A-Za-z]{10}', scope.identity):
        refs.append('https://www.bilibili.com/video/' + scope.identity)
    elif kind == 'live' and re.fullmatch(r'[1-9][0-9]*', scope.identity):
        refs.append('https://live.bilibili.com/' + scope.identity)
    return refs


def linked_refs(refs):
    """Enrich historical refs without rewriting facts or source revisions."""
    result = list(refs)
    for ref in refs:
        match = re.fullmatch(r'bilibili:video:(BV[0-9A-Za-z]{10})(?::session:[^\r\n]+)?', ref)
        if match:
            url = 'https://www.bilibili.com/video/' + match[1]
            if url not in result and len(result) < 8:
                result.append(url)
    return result
