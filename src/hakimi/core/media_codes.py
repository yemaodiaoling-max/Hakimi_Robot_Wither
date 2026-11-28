"""Short, stable media references for the model's wire copy.

Cached images are shown to the model as ``#k3f9``-style codes instead of
absolute ``img_cache`` paths.  A code is derived only from the image file name,
so the same picture keeps the same code on every turn (stable prompt prefix,
better Vertex implicit caching) and tools can resolve it again against the
session's daemon-authorized image list.  Chat text never authorizes an image.

The work is done by the C++ library ``native/hkmedia`` when it is built
(``make -C native/hkmedia``); this module carries a byte-for-byte Python mirror
that is used otherwise.  ``tests/test_media_codes.py`` checks both agree.
"""
import ctypes
import os
import re
from pathlib import Path

__all__ = ['image_code', 'code_map', 'resolve_image', 'compact_media', 'trim_quotes',
           'image_refs', 'native_available']

CODE_LENGTH = 4
WIDE_CODE_LENGTH = 6
_FIRST = b'abcdefghjkmnpqrstvwxyz'
_REST = b'0123456789abcdefghjkmnpqrstvwxyz'
_LIBRARY = None
_LOADED = False


def _library():
    global _LIBRARY, _LOADED
    if _LOADED:
        return _LIBRARY
    _LOADED = True
    if os.environ.get('HAKIMI_HKMEDIA') == 'python':
        return None
    candidates = [os.environ.get('HAKIMI_HKMEDIA_LIB', '')]
    root = Path(__file__).resolve().parents[3] / 'native' / 'hkmedia' / 'build'
    candidates += [str(root / name) for name in ('libhkmedia.so', 'hkmedia.dll', 'libhkmedia.dylib')]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            lib = ctypes.CDLL(candidate)
            lib.hk_version.restype = ctypes.c_int
            lib.hk_image_code.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int,
                                          ctypes.c_char_p, ctypes.c_size_t]
            lib.hk_image_code.restype = ctypes.c_int
            for name in ('hk_compact', 'hk_trim_quotes'):
                fn = getattr(lib, name)
                fn.restype = ctypes.c_size_t
            lib.hk_compact.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t,
                                       ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t]
            lib.hk_trim_quotes.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t,
                                           ctypes.c_char_p, ctypes.c_size_t]
            if lib.hk_version() == 1:
                _LIBRARY = lib
                break
        except (OSError, AttributeError):
            continue
    return _LIBRARY


def native_available():
    return _library() is not None


def _call(fn, *args):
    cap = max(256, sum(len(a) for a in args if isinstance(a, bytes)) * 2 + 256)
    while True:
        out = ctypes.create_string_buffer(cap)
        needed = fn(*args, out, cap)
        if needed < cap:
            return out.raw[:needed].decode('utf-8', 'replace')
        cap = needed + 1


# ---------------------------------------------------------------- python mirror

def _lower(data):
    return data.lower()  # bytes.lower() only changes ASCII, like the C++ code


def _basename(data):
    cut = max(data.rfind(b'/'), data.rfind(b'\\'))
    return data if cut < 0 else data[cut + 1:]


def _normalize(data):
    return _lower(data.replace(b'\\', b'/'))


