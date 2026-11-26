"""Bounded tool-call diagnostics in ``<base>/state/tool_calls.jsonl``: no prompts, arguments, tokens or output."""
import json
from pathlib import Path
import time


def log_tool_call(base,name,status,**fields):
    """Tiny bounded diagnostics: no prompts, arguments, tokens or tool output."""
    try:
        path=Path(base)/'state/tool_calls.jsonl';path.parent.mkdir(exist_ok=True)
        if path.exists() and path.stat().st_size>1024*1024:
            with path.open('rb') as stream:
                stream.seek(-262144,2);tail=stream.read().partition(b'\n')[2]
            path.write_bytes(tail)
        with path.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(dict(at=time.time(),tool=name,status=status,**fields))+'\n')
    except OSError:pass
