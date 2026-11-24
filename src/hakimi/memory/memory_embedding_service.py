"""Optional single-instance CPU semantic embedding service (loopback only).

Install the optional model explicitly, then run this process next to the bot:

    python -m pip install -r requirements-memory.txt
    python memory_embedding_service.py --download-model
    python memory_embedding_service.py --serve \
        --db /opt/hakimi/state/memory.sqlite3 --host 127.0.0.1 --port 18766

Normal startup never downloads a model.  It only loads a model already present in
the local cache.  The process is the only place that loads the model; short-lived
qq_cli processes use memory_embedding_client.py over 127.0.0.1.
"""
from __future__ import annotations

from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_REVISION = ""
DEFAULT_DIM = 512
MAX_BATCH = 32
MAX_TEXT_CHARS = 4000
MAX_BODY_BYTES = 512 * 1024


class ModelNotInstalled(RuntimeError):
    pass


def _cache_roots(cache_dir=None):
    roots = []
    if cache_dir:
        roots.append(Path(cache_dir).expanduser())
    for key in ("FASTEMBED_CACHE_PATH", "HAKIMI_EMBEDDING_CACHE"):
        value = os.environ.get(key)
        if value:
            roots.append(Path(value).expanduser())
    roots.extend([
        Path.home() / ".local/share/hakimi-memory-encoder/models",
        Path.home() / ".cache" / "fastembed",
        Path(tempfile.gettempdir()) / "fastembed_cache",
        Path.home() / ".cache" / "huggingface" / "hub",
        Path(os.environ.get("LOCALAPPDATA", Path.home())) / "fastembed",
    ])
    result = []
    seen = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            result.append(root)
    return result


def _looks_installed(model_name, cache_dir=None):
    """Conservative local-cache check that also understands HF hub snapshots."""
    slug = model_name.replace("/", "--")
    leaf = model_name.rsplit("/", 1)[-1]
    for root in _cache_roots(cache_dir):
        if not root.exists():
            continue
        if root.is_dir():
            try:
                if any(root.glob("*.onnx")):
                    return True
            except OSError:
                pass
        for pattern in (
            f"**/models--*{leaf}*/**/*.onnx",
            f"**/*{leaf}*/**/*.onnx",
            f"**/{slug}/**/*.onnx",
            f"**/{model_name}/**/*.onnx",
            f"**/*{leaf}*/**/model_optimized.onnx",
        ):
            try:
                if next(root.glob(pattern), None) is not None:
                    return True
            except OSError:
                continue
    return False


def _load_manifest(cache_dir=None):
    """Load the pinned source/manifest once; it is authoritative when present."""
    candidates = []
    value = os.environ.get("HAKIMI_EMBEDDING_MANIFEST")
    if value:
        candidates.append(Path(value).expanduser())
    for root in _cache_roots(cache_dir)[:2]:
        candidates.extend((root / "model-manifest.json", root.parent / "model-manifest.json"))
    seen = set()
    for path in candidates:
        key = str(path)
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            continue
        if isinstance(data, dict):
            data["_manifest_path"] = str(path)
            return data
    return {}


def _cache_identity(model_name, cache_dir=None):
    """Return (revision, file fingerprint) from the actual local model files."""
    leaf = model_name.rsplit("/", 1)[-1]
    files = []
    revision = ""
    for root in _cache_roots(cache_dir):
        if not root.exists():
            continue
        for pattern in (f"**/*{leaf}*/**/model*.onnx", f"**/*{leaf}*/**/config.json",
                        f"**/*{leaf}*/**/tokenizer*.json", f"**/*{leaf}*/**/special_tokens_map.json"):
            try:
                files.extend(root.glob(pattern))
            except OSError:
                continue
        if files:
            break
    files = sorted({path for path in files if path.is_file()})
    if not files:
        return revision, ""
    digest = hashlib.sha256()
    for path in files[:24]:
        digest.update(path.name.encode("utf-8", "replace"))
        try:
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError:
            continue
        parts = path.parts
        if "snapshots" in parts:
            index = len(parts) - 1 - parts[::-1].index("snapshots")
            if index + 1 < len(parts):
                candidate = parts[index + 1]
                if 6 <= len(candidate) <= 80 and all(ch.isalnum() or ch in "-_" for ch in candidate):
                    revision = candidate
    fingerprint = "sha256:" + digest.hexdigest()
    return revision or fingerprint, fingerprint