def _py_image_code(path, n):
    n = min(12, max(3, n))
    h = 1469598103934665603
    for byte in _lower(_basename(path)):
        h = ((h ^ byte) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    out = bytearray([_FIRST[h % 22]])
    h //= 22
    for _ in range(1, n):
        out.append(_REST[h % 32])
        h //= 32
    return out.decode('ascii')


_SPACE = b' \t\r\n\f\v'


def _trim(data):
    return data.strip(_SPACE)


def _clip(data, limit):
    points = i = 0
    while i < len(data):
        if points == limit:
            return data[:i] + '…'.encode()
        c = data[i]
        i += 1 if c < 0x80 else 2 if c >> 5 == 0x6 else 3 if c >> 4 == 0xE else 4 if c >> 3 == 0x1E else 1
        points += 1
    return data


def _hex(c):
    if 48 <= c <= 57:
        return c - 48
    c |= 0x20 if 65 <= c <= 90 else 0
    return c - 87 if 97 <= c <= 102 else -1


def _utf8(cp):
    return chr(cp).encode('utf-8', 'surrogatepass')


_CQ_ESCAPES = ((b'&#44;', b','), (b'&#91;', b'['), (b'&#93;', b']'), (b'&amp;', b'&'))


def _cq_unescape(data):
    out, i = bytearray(), 0
    while i < len(data):
        for escaped, plain in _CQ_ESCAPES:
            if data.startswith(escaped, i):
                out += plain
                i += 5
                break
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def _percent_decode(data):
    out, i = bytearray(), 0
    while i < len(data):
        if data[i] == 37 and i + 2 < len(data):
            a, b = _hex(data[i + 1]), _hex(data[i + 2])
            if a >= 0 and b >= 0:
                out.append(a * 16 + b)
                i += 3
                continue
        out.append(data[i])
        i += 1
    return bytes(out)


_XML_ESCAPES = ((b'&amp;', b'&'), (b'&lt;', b'<'), (b'&gt;', b'>'), (b'&quot;', b'"'), (b'&apos;', b"'"))


def _xml_unescape(data):
    out, i = bytearray(), 0
    while i < len(data):
        for escaped, plain in _XML_ESCAPES:
            if data.startswith(escaped, i):
                out += plain
                i += len(escaped)
                break
        else:
            if data.startswith(b'&#', i):
                j = i + 2
                is_hex = j < len(data) and data[j] in b'xX'
                j += is_hex
                cp = digits = 0
                while j < len(data) and digits < 7:
                    c = data[j]
                    v = _hex(c) if is_hex else (c - 48 if 48 <= c <= 57 else -1)
                    if v < 0:
                        break
                    cp = cp * (16 if is_hex else 10) + v
                    j += 1
                    digits += 1
                if digits and j < len(data) and data[j] == 59 and 0 < cp <= 0x10FFFF and not 0xD800 <= cp <= 0xDFFF:
                    out += _utf8(cp)
                    i = j + 1
                    continue
            out.append(data[i])
            i += 1
    return bytes(out)


def _cq_param(params, key):
    for item in params.split(b','):
        eq = item.find(b'=')
        if eq >= 0 and item[:eq] == key:
            return item[eq + 1:]
    return None


_URL_BYTES = frozenset(c for c in range(0x21, 0x7F) if c not in b'"\'<>[]{}|\\^`')
_TRACKING = {b'spm_id_from', b'spm', b'vd_source', b'from_spmid', b'is_from_webapp', b'sender_device',
             b'unique_k', b'wxshare_count', b'share_source', b'share_medium', b'share_plat',
             b'share_session_id', b'share_from', b'share_tag', b'fbclid', b'gclid'}
_TEMP_HOSTS = {b'multimedia.nt.qq.com.cn', b'gchat.qpic.cn', b'c2cpicdw.qpic.cn'}


def _tracking(key, value):
    key = _lower(key)
    if key in _TRACKING or key.startswith(b'utm_'):
        return True
    if key == b'share' and value:
        return value.isdigit()
    if key == b'fr':
        return value[:5] == b'share'
    return False


def _host(url):
    start = url.find(b'://')
    if start < 0:
        return b''
    start += 3
    ends = [p for p in (url.find(b'/', start), url.find(b'?', start), url.find(b'#', start)) if p >= 0]
    host = url[start:min(ends)] if ends else url[start:]
    at = host.rfind(b'@')
    if at >= 0:
        host = host[at + 1:]
    colon = host.find(b':')
    if colon >= 0:
        host = host[:colon]
    return _lower(host)


def _clean_url(raw):
    url = raw.replace(b'&amp;', b'&')
    if _host(url) in _TEMP_HOSTS:
        return '[QQ临时媒体链接]'.encode()
    hash_at = url.find(b'#')
    fragment = b'' if hash_at < 0 else url[hash_at:]
    head = url if hash_at < 0 else url[:hash_at]
    q = head.find(b'?')
    if q < 0:
        return url
    kept = []
    for item in head[q + 1:].split(b'&'):
        eq = item.find(b'=')
        key, value = (item, b'') if eq < 0 else (item[:eq], item[eq + 1:])
        if item and not _tracking(key, value):
            kept.append(item)
    return head[:q] + (b'?' + b'&'.join(kept) if kept else b'') + fragment


def _url_length(text, at):
    end = at
    while end < len(text) and text[end] in _URL_BYTES:
        end += 1
    while end > at:
        c = text[end - 1]
        if c in b'.,;:!?':
            end -= 1
            continue
        if c == 41:
            body = text[at:end]
            if body.count(b')') > body.count(b'('):
                end -= 1
                continue
        break
    return end - at


def _json_string(text, key):
    needle = b'"' + key + b'"'
    at = 0
    while True:
        at = text.find(needle, at)
        if at < 0:
            return None
        i = at + len(needle)
        while i < len(text) and text[i] in _SPACE:
            i += 1
        if i >= len(text) or text[i] != 58:
            at += len(needle)
            continue
        i += 1
        while i < len(text) and text[i] in _SPACE:
            i += 1
        if i >= len(text) or text[i] != 34:
            at += len(needle)
            continue
        i += 1
        out = bytearray()
        while i < len(text) and text[i] != 34:
            c = text[i]
            if c != 92:
                out.append(c)
                i += 1
                continue
            if i + 1 >= len(text):
                break
            e = text[i + 1]
            i += 2
            if e in b'nt':
                out.append(32)
            elif e in b'rbf':
                pass
            elif e == 117:
                if i + 4 > len(text):
                    i = len(text)
                    continue
                values = [_hex(c) for c in text[i:i + 4]]
                if min(values) < 0:
                    continue
                cp = int(text[i:i + 4], 16)
                i += 4
                if 0xD800 <= cp <= 0xDBFF and i + 6 <= len(text) and text[i] == 92 and text[i + 1] == 117:
                    low_values = [_hex(c) for c in text[i + 2:i + 6]]
                    if min(low_values) >= 0:
                        low = int(text[i + 2:i + 6], 16)
                        if 0xDC00 <= low <= 0xDFFF:
                            cp = 0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00)
                            i += 6
                if 0xD800 <= cp <= 0xDFFF:
                    continue
                out += _utf8(cp)
            else:
                out.append(e)
        return _trim(bytes(out))


