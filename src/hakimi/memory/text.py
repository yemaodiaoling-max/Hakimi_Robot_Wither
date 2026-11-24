"""Pure text helpers for memory: search terms, compact time stamps, public source URLs."""
import re
from datetime import datetime
from hakimi.core.context_window import CHAT_ZONE


def terms(text, *, limit=1800):
    chunks = re.findall(r"[a-zA-Z][a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]+", str(text).lower()[:limit])
    return {word for chunk in chunks for word in ([chunk] if chunk.isascii() else
            [chunk[i:i+2] for i in range(len(chunk)-1)])} - set("这个 那个 什么 怎么 可以 一下 我们 你们 还是 就是 不是 知道 哈基 基米".split())


def valid_source(source):
    """The same HTTPS-source rule used by memory writes and retrieval."""
    from urllib.parse import unquote, urlsplit
    import unicodedata
    try:
        if (not isinstance(source, str) or not 1 <= len(source) <= 400
                or any(c.isspace() or c in '\\<>"`' for c in source)
                or any(unicodedata.category(c).startswith("C") for c in source+unquote(source))):
            return False
        parsed = urlsplit(source)
        host = (parsed.hostname or "").encode("idna").decode("ascii")
        return (parsed.scheme == "https" and bool(host) and parsed.username is None and parsed.password is None
                and (parsed.port is None or parsed.port > 0) and (":" in host or len(host) <= 253 and all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in (host[:-1] if host.endswith(".") else host).split("."))))
    except (ValueError, UnicodeError):
        return False


def stamp(at, *, now=None):
    recorded = datetime.fromtimestamp(at, CHAT_ZONE)
    reference = datetime.now(CHAT_ZONE) if now is None else datetime.fromtimestamp(now, CHAT_ZONE)
    return recorded.strftime('%m-%d %H:%M' if recorded.year == reference.year else '%Y-%m-%d %H:%M')
