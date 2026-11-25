"""Memory store schema, scoring constants and pure text/vector helpers."""
import hashlib
import math
import operator
import os
import re
import struct


SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
 id INTEGER PRIMARY KEY, source_key TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
 session TEXT NOT NULL, user_id TEXT NOT NULL, key TEXT NOT NULL, text TEXT NOT NULL,
 at REAL NOT NULL, expires REAL NOT NULL, origin TEXT NOT NULL, source TEXT NOT NULL,
 summary TEXT NOT NULL DEFAULT '', vector BLOB NOT NULL DEFAULT X'');
CREATE INDEX IF NOT EXISTS records_person ON records(user_id,kind,key,at);
CREATE INDEX IF NOT EXISTS records_session ON records(session,at);
CREATE INDEX IF NOT EXISTS records_time ON records(kind,at);
CREATE INDEX IF NOT EXISTS records_note ON records(kind,session,key,at);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(tokens);
CREATE TABLE IF NOT EXISTS edges (
 record_id INTEGER NOT NULL, subject TEXT NOT NULL, relation TEXT NOT NULL, object TEXT NOT NULL,
 PRIMARY KEY(record_id,subject,relation,object));
CREATE INDEX IF NOT EXISTS edges_object ON edges(relation,object,record_id);
CREATE INDEX IF NOT EXISTS edges_subject ON edges(subject,relation,record_id);
CREATE TABLE IF NOT EXISTS cursors (name TEXT PRIMARY KEY, identity TEXT, offset INTEGER);
"""
EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_originals (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 record_id INTEGER,
 request_id TEXT NOT NULL,
 content_hash TEXT NOT NULL,
 text TEXT NOT NULL,
 kind TEXT NOT NULL,
 session TEXT NOT NULL,
 user_id TEXT NOT NULL DEFAULT '',
 item_key TEXT NOT NULL DEFAULT '',
 source TEXT NOT NULL DEFAULT '',
 evidence TEXT NOT NULL DEFAULT '',
 provider TEXT NOT NULL DEFAULT '',
 channel TEXT NOT NULL DEFAULT '',
 verification TEXT NOT NULL DEFAULT 'legacy',
 metadata TEXT NOT NULL DEFAULT '{}',
 created_at REAL NOT NULL,
 legacy INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS memory_originals_request ON memory_originals(request_id);
CREATE INDEX IF NOT EXISTS memory_originals_record ON memory_originals(record_id);
CREATE INDEX IF NOT EXISTS memory_originals_hash ON memory_originals(content_hash);
CREATE TABLE IF NOT EXISTS embeddings (
 record_id INTEGER NOT NULL,
 chunk_index INTEGER NOT NULL,
 start_offset INTEGER NOT NULL DEFAULT 0,
 end_offset INTEGER NOT NULL DEFAULT 0,
 model TEXT NOT NULL,
 revision TEXT NOT NULL DEFAULT '',
 dim INTEGER NOT NULL,
 content_hash TEXT NOT NULL,
 vector BLOB NOT NULL,
 signature BLOB NOT NULL DEFAULT X'',
 updated_at REAL NOT NULL,
 PRIMARY KEY(record_id,chunk_index,model)
);
CREATE INDEX IF NOT EXISTS embeddings_model ON embeddings(model,record_id);
CREATE TABLE IF NOT EXISTS embedding_state (
 record_id INTEGER PRIMARY KEY,
 model TEXT NOT NULL,
 revision TEXT NOT NULL DEFAULT '',
 content_hash TEXT NOT NULL,
 chunks INTEGER NOT NULL DEFAULT 0,
 updated_at REAL NOT NULL,
 error TEXT NOT NULL DEFAULT '',
 attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS memory_meta (key TEXT PRIMARY KEY, value TEXT);
"""
SELF_REPORT = re.compile(r"^(?:@哈基米\s*)?(?:(?:以后)?(叫我|称呼我|别叫我|不要叫我)|我(喜欢|不喜欢|在学|正在学|研究))\s*([^，。！？\n]{1,60})")
NAME_KEYS = {"称呼", "昵称", "名字", "叫法", "name"}
MANUAL_KINDS = {"person", "preference", "lore", "episode", "reflection", "commitment"}
# Legacy manual notes came from the two model backends (plus seed/import data).
# Deterministic/delivered automatic notes must not be promoted to manual originals.
LEGACY_MANUAL_PROVIDERS = {"gemini", "dsh", "seed", "legacy", "gemini_tool"}
try:
    DENSE_THRESHOLD = max(0.0, min(0.95, float(os.environ.get("HAKIMI_EMBEDDING_MIN_SCORE", "0.50"))))