def _xml_value(text, name, attribute):
    if attribute:
        needle = name + b'="'
        at = 0
        while True:
            at = text.find(needle, at)
            if at < 0:
                return None
            if at > 0 and text[at - 1] not in _SPACE:
                at += len(needle)
                continue
            start = at + len(needle)
            end = text.find(b'"', start)
            return None if end < 0 else _trim(_xml_unescape(text[start:end]))
    opener, closer = b'<' + name, b'</' + name + b'>'
    at = text.find(opener)
    while at >= 0:
        gt = text.find(b'>', at)
        if gt < 0:
            return None
        nxt = text[at + len(opener)] if len(text) > at + len(opener) else 0
        if nxt == 62 or (nxt and nxt in _SPACE):
            end = text.find(closer, gt + 1)
            return None if end < 0 else _trim(_xml_unescape(text[gt + 1:end]))
        at = text.find(opener, at + 1)
    return None


def _add_part(parts, raw):
    part = _clip(_trim(raw), 60)
    if not part:
        return
    for index, existing in enumerate(parts):
        if part in existing:
            return
        if existing in part:
            parts[index] = part
            return
    parts.append(part)


def _card(parts, url):
    out = '[卡片'.encode()
    for index, part in enumerate(parts):
        out += ('｜' if index else ':').encode() + part
    if url:
        out += ' 链接:'.encode() + _clean_url(url)
    return out + b']'


def _hash_like(stem):
    return len(stem) >= 16 and all(_hex(c) >= 0 or c == 45 for c in stem)


def _sticker_name(name):
    dot = name.rfind(b'.')
    if dot <= 0 or dot > 100:
        return False
    if not all(48 <= c <= 57 or 65 <= c <= 90 or 97 <= c <= 122 or c in b'_-' for c in name[:dot]):
        return False
    return _lower(name[dot + 1:]) in (b'gif', b'png', b'jpg', b'jpeg', b'webp')


def _cq_image(params, table):
    file = _cq_param(params, b'file')
    if not file:
        return '[图片]'.encode()
    path = _percent_decode(file)
    if path.startswith(b'file:///'):
        path = path[8:]
    code = table.get(_normalize(path))
    if code is not None:
        return '[图片#'.encode() + code + b']'
    name = _basename(path.replace(b'\\', b'/'))
    if not _sticker_name(name):
        return '[图片]'.encode()
    if _hash_like(name[:name.rfind(b'.')]):
        return '[表情#'.encode() + _py_image_code(name, 4).encode() + b']'
    return '[表情:'.encode() + name + b']'