class FastEmbedEncoder:
    """Loaded once; serialised because FastEmbed sessions are not guaranteed thread-safe."""

    def __init__(self, model_name=DEFAULT_MODEL, *, cache_dir=None, threads=None,
                 allow_download=False):
        self.model_id = model_name
        self.manifest = _load_manifest(cache_dir)
        self.revision = str(os.environ.get("HAKIMI_EMBEDDING_REVISION", DEFAULT_REVISION)
                            or self.manifest.get("revision") or self.manifest.get("model_revision") or "")
        self.fingerprint = str(self.manifest.get("sha256") or self.manifest.get("fingerprint")
                               or self.manifest.get("model_sha256") or "")
        try:
            self.dim = int(self.manifest.get("dim") or self.manifest.get("dimensions")
                           or os.environ.get("HAKIMI_EMBEDDING_DIM", str(DEFAULT_DIM)))
        except (TypeError, ValueError):
            self.dim = DEFAULT_DIM
        self._lock = threading.Lock()
        self._query_cache = OrderedDict()
        self._cache_dir = cache_dir
        self._model = None
        self._threads = threads
        self._allow_download = allow_download
        self._load()
        cache_revision, cache_fingerprint = _cache_identity(self.model_id, self._cache_dir)
        if not self.fingerprint:
            self.fingerprint = cache_fingerprint
        if not self.revision:
            self.revision = cache_revision or self.fingerprint or "unversioned"

    def _load(self):
        if not self._allow_download:
            # Hard offline guard, not a best-effort hint.  Set it before importing
            # FastEmbed/HuggingFace so module-level offline checks also see it.
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
        else:
            os.environ["HF_HUB_OFFLINE"] = "0"
            os.environ.pop("TRANSFORMERS_OFFLINE", None)
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise ModelNotInstalled(
                "fastembed is not installed; install optional requirements-memory.txt first"
            ) from exc
        kwargs = {"model_name": self.model_id, "cache_dir": str(self._cache_dir) if self._cache_dir else None}
        if kwargs["cache_dir"] is None:
            kwargs.pop("cache_dir")
        if self._threads:
            kwargs["threads"] = int(self._threads)
        if not self._allow_download:
            # The actual tested FastEmbed 0.9.1 accepts local_files_only; never
            # silently retry without it, because that could mean a download.
            kwargs["local_files_only"] = True
        try:
            self._model = TextEmbedding(**kwargs)
        except TypeError as exc:
            raise ModelNotInstalled(
                "installed FastEmbed does not support local_files_only; "
                "refusing to risk an implicit model download"
            ) from exc
        except Exception as exc:
            raise ModelNotInstalled(f"embedding model could not be loaded from local cache: {exc}") from exc

    def embed(self, texts, *, mode="document"):
        workload = [str(text)[:MAX_TEXT_CHARS] for text in texts]
        if not workload:
            return []
        cache_keys = None
        if mode == "query":
            import hashlib
            cache_keys = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in workload]
            with self._lock:
                if all(key in self._query_cache for key in cache_keys):
                    for key in cache_keys:
                        self._query_cache.move_to_end(key)
                    return [list(self._query_cache[key]) for key in cache_keys]
        with self._lock:
            try:
                raw = list(self._model.embed(workload, batch_size=min(MAX_BATCH, len(workload))))
            except Exception as exc:
                raise RuntimeError(f"encoder failed: {type(exc).__name__}") from exc
        result = []
        for vector in raw:
            values = [float(value) for value in vector]
            if not values:
                raise RuntimeError("encoder returned empty vector")
            norm = math.sqrt(sum(value * value for value in values))
            if not math.isfinite(norm) or norm <= 1e-12:
                raise RuntimeError("encoder returned non-finite vector")
            result.append([value / norm for value in values])
        if result:
            self.dim = len(result[0])
        if cache_keys:
            with self._lock:
                for key, vector in zip(cache_keys, result):
                    self._query_cache[key] = tuple(vector)
                    self._query_cache.move_to_end(key)
                while len(self._query_cache) > 1024:
                    self._query_cache.popitem(last=False)
        return result