except ValueError:
    DENSE_THRESHOLD = 0.50
RRF_K = 60
CHANNEL_WEIGHT = {
    "lexical": 1.0, "dense": 1.15, "card": 0.95, "preference": 0.65,
    "name": 0.95, "recall": 0.75, "graph": 0.6,
}
QUALITY = {
    "maintainer_checked_description": 0.030,
    "source_excerpt": 0.024,
    "self_report": 0.020,
    "observed_dialogue": 0.010,
    "model_note": 0.006,
    "subjective_reflection": 0.002,
    "answer_unverified": -0.020,
    "legacy": -0.004,
}


def key_name(key):
    return "称呼" if key in NAME_KEYS else str(key)


def index_text(text):
    return re.sub(r"\[CQ:[^\]]*\]|https?://\S+|[A-Za-z]:[/\\][^\s\]]+|<faceType[^>]*>", " ", str(text))


def vectorize(tokens):
    """Normalized sparse feature-hash vector, kept as a lexical fallback only."""
    values = {}
    for token in tokens:
        dimension = int.from_bytes(hashlib.blake2s(token.encode(), digest_size=2).digest(), "little")
        values[dimension] = values.get(dimension, 0) + 1
    norm = math.sqrt(sum(value * value for value in values.values())) or 1
    return b"".join(struct.pack("<Hf", key, value / norm) for key, value in sorted(values.items()))


def cosine(vector, query):
    return sum(value * query.get(key, 0) for key, value in struct.iter_unpack("<Hf", vector))


def content_hash(text):
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def chunk_text(text, *, max_chars=900, overlap=100):
    """Deterministic character-offset chunks; the original text is never changed."""
    text = str(text)
    length = len(text)
    if length <= max_chars:
        return [(0, length, text)] if text else []
    chunks = []
    start = 0
    while start < length:
        end = min(length, start + max_chars)
        if end < length:
            window = text[start + max_chars // 2:end]
            cut = max((window.rfind(mark) for mark in ("\n\n", "\n", "。", "！", "？", "；", "，", " ")), default=-1)
            if cut >= 0:
                end = start + max_chars // 2 + cut + 1
        if end <= start:
            end = min(length, start + max_chars)
        chunks.append((start, end, text[start:end]))
        if end >= length:
            break
        next_start = max(start + 1, end - overlap)
        if next_start <= start:
            next_start = end
        start = next_start
    return chunks


def _summary(text):
    text = str(text)
    if len(text) <= 320:
        return ""
    return text[:280].rstrip() + "…"


def _normalise_vector(values):
    values = [float(value) for value in values]
    norm = math.sqrt(sum(value * value for value in values))
    if not values or not math.isfinite(norm) or norm <= 1e-12:
        raise ValueError("empty embedding")
    return [value / norm for value in values]


def _vector_blob(values):
    return struct.pack("<" + "f" * len(values), *values)


def _vector_values(blob, dim=None):
    if not blob:
        return []
    return [value[0] for value in struct.iter_unpack("<f", blob)]


def _signature(values):
    data = bytearray((len(values) + 7) // 8)
    for index, value in enumerate(values):
        if value > 0:
            data[index // 8] |= 1 << (index % 8)
    return bytes(data)


def _hamming(left, right):
    if not left or not right or len(left) != len(right):
        return 10 ** 9
    if type(left) is bytes and type(right) is bytes:
        return (int.from_bytes(left, "little") ^ int.from_bytes(right, "little")).bit_count()
    return sum((a ^ b).bit_count() for a, b in zip(left, right))


def _dense_cosine(blob, query):
    if not blob:
        return -1.0
    if type(blob) is bytes and len(blob) % 4 == 0:
        # Keep the original rounding of each product and the built-in sum.
        values = struct.unpack(f"<{len(blob) // 4}f", blob)
        return sum(map(operator.mul, values, query))
    return sum(value[0] * other for value, other in zip(struct.iter_unpack("<f", blob), query))