def _labelled(label, value, limit, empty):
    return ('[' + empty + ']').encode() if not value else ('[' + label + ':').encode() + _clip(value, limit) + b']'


def _cq_rewrite(kind, params, table):
    if kind == b'image':
        return _cq_image(params, table)
    fixed = {b'record': '[语音]', b'video': '[视频]', b'poke': '[戳一戳]'}
    if kind in fixed:
        return fixed[kind].encode()
    if kind == b'file':
        value = _cq_param(params, b'name')
        if value is None:
            value = _cq_param(params, b'file')
        name = _basename(_cq_unescape(value)) if value is not None else b''
        return _labelled('文件', name, 60, '文件')
    if kind == b'mface':
        value = _cq_param(params, b'summary')
        summary = _trim(_cq_unescape(value)) if value is not None else b''
        if len(summary) >= 2 and summary[:1] == b'[' and summary[-1:] == b']':
            summary = summary[1:-1]
        return _labelled('表情', summary, 30, '表情')
    if kind in (b'location', b'music'):
        value = _cq_param(params, b'title')
        title = _trim(_cq_unescape(value)) if value is not None else b''
        return _labelled('位置', title, 60, '位置') if kind == b'location' else _labelled('音乐分享', title, 60, '音乐分享')
    if kind == b'contact':
        return ('[推荐群聊]' if _cq_param(params, b'type') == b'group' else '[推荐联系人]').encode()
    if kind == b'share':
        parts = []
        for key in (b'title', b'content'):
            value = _cq_param(params, key)
            if value is not None:
                _add_part(parts, _cq_unescape(value))
        value = _cq_param(params, b'url')
        return _card(parts, _cq_unescape(value) if value is not None else b'')
    if kind == b'json':
        value = _cq_param(params, b'data')
        if value is None:
            return None
        data, parts, url = _cq_unescape(value), [], b''
        for key in (b'prompt', b'title', b'desc'):
            found = _json_string(data, key)
            if found is not None:
                _add_part(parts, found)
        for key in (b'qqdocurl', b'jumpUrl', b'url'):
            found = _json_string(data, key)
            if found is not None and found.startswith(b'http'):
                url = found
                break
        return _card(parts, url)
    if kind == b'xml':
        value = _cq_param(params, b'data')
        if value is None:
            return None
        data, parts, url = _cq_unescape(value), [], b''
        for name, attribute in ((b'brief', True), (b'title', False), (b'summary', False)):
            found = _xml_value(data, name, attribute)
            if found is not None:
                _add_part(parts, found)
        found = _xml_value(data, b'url', True)
        if found is not None and found.startswith(b'http'):
            url = found
        return _card(parts, url)
    return None


_MARKERS = ['群友发图'.encode(), '表情包图片'.encode()]
_MARKER_TAIL = '(可用view_file看图):'.encode()
_NO_CACHE = ':无可读缓存'.encode()


def _url_boundary(text, at):
    if at == 0:
        return True
    c = text[at - 1]
    return not (48 <= c <= 57 or 65 <= c <= 90 or 97 <= c <= 122 or c in b'/=_-.')


def _py_compact(text, table, technical):
    out, i = bytearray(), 0
    while i < len(text):
        c = text[i]
        if c == 91:
            handled = False
            for label in _MARKERS:
                head = 1 + len(label)
                if not text.startswith(label, i + 1) or not text.startswith(_MARKER_TAIL, i + head):
                    continue
                start = end = i + head + len(_MARKER_TAIL)
                while end < len(text) and text[end] not in b']\n':
                    end += 1
                if end >= len(text) or text[end] != 93:
                    break
                path = _trim(text[start:end])
                if not path:
                    break
                code = table.get(_normalize(path))
                out += b'[' + label + (b'#' + code if code is not None else _NO_CACHE) + b']'
                i = end + 1
                handled = True
                break
            if handled:
                continue
            if not technical and text.startswith(b'[CQ:', i):
                end = text.find(b']', i)
                if end >= 0:
                    body = text[i + 4:end]
                    comma = body.find(b',')
                    kind = body if comma < 0 else body[:comma]
                    params = b'' if comma < 0 else body[comma + 1:]
                    replacement = _cq_rewrite(kind, params, table)
                    if replacement is not None:
                        out += replacement
                        i = end + 1
                        continue
        elif not technical and c in b'hH' and (text.startswith(b'http://', i) or text.startswith(b'https://', i)) \
                and _url_boundary(text, i):
            n = _url_length(text, i)
            if n > 8:
                out += _clean_url(text[i:i + n])
                i += n
                continue
        out.append(c)
        i += 1
    return bytes(out)


