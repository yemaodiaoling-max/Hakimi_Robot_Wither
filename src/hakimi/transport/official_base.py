"""QQ official channel primitives shared by transport, files and group adaptation."""
import hashlib
import json
import urllib.request
from pathlib import Path

from hakimi.core.runtime_io import atomic_replace


API_ROOT = "https://api.bot.qq.com"
SELF_ALIAS = "9000000000000000001"
GROUP_EVENTS = frozenset({"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"})
PRIVATE_EVENTS = frozenset({"C2C_MESSAGE_CREATE"})
MESSAGE_EVENTS = GROUP_EVENTS | PRIVATE_EVENTS
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class OfficialError(RuntimeError):
    def __init__(self, code, *, ambiguous=False):
        self.code = str(code)[:40]
        self.ambiguous = ambiguous
        super().__init__(self.code)  # Never retain raw provider text, URL or secret.


def alias(namespace, value):
    return str(10**18 + int.from_bytes(hashlib.sha256((namespace + ":" + value).encode()).digest()[:8], "big") % (8 * 10**18))


def cq_escape(value):
    return str(value).replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")


def fixed_json(path, value):
    path = Path(path)
    if path.is_symlink() or path.resolve() != path.absolute():
        raise OfficialError("REDIRECTED_STATE")
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    atomic_replace(temp,path)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise OfficialError("UNEXPECTED_HTTP_REDIRECT")
