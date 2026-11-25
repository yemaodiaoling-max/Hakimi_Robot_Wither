"""MemoryStore semantic vectors: pending work, status and dense candidate search."""
import heapq
import sqlite3
import time
from hakimi.memory.store_base import DENSE_THRESHOLD, _dense_cosine, _hamming, _normalise_vector, _signature, _vector_blob, chunk_text


class StoreEmbeddingMixin:
    """Mixin of MemoryStore; see memory_store."""

    # ------------------------------------------------------------- embeddings
    def pending_embedding_count(self, model=None, revision=None):
        sql = ("SELECT count(*) FROM records r LEFT JOIN embedding_state s ON s.record_id=r.id "
               "WHERE r.state='active' AND (r.text!='' OR r.summary!='') AND "
               "(s.record_id IS NULL OR s.content_hash!=r.content_hash")
        params = []
        if model is not None:
            sql += " OR s.model!=?"
            params.append(model)
        if revision is not None:
            sql += " OR s.revision!=?"
            params.append(revision)
        sql += ")"
        with self.db() as db:
            return db.execute(sql, params).fetchone()[0]

    def embedding_status(self, record_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM embedding_state WHERE record_id=?", (int(record_id),)).fetchone()
        return dict(row) if row else None

    def index_pending_embeddings(self, encoder=None, *, limit=16, now=None):
        """Incrementally embed new/changed records. Inference never holds a write lock."""
        now = time.time() if now is None else now
        encoder = encoder or self.encoder_instance()
        model, revision = self._refresh_encoder_identity(encoder)
        with self.db() as db:
            rows = db.execute(
                "SELECT r.id,r.text,r.summary,r.content_hash,s.content_hash AS old_hash,s.model AS old_model,"
                "s.revision AS old_revision,s.updated_at AS old_updated,s.error,s.attempts FROM records r "
                "LEFT JOIN embedding_state s ON s.record_id=r.id "
                "WHERE r.state='active' AND (r.text!='' OR r.summary!='') AND "
                "(s.record_id IS NULL OR s.content_hash!=r.content_hash OR s.model!=? OR s.revision!=? OR "
                "(s.error!='' AND s.updated_at<?)) ORDER BY r.id LIMIT ?",
                (model, revision, now - 300, max(1, min(int(limit), 128))),
            ).fetchall()
        indexed = 0
        errors = 0
        for row in rows:
            text = row['text'] or row['summary']
            chunks = chunk_text(text)
            if not chunks:
                with self.db() as db:
                    db.execute("INSERT OR REPLACE INTO embedding_state VALUES (?,?,?,?,?,?,?,?)",
                               (row['id'], model, revision, row['content_hash'], 0, now, '', 0))
                continue
            try:
                vectors = encoder.embed([chunk for _, _, chunk in chunks], mode="document")
                if len(vectors) != len(chunks):
                    raise ValueError('wrong number of vectors')
                normalised = [_normalise_vector(vector) for vector in vectors]
                dims = len(normalised[0])
                if any(len(vector) != dims for vector in normalised):
                    raise ValueError('inconsistent vector dimensions')
            except Exception as exc:
                errors += 1
                with self.db() as db:
                    attempts = int(row['attempts'] or 0) + 1
                    db.execute(
                        "INSERT INTO embedding_state(record_id,model,revision,content_hash,chunks,updated_at,error,attempts) "
                        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(record_id) DO UPDATE SET "
                        "model=excluded.model,revision=excluded.revision,content_hash=excluded.content_hash,"
                        "updated_at=excluded.updated_at,error=excluded.error,attempts=excluded.attempts",
                        (row['id'], model, revision, row['content_hash'], len(chunks), now,
                         f"{type(exc).__name__}: {str(exc)[:160]}", attempts))
                continue
            try:
                with self.db() as db:
                    current = db.execute("SELECT content_hash,text,summary FROM records WHERE id=?",
                                         (row['id'],)).fetchone()
                    if not current or current['content_hash'] != row['content_hash']:
                        continue
                    if (current['text'] or current['summary']) != (row['text'] or row['summary']):
                        continue
                    db.execute("DELETE FROM embeddings WHERE record_id=?", (row['id'],))
                    for index, ((start, end, _), vector) in enumerate(zip(chunks, normalised)):
                        db.execute(
                            "INSERT INTO embeddings(record_id,chunk_index,start_offset,end_offset,model,revision,dim,"
                            "content_hash,vector,signature,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (row['id'], index, start, end, model, revision, dims, row['content_hash'],
                             _vector_blob(vector), _signature(vector), now),
                        )
                    db.execute(
                        "INSERT INTO embedding_state(record_id,model,revision,content_hash,chunks,updated_at,error,attempts) "
                        "VALUES (?,?,?,?,?,?,'',0) ON CONFLICT(record_id) DO UPDATE SET "
                        "model=excluded.model,revision=excluded.revision,content_hash=excluded.content_hash,"
                        "chunks=excluded.chunks,updated_at=excluded.updated_at,error='',attempts=0",
                        (row['id'], model, revision, row['content_hash'], len(chunks), now))
                indexed += 1
            except sqlite3.Error:
                errors += 1
        return {'indexed': indexed, 'errors': errors,
                'pending': self.pending_embedding_count(model, revision),
                'model': model, 'revision': revision}

    def _dense_candidates(self, db, query_vector, model, revision, scope, params, now, *,
                          pool=256, threshold=DENSE_THRESHOLD):
        dim = len(query_vector)
        rows = db.execute(
            "SELECT e.record_id,e.dim,e.signature FROM embeddings e JOIN records r ON r.id=e.record_id "
            "WHERE e.model=? AND e.revision=? AND e.dim=? AND e.content_hash=r.content_hash "
            "AND length(e.vector)=? AND " + scope + " AND (r.expires=0 OR r.expires>?) LIMIT 30000",
            [model, revision, dim, dim * 4, *params, now],
        ).fetchall()
        if not rows:
            return []
        query_signature = _signature(query_vector)
        ranked = []
        for row in rows:
            if int(row['dim']) != dim:
                continue
            ranked.append((_hamming(row['signature'], query_signature), row['record_id']))
        count = max(8, min(int(pool), len(ranked)))
        # Preserve duplicates and the record ID tie-breaker in the tuple ordering.
        if count * 10 < len(ranked):
            ranked = heapq.nsmallest(count, ranked)
        else:
            ranked.sort()
        selected = [record_id for _, record_id in ranked[:count]]
        if not selected:
            return []
        marks = ','.join('?' for _ in selected)
        vectors = db.execute(
            "SELECT e.record_id,e.chunk_index,e.start_offset,e.end_offset,e.vector FROM embeddings e JOIN records r ON r.id=e.record_id "
            f"WHERE e.model=? AND e.revision=? AND e.dim=? AND e.content_hash=r.content_hash "
            f"AND length(e.vector)=? AND e.record_id IN ({marks})",
            [model, revision, dim, dim * 4, *selected],
        ).fetchall()
        best = {}
        for row in vectors:
            score = _dense_cosine(row['vector'], query_vector)
            if score >= threshold and score > best.get(row['record_id'], (-1, None))[0]:
                best[row['record_id']] = (score, {key: row[key] for key in
                    ('chunk_index', 'start_offset', 'end_offset')})
        if not best:
            return []
        marks = ','.join('?' for _ in best)
        records = db.execute(f"SELECT * FROM records WHERE id IN ({marks})", list(best)).fetchall()
        result = []
        for record in records:
            score, chunk = best[record['id']]
            item = dict(record)
            item['_dense_chunk'] = chunk
            result.append((item, score))
        result.sort(key=lambda pair: pair[1], reverse=True)
        return result
