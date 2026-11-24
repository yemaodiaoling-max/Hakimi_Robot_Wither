"""Who owns this bot instance (the "主人"), and which accounts are bots.

Values come from ``<root>/owner.json``, written by the setup panel (``hakimi setup``).
Everything here is read once per process; restart services after changing it.

    {
      "name": "主人",                    # how the bot refers to its owner
      "short_name": "主人",              # shorter call name used in casual text (default: name)
      "aliases": ["主人的群名片"],        # other nicknames that identify the owner
      "ids": ["<官方通道 member/user id>", "<旧 QQ 号>"],
      "pet_session": "private:<id>",     # the private session the desktop pet writes to
      "bot_self_id": "0",                # the bot's own QQ number on the old OneBot channel (optional)
      "known_bot_ids": [],               # other bots in your groups, never treated as people
      "local": {                         # optional vocabulary of your own groups
        "privacy_phrases": [],           #   phrases that mark a desktop observation as a private chat
        "public_group_keywords": [],     #   window titles that are public groups, not private chats
        "signature_terms": [],           #   nicknames / running jokes that count as core memories
        "no_instant_wake_ids": []        #   members whose @-mentions never wake the bot instantly (spam)
      }
    }
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from hakimi import paths

FILE_NAME = "owner.json"
DEFAULT_NAME = "主人"


@lru_cache(maxsize=1)
def load(root: str | None = None) -> dict:
    import os
    override = os.environ.get("HAKIMI_OWNER_FILE", "").strip()
    path = Path(override) if override else (Path(root) if root else paths.root()) / FILE_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _strings(values):
    return [str(v).strip() for v in (values or []) if str(v).strip()]


def owner_name() -> str:
    return str(load().get("name") or DEFAULT_NAME)


def owner_ids() -> frozenset:
    return frozenset(i for i in _strings(load().get("ids")) if i.isdigit())


def owner_names() -> frozenset:
    """Nicknames that mark the owner.  Only explicitly configured names count: with no
    owner.json the display default "主人" never makes an arbitrary group member an admin."""
    names = set(_strings(load().get("aliases")))
    if load().get("name"):
        names.add(str(load()["name"]).strip())
    return frozenset(n for n in names if len(n) >= 2)


def owner_session() -> str:
    value = str(load().get("pet_session") or "")
    if value.startswith("private:") and value.split(":", 1)[1].isdigit():
        return value
    ids = sorted(owner_ids(), key=len, reverse=True)
    return "private:" + ids[0] if ids else "private:0"


def bot_self_id() -> int:
    try:
        return int(load().get("bot_self_id") or 0)
    except (TypeError, ValueError):
        return 0


def known_bot_ids() -> frozenset:
    ids = set(_strings(load().get("known_bot_ids")))
    if bot_self_id():
        ids.add(str(bot_self_id()))
    return frozenset(i for i in ids if i.isdigit())


def short_name() -> str:
    return str(load().get("short_name") or owner_name())


def local_terms(key: str) -> tuple:
    """Instance vocabulary from ``owner.json: local.<key>`` (empty when not configured)."""
    local = load().get("local")
    return tuple(_strings(local.get(key))) if isinstance(local, dict) else ()


def phrase_alternation(key: str) -> str:
    """``|phrase1|phrase2`` (escaped) to append to a regex alternation; empty when none."""
    return "".join("|" + re.escape(term) for term in local_terms(key))


ADMIN_IDS = owner_ids()
ADMIN_NAMES = owner_names()
OWNER_NAME = owner_name()
OWNER_SHORT = short_name()
