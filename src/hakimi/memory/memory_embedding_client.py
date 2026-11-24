"""Local-only client for the optional CPU embedding service.

The rest of the bot must never import a model directly.  This module talks to a
single loopback HTTP service (default 127.0.0.1:18766), caches query vectors and
fails fast when the service is absent so FTS/lexical memory keeps working.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:18766"
DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_REVISION = ""
DEFAULT_DIM = 512
MAX_QUERY_CACHE = 256


class EmbeddingUnavailable(RuntimeError):
    """Raised when the local encoder cannot serve a request."""


def _normalise(vector):
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if not values or not math.isfinite(norm) or norm <= 1e-12:
        raise EmbeddingUnavailable("encoder returned an empty or non-finite vector")
    return [value / norm for value in values]


class EmbeddingClient:
    """Small, dependency-free JSON client with bounded query caching."""

    def __init__(self, url=None, *, timeout=1.0, disabled=None):
        self.url = (url or os.environ.get("HAKIMI_EMBEDDING_URL") or DEFAULT_URL).rstrip("/")
        self.timeout = max(0.05, min(float(timeout), 5.0))
        env_disabled = os.environ.get("HAKIMI_EMBEDDING_DISABLE", "").lower() in {"1", "true", "yes"}
        self.disabled = env_disabled if disabled is None else bool(disabled)
        self.model_id = os.environ.get("HAKIMI_EMBEDDING_MODEL", DEFAULT_MODEL)
        self.revision = os.environ.get("HAKIMI_EMBEDDING_REVISION", DEFAULT_REVISION)
        self.fingerprint = os.environ.get("HAKIMI_EMBEDDING_FINGERPRINT", "")
        self.dim = int(os.environ.get("HAKIMI_EMBEDDING_DIM", str(DEFAULT_DIM)))
        self._cache = OrderedDict()
        self._lock = threading.RLock()
        self._unavailable_until = 0.0
        self._last_error = ""

    def status(self):
        return {
            "url": self.url,
            "model": self.model_id,
            "revision": self.revision,
            "fingerprint": self.fingerprint,
            "dim": self.dim,
            "disabled": self.disabled,
            "available": (not self.disabled) and time.monotonic() >= self._unavailable_until,
            "last_error": self._last_error,
        }

    def _set_identity(self, result):
        model = str((result or {}).get("model") or self.model_id)
        revision = str((result or {}).get("revision") or self.revision)
        fingerprint = str((result or {}).get("fingerprint") or self.fingerprint)
        changed = (model, revision, fingerprint) != (self.model_id, self.revision, self.fingerprint)
        self.model_id, self.revision, self.fingerprint = model, revision, fingerprint
        if changed:
            # Vectors from a previous model revision are never reused.
            with self._lock:
                self._cache.clear()
        return changed

    def _mark_unavailable(self, error):
        # Short cooldown: a stopped service must never add seconds to every turn.
        self._unavailable_until = time.monotonic() + 20.0
        self._last_error = str(error)[:240]
        raise EmbeddingUnavailable(self._last_error)

    def _request(self, path, payload=None, *, timeout=None):
        if self.disabled:
            raise EmbeddingUnavailable("embedding client disabled by configuration")
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url + path,
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        limit = self.timeout if timeout is None else max(0.05, min(float(timeout), 10.0))
        try:
            with urllib.request.urlopen(request, timeout=limit) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            self._mark_unavailable(exc)
        if len(raw) > 2 * 1024 * 1024:
            self._mark_unavailable("embedding service response too large")
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            self._mark_unavailable("invalid embedding service response: " + str(exc))

    def health(self, *, timeout=0.35):
        if self.disabled:
            return False
        try:
            result = self._request("/health", timeout=timeout)
        except EmbeddingUnavailable:
            return False
        if not isinstance(result, dict) or not result.get("ok"):
            self._last_error = "embedding service health check failed"
            return False
        self._set_identity(result)
        try:
            self.dim = int(result.get("dim") or self.dim)
        except (TypeError, ValueError):
            pass
        self._unavailable_until = 0.0
        self._last_error = ""
        return True

    def embed(self, texts, *, mode="document", timeout=None):
        if not isinstance(texts, (list, tuple)) or not texts:
            raise ValueError("texts must be a non-empty list")
        texts = [str(text) if isinstance(text, str) else "" for text in texts]
        if len(texts) > 32:
            raise ValueError("at most 32 texts per request")
        if any(not text for text in texts):
            raise ValueError("empty text")
        prepared = [text[:4000] for text in texts]
        if mode not in {"query", "document"}:
            raise ValueError("mode must be query or document")

        cache_keys = None
        if mode == "query":
            identity = f"{self.model_id}\0{self.revision}\0{self.fingerprint}\0"
            cache_keys = [hashlib.sha256((identity + text).encode("utf-8")).hexdigest()
                          for text in prepared]
            with self._lock:
                cached = [self._cache.get(key) for key in cache_keys]
                if all(item is not None for item in cached):
                    for key in cache_keys:
                        self._cache.move_to_end(key)
                    return [list(item) for item in cached]

        if time.monotonic() < self._unavailable_until:
            raise EmbeddingUnavailable(self._last_error or "embedding service cooling down")
        response = self._request(
            "/embed",
            {"texts": prepared, "mode": mode, "model": self.model_id, "revision": self.revision},
            timeout=timeout,
        )
        if not isinstance(response, dict) or not response.get("ok"):
            self._mark_unavailable((response or {}).get("error", "embedding request failed"))
        vectors = response.get("vectors")
        if not isinstance(vectors, list) or len(vectors) != len(prepared):
            self._mark_unavailable("embedding service returned the wrong number of vectors")
        result = []
        for vector in vectors:
            if not isinstance(vector, list):
                self._mark_unavailable("embedding vector is not a list")
            try:
                result.append(_normalise(vector))
            except (TypeError, ValueError, EmbeddingUnavailable) as exc:
                self._mark_unavailable(exc)
        self._set_identity(response)
        try:
            self.dim = int(response.get("dim") or len(result[0]))
        except (TypeError, ValueError):
            self.dim = len(result[0])
        self._unavailable_until = 0.0
        self._last_error = ""
        if cache_keys:
            with self._lock:
                for key, vector in zip(cache_keys, result):
                    self._cache[key] = tuple(vector)
                    self._cache.move_to_end(key)
                while len(self._cache) > MAX_QUERY_CACHE:
                    self._cache.popitem(last=False)
        return result

    def embed_one(self, text, *, mode="query", timeout=None):
        return self.embed([text], mode=mode, timeout=timeout)[0]


_DEFAULT = None
_DEFAULT_LOCK = threading.Lock()


def default_client():
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = EmbeddingClient()
        return _DEFAULT
