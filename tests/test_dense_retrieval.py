"""Regression coverage for dense retrieval arithmetic and candidate ordering."""
import math
import random
import sqlite3
import struct
import unittest
from contextlib import closing

from hakimi.memory.store_base import _dense_cosine, _hamming, _vector_blob
from hakimi.memory.store_embeddings import StoreEmbeddingMixin


class DenseRetrievalTests(unittest.TestCase):
    def test_hamming_matches_bytewise_count(self):
        rng = random.Random(325)
        for length in (1, 8, 64, 96, 128):
            left, right = rng.randbytes(length), rng.randbytes(length)
            expected = sum((a ^ b).bit_count() for a, b in zip(left, right))
            self.assertEqual(_hamming(left, right), expected)
            self.assertEqual(_hamming(bytearray(left), memoryview(right)), expected)
        self.assertEqual(_hamming(b'', b''), 10 ** 9)
        self.assertEqual(_hamming(b'abc', b'ab'), 10 ** 9)
        with self.assertRaises(TypeError):
            _hamming('a', b'a')

    def test_dense_score_matches_rounded_products(self):
        rng = random.Random(721)
        cases = [([1, 1, 1], [1e100, 1.0, -1e100])]
        for length in (1, 8, 384, 768, 1024):
            for _ in range(8):
                vector = [rng.uniform(-1, 1) for _ in range(length)]
                query = [rng.uniform(-1, 1) for _ in range(length)]
                cases.append((vector, query))
        for vector, query in cases:
            blob = _vector_blob(vector)
            expected = sum(a[0] * b for a, b in zip(struct.iter_unpack('<f', blob), query))
            self.assertEqual(_dense_cosine(blob, iter(query)).hex(), expected.hex())
        self.assertEqual(_dense_cosine(_vector_blob([1, 2]), [0.5]), 0.5)
        self.assertEqual(_dense_cosine(b'', [1.0]), -1.0)
        with self.assertRaisesRegex(struct.error, 'multiple of 4 bytes'):
            _dense_cosine(b'abc', [1.0])

    def test_dense_candidate_pool_preserves_ties_and_duplicate_chunks(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.row_factory = sqlite3.Row
            db.executescript("""
                CREATE TABLE records (id INTEGER PRIMARY KEY, content_hash TEXT, expires REAL);
                CREATE TABLE embeddings (
                    record_id INTEGER, chunk_index INTEGER, start_offset INTEGER,
                    end_offset INTEGER, model TEXT, revision TEXT, dim INTEGER,
                    content_hash TEXT, vector BLOB, signature BLOB
                );
            """)
            db.executemany("INSERT INTO records VALUES (?, 'hash', 0)",
                           [(record_id,) for record_id in range(1, 91)])
            # Insert tied signatures in reverse record order. Six pool slots go to
            # the first record, so only IDs 1-3 qualify when the pool is eight.
            rows = [(record_id, 0, _vector_blob([1, 1])) for record_id in range(90, 1, -1)]
            rows[-1] = (2, 0, _vector_blob([1, 0]))
            rows.extend((1, index, _vector_blob([1, int(index > 0)])) for index in range(6))
            db.executemany(
                "INSERT INTO embeddings VALUES (?, ?, 5, 10, 'model', 'rev', 2, 'hash', ?, X'03')",
                rows,
            )
            store = StoreEmbeddingMixin()
            epsilon = math.ulp(1.0)

            def retrieve(pool, threshold, low=1):
                results = store._dense_candidates(
                    db, [1.0, epsilon], 'model', 'rev', 'r.id >= ?', [low], 0,
                    pool=pool, threshold=threshold,
                )
                return [(record['id'], record['_dense_chunk']['chunk_index'], score)
                        for record, score in results]

            score = 1.0 + epsilon
            expected = [(1, 1, score), (3, 0, score), (2, 0, 1.0)]
            self.assertEqual(retrieve(0, True), expected)
            self.assertEqual(retrieve(8, score), expected[:2])
            self.assertEqual(retrieve(8, math.nextafter(score, math.inf)), [])
            self.assertEqual(retrieve(8, 1.0, low=85),
                             [(record_id, 0, score) for record_id in range(85, 91)])


if __name__ == '__main__':
    unittest.main()
