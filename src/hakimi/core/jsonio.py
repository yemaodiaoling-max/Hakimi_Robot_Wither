"""Small tolerant JSON / JSON-lines readers with bounded tail reads."""
import json


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def tail_jsonl(path, count=80, max_bytes=131072):
    """Bound reads; ignore partial/truncated/corrupt records without loading all history."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            start = max(0, f.tell() - max_bytes)
            f.seek(start)
            if start:
                f.readline()
            lines = f.read(max_bytes).decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    result = []
    for line in lines:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                result.append(row)
        except ValueError:
            pass
    return result[-count:]