class Service:
    def __init__(self, encoder, *, max_queue_wait=5.0):
        self.encoder = encoder
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embedding")
        self.semaphore = threading.BoundedSemaphore(1)
        self.max_queue_wait = max_queue_wait
        self.started_at = time.time()
        self.requests = 0
        self.errors = 0

    def embed(self, texts, mode):
        if not self.semaphore.acquire(timeout=self.max_queue_wait):
            raise TimeoutError("embedding queue is busy")
        try:
            future = self.executor.submit(self.encoder.embed, texts, mode=mode)
            try:
                return future.result(timeout=60.0)
            except FutureTimeout as exc:
                future.cancel()
                raise TimeoutError("embedding timed out") from exc
        finally:
            self.semaphore.release()


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "HakimiMemoryEmbedding/1"
        sys_version = ""

        def log_message(self, fmt, *args):
            if os.environ.get("HAKIMI_EMBEDDING_LOG", "").lower() in {"1", "true"}:
                print("%s - %s" % (self.address_string(), fmt % args), file=sys.stderr)

        def _send(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"ok": True, "model": service.encoder.model_id,
                                 "revision": service.encoder.revision,
                                 "fingerprint": service.encoder.fingerprint,
                                 "dim": service.encoder.dim,
                                 "uptime": round(time.time() - service.started_at, 1)})
            elif self.path == "/status":
                self._send(200, {"ok": True, "model": service.encoder.model_id,
                                 "revision": service.encoder.revision,
                                 "fingerprint": service.encoder.fingerprint,
                                 "dim": service.encoder.dim,
                                 "requests": service.requests, "errors": service.errors,
                                 "started_at": service.started_at})
            else:
                self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if self.path != "/embed":
                self._send(404, {"ok": False, "error": "not found"})
                return
            length = self.headers.get("Content-Length")
            try:
                length = int(length or "0")
            except ValueError:
                self._send(400, {"ok": False, "error": "invalid content-length"})
                return
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send(413, {"ok": False, "error": "body too large"})
                return
            raw = self.rfile.read(length)
            try:
                request = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                self._send(400, {"ok": False, "error": "invalid JSON"})
                return
            texts = request.get("texts") if isinstance(request, dict) else None
            mode = request.get("mode", "document") if isinstance(request, dict) else "document"
            if (not isinstance(texts, list) or not 1 <= len(texts) <= MAX_BATCH
                    or any(not isinstance(text, str) or not text or len(text) > MAX_TEXT_CHARS for text in texts)):
                self._send(400, {"ok": False, "error": "texts must be 1-32 non-empty strings <=4000 chars"})
                return
            if mode not in {"query", "document"}:
                self._send(400, {"ok": False, "error": "mode must be query or document"})
                return
            service.requests += 1
            try:
                vectors = service.embed(texts, mode)
            except TimeoutError as exc:
                service.errors += 1
                self._send(503, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                service.errors += 1
                print("embedding error: %s" % type(exc).__name__, file=sys.stderr)
                self._send(500, {"ok": False, "error": "encoder failure"})
                return
            self._send(200, {"ok": True, "model": service.encoder.model_id,
                             "revision": service.encoder.revision,
                             "fingerprint": service.encoder.fingerprint,
                             "dim": service.encoder.dim,
                             "vectors": vectors})

    return Handler


class IndexThread(threading.Thread):
    """Incremental background writer; inference happens outside DB write transactions."""

    def __init__(self, db_path, encoder, *, interval=5.0, batch=16):
        super().__init__(name="memory-embedding-index", daemon=True)
        self.db_path = str(db_path)
        self.encoder = encoder
        self.interval = interval
        self.batch = batch
        self.stop_event = threading.Event()
        self.last = {"indexed": 0, "pending": 0, "error": ""}

    def run(self):
        base = str(Path(self.db_path).resolve().parent.parent)
        try:
            from hakimi.memory.memory_store import MemoryStore
            store = MemoryStore(base, encoder=self.encoder)
        except Exception as exc:
            self.last["error"] = f"indexer init failed: {type(exc).__name__}: {exc}"
            return
        while not self.stop_event.is_set():
            try:
                result = store.index_pending_embeddings(self.encoder, limit=self.batch)
                self.last = result
            except Exception as exc:
                self.last = {"indexed": 0, "pending": -1,
                             "error": f"{type(exc).__name__}: {str(exc)[:180]}"}
            self.stop_event.wait(self.interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local CPU semantic memory embedding service")
    parser.add_argument("--serve", action="store_true", help="run loopback HTTP service")
    parser.add_argument("--download-model", action="store_true", help="explicitly download the model, then exit")
    parser.add_argument("--check", action="store_true", help="check local model availability")
    parser.add_argument("--db", default="", help="optional state/memory.sqlite3 for background indexing")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18766)
    parser.add_argument("--model", default=os.environ.get("HAKIMI_EMBEDDING_MODEL", DEFAULT_MODEL))
    parser.add_argument("--cache-dir", default=os.environ.get("HAKIMI_EMBEDDING_CACHE", ""))
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--index-interval", type=float, default=5.0)
    parser.add_argument("--index-batch", type=int, default=16)
    args = parser.parse_args(argv)
    if not (args.serve or args.download_model or args.check):
        parser.print_help()
        return 2
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        print("refusing to bind a non-loopback address", file=sys.stderr)
        return 2
    cache_dir = args.cache_dir or None
    if args.download_model:
        try:
            encoder = FastEmbedEncoder(args.model, cache_dir=cache_dir,
                                       threads=args.threads or None, allow_download=True)
            encoder.embed(["离线模型安装自检"], mode="query")
        except Exception as exc:
            print(f"model download failed: {exc}", file=sys.stderr)
            return 2
        print(f"model ready: {encoder.model_id} ({encoder.dim} dims, revision {encoder.revision})")
        return 0
    try:
        encoder = FastEmbedEncoder(args.model, cache_dir=cache_dir, threads=args.threads or None,
                                   allow_download=False)
    except ModelNotInstalled as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.check:
        try:
            vector = encoder.embed(["本地模型自检"], mode="query")[0]
        except Exception as exc:
            print(f"model self-check failed: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"ok": True, "model": encoder.model_id, "revision": encoder.revision,
                          "fingerprint": encoder.fingerprint, "dim": len(vector)},
                         ensure_ascii=False))
        return 0
    service = Service(encoder)
    indexer = None
    if args.db:
        db_path = Path(args.db).expanduser().resolve()
        if not db_path.is_file():
            print(f"database not found: {db_path}", file=sys.stderr)
            return 2
        indexer = IndexThread(db_path, encoder, interval=max(1.0, args.index_interval),
                              batch=max(1, min(128, args.index_batch)))
        indexer.start()
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(service))
    httpd.daemon_threads = True
    print(f"memory embedding service on http://{args.host}:{args.port} "
          f"model={encoder.model_id} revision={encoder.revision} dim={encoder.dim} "
          f"indexer={'on' if indexer else 'off'}")
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if indexer:
            indexer.stop_event.set()
            indexer.join(timeout=5)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
