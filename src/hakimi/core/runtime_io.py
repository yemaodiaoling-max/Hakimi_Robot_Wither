"""Process-local JSON read cache, invalidated by atomic file replacement.

This caches reads only. Ownership changes, sends and tool authorization still
use their original locked/durable paths. Nothing is cached across processes.
"""
from collections import OrderedDict
from copy import deepcopy
import json
from pathlib import Path
import threading
import time

_CACHE = OrderedDict()
_DERIVED = OrderedDict()
_LOCK = threading.RLock()
_MISSING = object()


def atomic_replace(temporary, destination):
    """Retry only short Windows reader conflicts; preserve callers' locks/fsync."""
    for delay in (0.02, 0.05, 0.1, 0.2, None):
        try:
            Path(temporary).replace(destination)
            return
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)


def signature(path):
    try:
        st = Path(path).stat()
        return st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns
    except FileNotFoundError:
        return None


def read_json_cached(path, default=_MISSING, *, readonly=False):
    path = Path(path)
    key = str(path.absolute())
    with _LOCK:
        stamp = signature(path)
        if stamp is None:
            _CACHE.pop(key, None)
            if default is _MISSING:
                raise FileNotFoundError(path)
            return deepcopy(default)
        entry = _CACHE.get(key)
        if entry is None or entry[0] != stamp:
            for _ in range(3):
                value = json.loads(path.read_text(encoding='utf-8'))
                after = signature(path)
                if stamp == after:
                    _CACHE[key] = stamp, value
                    break
                stamp = after
            else:
                raise OSError('JSON file changed repeatedly while reading')
        _CACHE.move_to_end(key)
        while len(_CACHE) > 128:
            _CACHE.popitem(last=False)
        value = _CACHE[key][1]
        return value if readonly else deepcopy(value)


def invalidate(path):
    with _LOCK:
        key = str(Path(path).absolute())
        _CACHE.pop(key, None)
        for cached in list(_DERIVED):
            if cached[0] == key:
                _DERIVED.pop(cached, None)


def read_derived_cached(path, loader, variant):
    """Reuse bounded, read-only parsing; callers still apply time/access checks.

    A concurrent append keeps the loader's original EOF-snapshot behavior, but
    that result is not cached. Missing inputs and failures are never cached.
    """
    path = Path(path)
    key = (str(path.absolute()), variant)
    before = signature(path)
    with _LOCK:
        cached = _DERIVED.get(key)
        if cached is not None and before is not None and cached[0] == before:
            _DERIVED.move_to_end(key)
            return cached[1]
        _DERIVED.pop(key, None)
    result = loader(path)
    if before is not None and signature(path) == before:
        with _LOCK:
            _DERIVED[key] = before, result
            _DERIVED.move_to_end(key)
            while len(_DERIVED) > 16:
                _DERIVED.popitem(last=False)
    return result