_QUOTE_OPEN = '[引用 '.encode()
_QUOTE_MIDDLE = ' 的消息(id='.encode()


def _py_trim_quotes(text, ids):
    out, i = bytearray(), 0
    while i < len(text):
        if text.startswith(_QUOTE_OPEN, i):
            nick_start = i + len(_QUOTE_OPEN)
            mid = text.find(_QUOTE_MIDDLE, nick_start)
            if mid >= 0 and mid - nick_start <= 120 and b'\n' not in text[nick_start:mid]:
                id_start = j = mid + len(_QUOTE_MIDDLE)
                if j < len(text) and text[j] == 45:
                    j += 1
                while j < len(text) and 48 <= text[j] <= 57:
                    j += 1
                ident = text[id_start:j]
                if j > id_start and ident != b'-' and text.startswith(b'): "', j) and ident in ids:
                    body_end = text.find(b'"]', j + 4)
                    if body_end >= 0:
                        out += text[i:j] + b')]'
                        i = body_end + 2
                        continue
        out.append(text[i])
        i += 1
    return bytes(out)


# ---------------------------------------------------------------- public API

def _bytes(value):
    return str(value).encode('utf-8', 'surrogatepass')


def image_code(path, length=CODE_LENGTH):
    """Stable short code for a cached image, from its file name only."""
    data = _bytes(path)
    lib = _library()
    if lib is not None:
        out = ctypes.create_string_buffer(16)
        lib.hk_image_code(data, len(data), int(length), out, 16)
        return out.value.decode('ascii')
    return _py_image_code(data, int(length))


def code_map(paths):
    """{path: code} for an authorized list; colliding codes widen deterministically."""
    paths = list(dict.fromkeys(str(p) for p in paths))
    codes = {path: image_code(path) for path in paths}
    names = {}
    for path in paths:
        names.setdefault(Path(path.replace('\\', '/')).name.lower(), set()).add(path)
    by_code = {}
    for path, code in codes.items():
        by_code.setdefault(code, []).append(path)
    for code, group in by_code.items():
        if len(group) > 1:
            for path in group:
                codes[path] = image_code(path, WIDE_CODE_LENGTH)
    return codes


_TOKEN = re.compile(r'(?:\[?(?:群友发图|表情包图片|图片)?\s*)?#?([a-z][0-9a-z]{2,11})\]?', re.I)


def resolve_image(token, paths):
    """Map a model-supplied code (``k3f9``, ``#k3f9``, ``[群友发图#k3f9]``) to an authorized path."""
    match = _TOKEN.fullmatch(str(token or '').strip())
    if not match:
        return None
    wanted = match[1].lower()
    hits = [path for path, code in code_map(paths).items() if code == wanted]
    return hits[0] if len(hits) == 1 else None


def compact_media(text, paths=(), *, technical=False):
    """Rewrite image markers, raw CQ media/cards and tracking URLs for the wire copy."""
    text = str(text)
    if not text:
        return text
    mapping = code_map(paths)
    lib = _library()
    data = _bytes(text)
    if lib is not None:
        table = ''.join(f'{path}\t{code}\n' for path, code in mapping.items()).encode('utf-8', 'surrogatepass')
        return _call(lib.hk_compact, data, len(data), table, len(table), 1 if technical else 0)
    table = {_normalize(_bytes(path)): code.encode() for path, code in mapping.items()}
    return _py_compact(data, table, technical).decode('utf-8', 'replace')


def trim_quotes(text, visible_ids):
    """Drop a quote's copied body when the quoted message is itself visible."""
    text = str(text)
    ids = {str(value) for value in visible_ids if str(value)}
    if not ids or '[引用 ' not in text:
        return text
    data = _bytes(text)
    lib = _library()
    if lib is not None:
        raw = '\n'.join(sorted(ids)).encode('utf-8', 'surrogatepass')
        return _call(lib.hk_trim_quotes, data, len(data), raw, len(raw))
    return _py_trim_quotes(data, {_bytes(value) for value in ids}).decode('utf-8', 'replace')


def image_refs(paths):
    """Wire form of the authorized image list: codes only, in list order."""
    return list(code_map(paths).values())
