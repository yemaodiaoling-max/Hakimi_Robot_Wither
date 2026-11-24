"""Identity linking primitives: ids, validation, instance roots, errors."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path


REVISION = "2026-10-27"
MAX_BODY_FIELDS = 32768
GROUP_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
USER_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
LEGACY_RECORD_ID = re.compile(r"L([1-9][0-9]{0,19})\Z", re.IGNORECASE)
LEGACY_REVIEW_GROUP = re.compile(r"R([1-9][0-9]{0,19})-g([0-9]{1,9})\Z", re.IGNORECASE)
LEGACY_REVIEW_MEMBER = re.compile(
    r"R([1-9][0-9]{0,19})-m([1-9][0-9]{0,19})-([0-9]{1,9})\Z", re.IGNORECASE)
LEGACY_MD_NOTE = re.compile(r"LM([0-9a-f]{24})\Z", re.IGNORECASE)
LINK_ACTIONS = {"link_group", "unlink_group", "link_member", "unlink_member"}
_REQUEST_FIELDS = {
    "link_group": {"action", "old_group", "new_group", "expected_revision", "confirm"},
    "unlink_group": {"action", "old_group", "new_group", "expected_revision", "confirm"},
    "link_member": {"action", "old_group", "new_group", "old_user", "new_user",
                    "expected_revision", "confirm"},
    "unlink_member": {"action", "old_group", "new_group", "old_user", "new_user",
                      "expected_revision", "confirm"},
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS identity_revision (
 id INTEGER PRIMARY KEY CHECK (id=1),
 revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_group_links (
 old_root TEXT NOT NULL,
 old_group TEXT NOT NULL,
 new_group TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1,
 created_revision INTEGER NOT NULL,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 PRIMARY KEY (old_root, old_group)
);
CREATE UNIQUE INDEX IF NOT EXISTS identity_group_links_new_active
 ON identity_group_links(old_root, new_group) WHERE active=1;
CREATE TABLE IF NOT EXISTS identity_member_links (
 old_root TEXT NOT NULL,
 old_group TEXT NOT NULL,
 old_user TEXT NOT NULL,
 new_group TEXT NOT NULL,
 new_user TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1,
 disabled_by TEXT NOT NULL DEFAULT '',
 created_revision INTEGER NOT NULL,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 PRIMARY KEY (old_root, old_group, old_user)
);
CREATE UNIQUE INDEX IF NOT EXISTS identity_member_links_new_active
 ON identity_member_links(old_root, new_group, new_user) WHERE active=1;
CREATE INDEX IF NOT EXISTS identity_member_links_old_user
 ON identity_member_links(old_root, old_user, active);
CREATE INDEX IF NOT EXISTS identity_member_links_new_user
 ON identity_member_links(old_root, new_user, active);
CREATE TABLE IF NOT EXISTS identity_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 revision INTEGER NOT NULL,
 at REAL NOT NULL,
 action TEXT NOT NULL,
 old_root TEXT NOT NULL,
 old_group TEXT NOT NULL DEFAULT '',
 old_user TEXT NOT NULL DEFAULT '',
 new_group TEXT NOT NULL DEFAULT '',
 new_user TEXT NOT NULL DEFAULT '',
 operator TEXT NOT NULL,
 operator_source TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS identity_audit_at ON identity_audit(at DESC,id DESC);
"""


class IdentityError(Exception):
    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        self.extra = extra


def _runtime_path(base):
    return Path(base).resolve() / "state/memory.sqlite3"


def source_root(base):
    """The old QQ root, using the same rule as the official adapter."""
    base = Path(base).resolve()
    return base.parent if base.name == "official_test_runtime" else base


def old_root_key(base):
    return str(source_root(base))


def old_root_label(base):
    root = source_root(base)
    try:
        return root.name or "旧数据源"
    except Exception:
        return "旧数据源"


def _read_json(path, default):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return value
    except (OSError, ValueError, TypeError):
        return default


def _valid_group(value):
    return bool(GROUP_ID.fullmatch(str(value or "")))


def _valid_user(value):
    return bool(USER_ID.fullmatch(str(value or "")))


def _clean_group(value, field="group"):
    text = str(value or "").strip()
    if not _valid_group(text):
        raise IdentityError(400, "invalid_" + field, f"{field}必须是公开数字编号")
    return text


def _clean_user(value, field="user"):
    text = str(value or "").strip()
    if not _valid_user(text):
        raise IdentityError(400, "invalid_" + field, f"{field}必须是数字QQ号")
    return text


def _old_root_id(base):
    return hashlib.sha256(old_root_key(base).encode("utf-8")).hexdigest()[:12]


def _old_root_matches(base, stored):
    left = os.path.normcase(str(stored or ""))
    right = os.path.normcase(old_root_key(base))
    return bool(left) and left == right
