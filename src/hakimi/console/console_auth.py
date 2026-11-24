"""Console bearer token: created once with private permissions, never printed."""
from __future__ import annotations

import os
import re
import secrets
from pathlib import Path


def load_or_create_token(path):
    """Read an existing token, or generate one only when called by explicit startup."""
    path = Path(path).expanduser()
    if path.exists():
        raw = path.read_bytes().strip()
        if len(raw) < 32 or not re.fullmatch(rb"[A-Za-z0-9_\-]+", raw):
            raise ValueError("令牌文件长度不足或格式异常，拒绝覆盖")
        _chmod_private(path)
        return raw.decode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private(path.parent, directory=True)
    token = secrets.token_urlsafe(48)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(str(path), flags, 0o600)
    try:
        os.write(descriptor, token.encode("ascii"))
    finally:
        os.close(descriptor)
    _chmod_private(path)
    return token


def _chmod_private(path, *, directory=False):
    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except OSError:
        pass


def _read_token_file(path):
    raw = Path(path).read_bytes().strip()
    if len(raw) < 32:
        raise ValueError("令牌文件长度不足")
    return raw.decode("ascii", "strict")
