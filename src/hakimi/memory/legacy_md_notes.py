"""Conservative, read-only parser for legacy handwritten ``memory.md`` notes.

The parser only ever reads ``<root>/memory.md``.  It never writes, never follows
the source file through a symlink, and fails closed on missing, oversized,
unreadable, redirected, or invalid-UTF-8 input.  Failures are silent: no file
content is logged.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

MAX_FILE_BYTES = 2 * 1024 * 1024
SOURCE = "memory.md"
SOURCE_KIND = "legacy_handwritten"
SOURCE_LABEL = "旧版手记（历史看法，尚未重新确认）"

_ID_RE = r"[1-9][0-9]{0,19}"

_HEADING_RE = re.compile(r"^( {0,3})(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")

# Primary group heading and a small number of unambiguous compatible forms.
_GROUP_PATTERNS = (
    re.compile(
        rf"^- \*\*`(?P<gid>{_ID_RE})`[ \t]*[（(](?P<name>[^()（）\r\n]+)[）)]\*\*"
        r"(?:[ \t]*[：:].*)?$"
    ),
    re.compile(
        rf"^- \*\*(?P<gid>{_ID_RE})[ \t]*[（(](?P<name>[^()（）\r\n]+)[）)]\*\*"
        r"(?:[ \t]*[：:].*)?$"
    ),
    re.compile(
        rf"^- `(?P<gid>{_ID_RE})`[ \t]*[（(](?P<name>[^()（）\r\n]+)[）)]"
        r"(?:[ \t]*\*\*)?(?:[ \t]*[：:].*)?$"
    ),
    re.compile(
        rf"^- (?P<gid>{_ID_RE})[ \t]*[（(](?P<name>[^()（）\r\n]+)[）)]"
        r"(?:[ \t]*\*\*)?(?:[ \t]*[：:].*)?$"
    ),
)

_NOTE_START_RE = re.compile(
    rf"^  - 群友[ \t]+`(?P<uid>{_ID_RE})`(?P<rest>[^\r\n]*)$"
)

_PRIVATE_HEADING_MARKERS = (
    "私人会话",
    "私密会话",
    "私人聊天",
    "私聊",
    "个人会话",
)


def _safe_memory_path(root: Path) -> Path | None:
    """Return the source path only when it is a plain file inside *root*."""

    try:
        root_path = Path(root)
        # A root supplied through a symlink is untrusted too.
        if root_path.is_symlink():
            return None
        root_resolved = root_path.resolve(strict=True)
        if not root_resolved.is_dir():
            return None

        candidate = root_path / SOURCE
        if candidate.is_symlink():
            return None

        source_lstat = candidate.lstat()
        if not stat.S_ISREG(source_lstat.st_mode):
            return None

        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(root_resolved)
        except ValueError:
            return None
        return candidate
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _read_bytes(path: Path) -> bytes | None:
    """Read at most MAX_FILE_BYTES + 1 bytes without following a final symlink."""

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = None
    try:
        fd = os.open(path, flags | nofollow)
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            return None
        if file_stat.st_size > MAX_FILE_BYTES:
            return None

        chunks: list[bytes] = []
        remaining = MAX_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_FILE_BYTES:
            return None
        return data
    except OSError:
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _read_text(root: Path) -> str | None:
    path = _safe_memory_path(root)
    if path is None:
        return None
    data = _read_bytes(path)
    if data is None:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # A BOM is file metadata, not part of the first handwritten line.
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def _lines(text: str) -> list[tuple[str, str]]:
    """Split into (content, original-line-ending) pairs.

    Only CR, LF, and CRLF are line endings here; unusual Unicode separators
    inside a note stay in the note.  A trailing line ending does not create a
    synthetic extra line.
    """

    result: list[tuple[str, str]] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\r":
            end = index + 1
            if end < length and text[end] == "\n":
                end += 1
            result.append((text[start:index], text[index:end]))
            index = end
            start = end
        elif char == "\n":
            result.append((text[start:index], "\n"))
            index += 1
            start = index
        else:
            index += 1
    if start < length:
        result.append((text[start:], ""))
    return result


def _heading(content: str) -> tuple[int, str] | None:
    match = _HEADING_RE.match(content)
    if match is None:
        return None
    return len(match.group(2)), match.group(3) or ""


def _is_private_heading(title: str) -> bool:
    compact = re.sub(r"\s+", "", title)
    return any(marker in compact for marker in _PRIVATE_HEADING_MARKERS)


def _group_header(content: str) -> tuple[str, str] | None:
    if not content.startswith("- "):
        return None
    for pattern in _GROUP_PATTERNS:
        match = pattern.match(content)
        if match is not None:
            name = match.group("name").strip()
            if not name:
                return None
            return match.group("gid"), name
    return None


def _note_start(content: str) -> tuple[str, str | None] | None:
    """Parse only an explicit two-space ``- 群友 `id``` entry start."""

    match = _NOTE_START_RE.match(content)
    if match is None:
        return None

    rest = match.group("rest")
    position = 0
    while position < len(rest) and rest[position] in " \t":
        position += 1

    nickname: str | None = None
    if position < len(rest) and rest[position] in "（(":
        closing = "）" if rest[position] == "（" else ")"
        end = rest.find(closing, position + 1)
        if end < 0:
            return None
        inner = rest[position + 1 : end].strip()
        if inner.startswith("**") and inner.endswith("**") and len(inner) >= 4:
            nickname = inner[2:-2].strip() or None
        elif inner:
            nickname = inner
        position = end + 1
        while position < len(rest) and rest[position] in " \t":
            position += 1

    if position >= len(rest) or rest[position] not in ":：":
        return None
    return match.group("uid"), nickname


def _is_note_boundary(content: str) -> bool:
    if content == "":
        return False
    if _heading(content) is not None:
        return True
    if content.startswith("- "):
        return True
    if content.startswith("  - "):
        return True
    # Any other non-indented line is a new block rather than a continuation.
    if not content[0].isspace():
        return True
    return False


def _entry_text(lines: list[tuple[str, str]], start: int, stop: int) -> str:
    """Return the exact entry text, excluding the final line terminator."""

    last = stop - 1
    # Blank separator lines after the last content line are not part of it.
    while last > start and lines[last][0] == "":
        last -= 1

    pieces: list[str] = []
    for index in range(start, last + 1):
        content, ending = lines[index]
        pieces.append(content)
        if index < last:
            pieces.append(ending)
    return "".join(pieces)


def _make_note(
    group_id: str,
    group_name: str,
    user_id: str,
    nickname: str | None,
    text: str,
) -> dict:
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    identity_payload = f"{SOURCE}/{group_id}/{user_id}/{text}".encode("utf-8")
    note_id = "LM" + hashlib.sha256(identity_payload).hexdigest()[:24]
    return {
        "id": note_id,
        "group_id": group_id,
        "user_id": user_id,
        "nickname": nickname,
        "group_name": group_name,
        "text": text,
        "hash": text_hash,
        "source": SOURCE,
        "source_kind": SOURCE_KIND,
        "source_label": SOURCE_LABEL,
    }


def _parse(text: str) -> tuple[list[dict], list[dict]]:
    lines = _lines(text)
    notes: list[dict] = []
    groups: list[dict] = []
    current_group: tuple[str, str] | None = None
    section_stack: list[tuple[int, bool]] = []

    index = 0
    while index < len(lines):
        content = lines[index][0]

        heading = _heading(content)
        if heading is not None:
            level, title = heading
            while section_stack and section_stack[-1][0] >= level:
                section_stack.pop()
            private = section_stack[-1][1] if section_stack else False
            if _is_private_heading(title):
                private = True
            section_stack.append((level, private))
            current_group = None
            index += 1
            continue

        private = section_stack[-1][1] if section_stack else False

        group_header = _group_header(content)
        if group_header is not None:
            current_group = None if private else group_header
            if not private:
                groups.append(
                    {"group_id": group_header[0], "name": group_header[1]}
                )
            index += 1
            continue

        if content.startswith("- "):
            current_group = None
            index += 1
            continue

        note_start = _note_start(content)
        if note_start is not None:
            stop = index + 1
            while stop < len(lines) and not _is_note_boundary(lines[stop][0]):
                stop += 1

            if current_group is not None and not private:
                user_id, nickname = note_start
                group_id, group_name = current_group
                text_value = _entry_text(lines, index, stop)
                notes.append(
                    _make_note(
                        group_id,
                        group_name,
                        user_id,
                        nickname,
                        text_value,
                    )
                )
            index = stop
            continue

        if content and not content[0].isspace():
            current_group = None
        index += 1

    return notes, groups


def read_notes(root: Path) -> list[dict]:
    """Read explicit legacy person notes from ``root/memory.md``.

    Returns an empty list for a missing/unreadable/unsafe/oversized file.
    """

    text = _read_text(root)
    if text is None:
        return []
    try:
        notes, _ = _parse(text)
        return notes
    except Exception:
        # Parsing is local and should not normally fail, but never expose file
        # content or propagate a malformed-file error.
        return []


def groups(root: Path) -> list[dict]:
    """Return explicit public group headers as ``group_id``/``name`` dicts."""

    text = _read_text(root)
    if text is None:
        return []
    try:
        _, group_list = _parse(text)
        return group_list
    except Exception:
        return []


def read_note(root: Path, note_id: str) -> dict | None:
    """Return a note by its content-derived ID, or ``None``.

    The file is re-read each time, so changed text (and therefore a changed
    ID) cannot be served from a stale cache.
    """

    if not isinstance(note_id, str) or not note_id:
        return None
    text = _read_text(root)
    if text is None:
        return None
    try:
        notes, _ = _parse(text)
    except Exception:
        return None
    for note in notes:
        if note["id"] == note_id:
            return note
    return None
