"""v3 manual-memory, semantic-retrieval and provenance-graph tests (no network)."""
import contextlib
import hashlib
import io
import json
import math
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

here = Path(__file__).resolve().parent
if here.name == 'tests':
    sys.path.insert(0, str(here.parent / 'official_test_runtime'))
    sys.path.insert(0, str(here.parent))
else:
    sys.path.insert(0, str(here / 'src'))

from hakimi.memory.memory_store import MemoryStore, chunk_text, content_hash
from hakimi.memory.smart_memory import valid_memory, remember_memory, save_memory, MAX_MANUAL_CHARS
from hakimi.memory.memory_embedding_client import EmbeddingUnavailable
from hakimi.console.memory_graph_view import render_html
from hakimi.memory import memory_embedding_eval
from _repo import repo_file


class FakeSemanticEncoder:
    """Deterministic stand-in for the real CPU encoder; no model is loaded."""
    model_id = 'fake-bge-zh-test'
    revision = 'test-v1'
    dim = 8

    def __init__(self):
        self.calls = []

    def embed(self, texts, mode='document'):
        self.calls.append((mode, list(texts)))
        rows = []
        for text in texts:
            text = str(text)
            vector = [0.01] * self.dim
            if any(word in text for word in ('矩阵', '线性映射', '射线', '子空间', '变换', '方向')):
                vector[0] += 1
            if any(word in text for word in ('火锅', '晚饭', '烧烤', '吃')):
                vector[1] += 1
            if any(word in text for word in ('猫', '哈基米', '贴贴')):
                vector[2] += 1
            if max(vector) <= 0.011:
                vector[7] += 1
            norm = math.sqrt(sum(value * value for value in vector))
            rows.append([value / norm for value in vector])
        return rows


class UnavailableEncoder:
    model_id = 'offline'
    revision = 'test'
    dim = 8

    def embed(self, texts, mode='document'):
        raise EmbeddingUnavailable('service down')


class ManualMemoryV3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=here)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        (self.base / 'state').mkdir(parents=True, exist_ok=True)
        self.now = time.time()
        self.store = MemoryStore(self.base)

    def save(self, note, kind='commitment', key='约定', **extra):
        proposal = {'kind': kind, 'note': note, 'key': key, **extra}
        return remember_memory(self.base, 'group:1', proposal, {'101': {}}, now=self.now)

    def test_exact_utf8_hash_and_pagination_reassembles(self):
        note = '  第一行：空格保留\n\n第二行 ✨ e\u0301\t结尾  \n'
        result = self.save(note)
        self.assertTrue(result['saved'])
        self.assertEqual(result['hash'], hashlib.sha256(note.encode('utf-8')).hexdigest())
        read = self.store.read(result['id'], 'group:1', {'101'}, limit=3)
        self.assertEqual(read['text_kind'], 'manual_original')
        self.assertEqual(read['total_chars'], len(note))
        self.assertTrue(read['has_more'])
        parts = [read['text']]
        while read['next_offset'] is not None:
            read = self.store.read(result['id'], 'group:1', {'101'}, offset=read['next_offset'], limit=3)
            parts.append(read['text'])
        self.assertEqual(''.join(parts), note)
        with self.store.db() as db:
            original = db.execute("SELECT text,content_hash,legacy FROM memory_originals WHERE record_id=?",
                                  (result['record_id'],)).fetchone()
            self.assertEqual(original[0], note)
            self.assertEqual(original[1], hashlib.sha256(note.encode('utf-8')).hexdigest())
            self.assertEqual(original[2], 0)

    def test_overlong_is_rejected_without_storage(self):
        note = 'x' * (MAX_MANUAL_CHARS + 1)
        result = self.save(note)
        self.assertFalse(result['saved'])
        self.assertEqual(result['error'], 'invalid_note')
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM memory_originals").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM records").fetchone()[0], 0)
        self.assertIsNone(valid_memory({'kind': 'commitment', 'key': 'x', 'note': note}))

    def test_retry_is_idempotent_and_uses_same_original(self):
        note = '同一条工具请求\n第二行'
        first = self.save(note)
        second = self.save(note)
        self.assertEqual(second['id'], first['id'])
        self.assertTrue(second['duplicate'])
        self.assertFalse(second['created'])
        self.assertEqual(second['hash'], first['hash'])
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM memory_originals").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM records").fetchone()[0], 1)
        path = self.base / 'state/session_memory.jsonl'
        self.assertEqual(len(path.read_text(encoding='utf-8').splitlines()), 1)

    def test_correction_keeps_old_version_and_never_resurrects_it(self):
        old_note = '旧约定：周六上午整理'
        old = self.save(old_note)
        new = self.save('新约定：周六晚上整理', topics=['约定'])
        self.assertNotEqual(old['id'], new['id'])
        old_read = self.store.read(old['id'], 'group:1', {'101'})
        self.assertEqual(old_read['status'], 'superseded')
        self.assertEqual(old_read['superseded_by'], new['id'])
        self.assertEqual(old_read['text'], old_note)
        self.assertEqual(old_read['hash'], old['hash'])
        with self.store.db() as db:
            old_row = db.execute("SELECT state,text FROM records WHERE id=?", (old['record_id'],)).fetchone()
            self.assertEqual(old_row['state'], 'superseded')
            self.assertEqual(old_row['text'], old_note)
            self.assertEqual(db.execute("SELECT count(*) FROM memory_originals").fetchone()[0], 2)
        hits = ' '.join(self.store.retrieve('group:1', [], {'text': '约定 整理', 'user_id': '101'}, now=self.now + 5))
        self.assertIn('晚上', hits)
        self.assertNotIn('上午', hits)

    def test_prune_never_deletes_manual_original(self):
        note = '长期保留的原文\n带换行'
        result = self.save(note)
        self.store.observe({'session_key': 'group:1', 'user_id': '202', 'message_id': 'old',
                            'received_at': self.now - 30 * 86400, 'text': '短期聊天'})
        self.store.prune(self.now)
        read = self.store.read(result['id'], 'group:1', {'101'})
        self.assertEqual(read['text'], note)
        self.assertEqual(read['status'], 'active')
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT text FROM memory_originals WHERE record_id=?", (result['record_id'],)).fetchone()[0], note)

    def test_fts_failure_keeps_original_and_rebuilds(self):
        original_method = MemoryStore._insert_search
        try:
            with patch.object(MemoryStore, '_insert_search', side_effect=sqlite3.OperationalError('disk busy')):
                result = self.save('索引失败也要保住原文')
        finally:
            MemoryStore._insert_search = original_method
        self.assertTrue(result['saved'])
        self.assertEqual(result['index_status'], 'pending_fts')
        read = self.store.read(result['id'], 'group:1', {'101'})
        self.assertEqual(read['text'], '索引失败也要保住原文')
        self.assertGreaterEqual(self.store.rebuild_search_index(), 1)
        hits = self.store.retrieve('group:1', [], {'text': '索引失败 原文', 'user_id': '101'}, now=self.now + 2)
        self.assertTrue(any('索引失败' in line for line in hits))

    def test_related_ids_must_be_readable_and_topics_are_connected(self):
        first = self.save('第一条可关联笔记', topics=['线性代数'])
        second = self.save('第二条关联笔记', topics=['线性代数'], related_memory_ids=[first['id']])
        self.assertTrue(second['saved'])
        with self.store.db() as db:
            self.assertGreaterEqual(db.execute(
                "SELECT count(*) FROM graph_edges WHERE relation='refers_to' AND subject=?",
                ('memory:' + str(second['record_id']),)).fetchone()[0], 1)
        invalid = remember_memory(self.base, 'group:1', {'kind': 'commitment', 'key': '坏链接',
                                 'note': '链接不存在', 'related_memory_ids': ['M999']}, {'101': {}}, now=self.now)
        self.assertFalse(invalid['saved'])
        self.assertEqual(invalid['error'], 'related_memory_unreadable')
        private = remember_memory(self.base, 'private:202', {'kind': 'commitment', 'key': '私聊链接',
                                  'note': '不应读到群里的非共享笔记', 'related_memory_ids': [first['id']]},
                                  {'202': {}}, now=self.now)
        self.assertFalse(private['saved'])
        self.assertEqual(private['error'], 'related_memory_unreadable')

    def test_reflection_is_labelled_subjective(self):
        result = self.save('我觉得她今天有点累，可能只是我的理解', kind='reflection', key='互动理解')
        read = self.store.read(result['id'], 'group:1', {'101'})
        self.assertEqual(read['verification'], 'subjective_reflection')
        hits = self.store.retrieve('group:1', [], {'text': '互动理解 今天', 'user_id': '101'}, now=self.now + 1)
        self.assertTrue(any('主观反思' in line for line in hits))

    def test_jsonl_roundtrip_keeps_exact_text_and_does_not_duplicate(self):
        note = 'JSONL 往返\n  保留空格和换行 ✨  '
        result = self.save(note)
        fresh = MemoryStore(self.base)
        fresh.sync()
        read = fresh.read(result['id'], 'group:1', {'101'})
        self.assertEqual(read['text'], note)
        with fresh.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM records").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM memory_originals WHERE record_id=?",
                                        (result['record_id'],)).fetchone()[0], 1)

    def test_legacy_jsonl_is_marked_and_exact_jsonl_can_rebuild(self):
        legacy = Path(str(self.base) + '_legacy')
        (legacy / 'state').mkdir(parents=True)
        old = {'kind': 'preference', 'session_key': 'group:9', 'key': '旧事项',
               'note': '旧笔记  \n格式已不可考', 'created_at': self.now, 'provider': 'legacy'}
        (legacy / 'state/session_memory.jsonl').write_text(json.dumps(old, ensure_ascii=False) + '\n', encoding='utf-8')
        index = MemoryStore(legacy)
        index.sync()
        with index.db() as db:
            row = db.execute("SELECT id FROM records WHERE session='group:9'").fetchone()
        read = index.read('M' + str(row[0]), 'group:9', {'101'})
        self.assertTrue(read['legacy'])
        self.assertEqual(read['text_kind'], 'legacy_note')
        self.assertEqual(read['text'], old['note'])
        self.assertIsNotNone(read['original_id'])

        rebuilt = Path(str(self.base) + '_rebuilt')
        (rebuilt / 'state').mkdir(parents=True)
        exact_note = '  逐字符重建\n第二行  '
        saved = self.save(exact_note)
        (rebuilt / 'state/session_memory.jsonl').write_bytes(
            (self.base / 'state/session_memory.jsonl').read_bytes())
        fresh = MemoryStore(rebuilt)
        fresh.sync()
        with fresh.db() as db:
            row = db.execute("SELECT id,legacy FROM records WHERE kind='commitment'").fetchone()
        read = fresh.read('M' + str(row[0]), 'group:1', {'101'})
        self.assertEqual(read['text'], exact_note)
        self.assertFalse(read['legacy'])
        self.assertEqual(read['hash'], saved['hash'])
        self.assertEqual(read['text_kind'], 'manual_original')

    def test_fallback_json_accepts_new_reflection_kind_verbatim(self):
        from hakimi.models.fallback_runner import parse_response
        note = '  我觉得她可能只是累了\n这只是我的理解  '
        parsed = parse_response(json.dumps({'action': 'silent', 'text': '',
                                            'memory': {'kind': 'reflection', 'key': '理解',
                                                       'note': note}}, ensure_ascii=False))
        self.assertEqual(parsed['memory']['note'], note)
        self.assertEqual(parsed['memory']['kind'], 'reflection')


class DenseRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=here)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        (self.base / 'state').mkdir(parents=True, exist_ok=True)
        self.now = time.time()
        self.encoder = FakeSemanticEncoder()
        self.store = MemoryStore(self.base, encoder=self.encoder)

    def note(self, note, kind='lore', key='矩阵', source='https://example.org/a', **extra):
        return self.store.remember_manual({'kind': kind, 'note': note, 'key': key, 'source': source, **extra},
                                          session='group:1', provider='gemini', channel='test', now=self.now)

    def test_dense_path_finds_no_literal_overlap_paraphrase(self):
        target = self.note('矩阵把某条射线映到自身，只改变长度')
        other = self.note('今晚吃火锅和烧烤', kind='preference', key='晚饭', source='')
        self.assertEqual(self.store.index_pending_embeddings(self.encoder)['errors'], 0)
        hits = self.store.retrieve('group:1', [], {'text': '线性映射中哪些一维子空间保持不变', 'user_id': '101'},
                                   now=self.now + 2, explain=True)
        rendered = ' '.join(hits)
        self.assertIn(target['id'], rendered)
        self.assertIn('命中:dense', rendered)
        self.assertNotIn(other['id'], rendered)
        # Dense candidates are not merely a rerank of lexical hits: this query
        # has no FTS row, yet the semantic record is returned.
        with self.store.db() as db:
            rows = db.execute("SELECT record_id,model,dim,content_hash,start_offset,end_offset FROM embeddings").fetchall()
            self.assertTrue(rows)
            self.assertEqual(rows[0]['model'], self.encoder.model_id)
            self.assertEqual(rows[0]['content_hash'], content_hash('矩阵把某条射线映到自身，只改变长度'))

    def test_unavailable_encoder_falls_back_to_fts_without_raising(self):
        self.store.encoder = UnavailableEncoder()
        result = self.store.remember_manual({'kind': 'commitment', 'note': '声学实验先校准麦克风', 'key': '实验'},
                                            session='group:1', provider='gemini', channel='test', now=self.now)
        hits = self.store.retrieve('group:1', [], {'text': '声学实验', 'user_id': '101'}, now=self.now + 1)
        self.assertTrue(any('声学实验' in line for line in hits))
        self.assertIn(result['id'], ' '.join(hits))

    def test_long_note_is_chunked_but_original_is_unchanged(self):
        note = ('段落一。' + '矩阵和射线。' * 200 + '\n\n' + '段落二。' + '子空间。' * 200)
        result = self.store.remember_manual({'kind': 'commitment', 'note': note, 'key': '长文'},
                                            session='group:1', provider='gemini', channel='test', now=self.now)
        self.assertEqual(self.store.index_pending_embeddings(self.encoder)['errors'], 0)
        with self.store.db() as db:
            count = db.execute("SELECT count(*) FROM embeddings WHERE record_id=?", (result['record_id'],)).fetchone()[0]
            original = db.execute("SELECT text FROM memory_originals WHERE record_id=?", (result['record_id'],)).fetchone()[0]
        self.assertGreater(count, 1)
        self.assertEqual(original, note)
        chunks = chunk_text(note)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(chunks[0][0], 0)
        self.assertEqual(chunks[-1][1], len(note))
        covered = set()
        for start, end, piece in chunks:
            self.assertEqual(note[start:end], piece)
            covered.update(range(start, end))
        self.assertEqual(len(covered), len(note))
        read = self.store.read(result['id'], 'group:1', {'101'}, limit=100)
        self.assertTrue(read['has_more'])

    def test_no_evidence_returns_empty(self):
        self.note('矩阵把某条射线映到自身')
        self.store.index_pending_embeddings(self.encoder)
        self.assertEqual(self.store.retrieve('group:1', [], {'text': '完全不相关的天气问题', 'user_id': '101'},
                                             now=self.now + 1), [])


class GraphV3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=here)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        (self.base / 'state').mkdir(parents=True, exist_ok=True)
        self.now = time.time()
        self.store = MemoryStore(self.base)

    def observe(self, text, user='101', session='group:1', mid='1', at=None, **extra):
        row = {'session_key': session, 'user_id': user, 'message_id': mid,
               'received_at': self.now if at is None else at, 'text': text, **extra}
        self.store.observe(row)
        return row

    def note(self, note, key='约定', kind='commitment', session='group:1', **extra):
        return self.store.remember_manual({'kind': kind, 'note': note, 'key': key, **extra},
                                          session=session, provider='gemini', channel='test', now=self.now)

    def test_cross_group_visible_but_private_isolated(self):
        self.observe('第一群公开讨论', user='101', session='group:1', mid='g1')
        self.observe('第二群公开讨论', user='202', session='group:2', mid='g2')
        self.observe('私聊秘密内容', user='202', session='private:202', mid='p1')
        current = self.store.graph('group:1', {'101'}, cross_groups=False, limit=40)
        cross = self.store.graph('group:1', {'101'}, cross_groups=True, limit=40)
        self.assertIn('group:1', json.dumps(current, ensure_ascii=False))
        self.assertNotIn('group:2', json.dumps(current, ensure_ascii=False))
        self.assertIn('group:2', json.dumps(cross, ensure_ascii=False))
        self.assertNotIn('私聊秘密内容', json.dumps(cross, ensure_ascii=False))
        private_graph = self.store.graph('private:202', {'202'}, cross_groups=True, limit=40)
        self.assertIn('私聊秘密内容', json.dumps(private_graph, ensure_ascii=False))

    def test_same_nickname_is_not_an_identity_and_direct_differs_from_co_present(self):
        self.observe('@哈基米 你好', user='101', mid='1', nickname='同名', direct_targets=['202'],
                     self_id='999', at=self.now - 3)
        self.observe('我也在', user='303', mid='2', nickname='同名', at=self.now - 2)
        self.observe('回复上一句', user='202', mid='3', nickname='同名', reply_to='2', at=self.now - 1)
        graph = self.store.graph('group:1', {'101'}, cross_groups=True, limit=50)
        person_ids = {node['id'] for node in graph['nodes'] if node['type'] == 'person'}
        self.assertIn('person:101', person_ids)
        self.assertIn('person:202', person_ids)
        self.assertIn('person:303', person_ids)
        direct = [edge for edge in graph['edges'] if edge['relation'] == 'direct_interaction']
        co = [edge for edge in graph['edges'] if edge['relation'] == 'co_present']
        self.assertTrue(direct)
        self.assertTrue(co)
        self.assertTrue(all(edge['direct'] for edge in direct))
        self.assertTrue(all(edge['co_present'] for edge in co))
        # Co-presence is not presented as a social relation.
        for edge in co:
            self.assertNotIn(edge['relation'], {'friend', 'lover', 'close', 'hate'})
        self.assertNotIn(('person:101', 'person:303'), {(edge['source'], edge['target']) for edge in direct})

    def test_duplicate_import_and_rebuild_do_not_inflate_weight(self):
        row = self.observe('@哈基米 重复导入', user='101', mid='same', direct_targets=['202'], self_id='999')
        self.store.observe(row)
        self.store.rebuild_graph(limit=100, full=True)
        self.store.rebuild_graph(limit=100, full=True)
        with self.store.db() as db:
            edges = db.execute("SELECT relation,weight FROM graph_edges WHERE relation IN ('mentions','direct_interaction')").fetchall()
            evidence = db.execute("SELECT count(*) FROM graph_edge_evidence e JOIN graph_edges g ON g.edge_id=e.edge_id "
                                  "WHERE g.relation='direct_interaction'").fetchone()[0]
        self.assertEqual([row['weight'] for row in edges], [1, 1])
        self.assertEqual(evidence, 1)

    def test_correction_edges_trace_to_real_evidence(self):
        old = self.note('旧约定：周六上午', key='周末计划')
        new = self.note('新约定：周六晚上', key='周末计划')
        graph = self.store.graph('group:1', {'101'}, cross_groups=True, focus_type='memory',
                                 focus_id=new['id'], depth=2, limit=40)
        supersedes = [edge for edge in graph['edges'] if edge['relation'] == 'supersedes']
        self.assertTrue(supersedes)
        self.assertEqual(supersedes[0]['source'], 'memory:' + str(new['record_id']))
        self.assertEqual(supersedes[0]['target'], 'memory:' + str(old['record_id']))
        self.assertTrue(supersedes[0]['evidence'])
        evidence = supersedes[0]['evidence'][0]
        self.assertEqual(evidence['memory_id'], new['id'])
        self.assertEqual(evidence['excerpt'], '新约定：周六晚上')
        old_read = self.store.read(old['id'], 'group:1', {'101'})
        self.assertEqual(old_read['status'], 'superseded')
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT text FROM memory_originals WHERE record_id=?", (old['record_id'],)).fetchone()[0],
                             '旧约定：周六上午')

    def test_graph_is_bounded_and_reports_omissions(self):
        for index in range(45):
            self.note(f'第{index}条笔记，主题{index % 7}', key=f'事项{index}')
            self.observe(f'第{index}条公开对话', user=str(100 + index), mid=f'm{index}', at=self.now + index)
        graph = self.store.graph('group:1', {'101'}, cross_groups=True, limit=12, depth=2)
        self.assertLessEqual(len(graph['nodes']), 12)
        self.assertLessEqual(len(graph['edges']), 36)
        self.assertGreater(graph['counts']['omitted_nodes'], 0)
        for edge in graph['edges']:
            self.assertLessEqual(len(edge['evidence']), 3)
        self.assertTrue(graph['sampled'])

    def test_graph_can_be_rebuilt_from_existing_records_without_new_ids(self):
        row = self.observe('可重建的公开对话', user='101', mid='rebuild')
        note = self.note('可重建的手动笔记', key='重建')
        with self.store.db() as db:
            before = db.execute("SELECT id FROM records ORDER BY id").fetchall()
            db.execute("DELETE FROM graph_edge_evidence")
            db.execute("DELETE FROM graph_edges")
            db.execute("DELETE FROM graph_aliases")
            db.execute("DELETE FROM graph_nodes")
            db.execute("DELETE FROM memory_meta WHERE key='graph_cursor'")
        self.store.rebuild_graph(limit=100, full=True)
        with self.store.db() as db:
            after = db.execute("SELECT id FROM records ORDER BY id").fetchall()
            self.assertGreater(db.execute("SELECT count(*) FROM graph_nodes").fetchone()[0], 0)
            self.assertGreater(db.execute("SELECT count(*) FROM graph_edges").fetchone()[0], 0)
        self.assertEqual([row[0] for row in before], [row[0] for row in after])
        graph = self.store.graph('group:1', {'101'}, cross_groups=True, limit=40)
        self.assertIn(note['id'], json.dumps(graph, ensure_ascii=False))
        self.assertIn('可重建的公开对话', json.dumps(graph, ensure_ascii=False))

    def test_html_is_self_contained_and_has_working_affordances(self):
        self.observe('@哈基米 看这个', user='101', mid='1', nickname='阿蓝', direct_targets=['202'], self_id='999')
        self.observe('我也在', user='303', mid='2', nickname='同名')
        self.note('一起整理笔记', key='周末')
        graph = self.store.graph('group:1', {'101'}, cross_groups=True, limit=40)
        page = render_html(graph)
        self.assertIn('<!doctype html>', page)
        self.assertNotIn('<script src', page)
        self.assertNotIn('fetch(', page)
        self.assertNotIn('XMLHttpRequest', page)
        self.assertIn('direct_interaction', page)
        self.assertIn('co_present', page)
        self.assertIn('同场出现', page)
        self.assertIn('缩放', page)
        self.assertIn('省略', page)
        self.assertIn('直接互动', page)
        self.assertNotIn('</script><script>', page)


class ToolGraphIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=here)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        (self.base / 'state/img_cache').mkdir(parents=True)
        now = time.time()
        self.payload = {'session_key': 'group:100', 'created_at': now,
                        'recent_context_messages': [{'session_key': 'group:100', 'message_id': '101',
                                                     'user_id': '22', 'text': '记住这个约定', 'nickname': '成员'}]}
        from hakimi.bridge.turn_coordinator import TurnCoordinator
        self.coordinator = TurnCoordinator(self.base)
        self.coordinator.publish(self.payload)
        self.claim = self.coordinator.claim_primary()
        from hakimi.prompt.prompt_context import activate_context
        activate_context(self.claim['payload'], self.base, claim=self.claim)
        from hakimi.tools import qq_cli
        self.qq_cli = qq_cli
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, value in {'BASE_DIR': self.base, 'STATE_DIR': self.base / 'state',
                            'SESSION_MEMORY_FILE': self.base / 'state/session_memory.jsonl',
                            'OUTBOX_FILE': self.base / 'state/outbox.jsonl'}.items():
            stack.enter_context(patch.object(qq_cli, name, value))

    def call(self, name, **data):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.qq_cli.cmd_call(SimpleNamespace(call_name=name, session='group:100',
                                                 turn=self.claim['token'], **data))
        return json.loads(output.getvalue())['result']

    def test_remember_receipt_duplicate_exact_read_and_graph_tool(self):
        note = '  约定原文\n第二行 ✨  '
        first = self.call('memory.remember', kind='commitment', key='周末', note=note, topics='约定,周末')
        self.assertEqual(first['id'], 'M1')
        self.assertFalse(first['duplicate'])
        self.assertEqual(first['hash'], hashlib.sha256(note.encode('utf-8')).hexdigest())
        second = self.call('memory.remember', kind='commitment', key='周末', note=note, topics='约定,周末')
        self.assertTrue(second['duplicate'])
        self.assertEqual(second['id'], first['id'])
        read = self.call('memory.read', memory_id=first['id'], limit=1000)
        self.assertEqual(read['text'], note)
        graph = self.call('memory.graph', memory_id=first['id'], scope='all_groups')
        self.assertEqual(graph['focus'], 'memory:1')
        self.assertTrue(graph['edges'])
        self.assertIn('policy', graph)
        self.assertIn('co_present', graph['legend']['relations'])

    def test_read_pagination_via_tool(self):
        note = '长' * 2400
        saved = self.call('memory.remember', kind='commitment', key='长文', note=note)
        first = self.call('memory.read', memory_id=saved['id'], limit=500)
        self.assertEqual(len(first['text']), 500)
        self.assertEqual(first['next_offset'], 500)
        parts = [first['text']]
        while first['next_offset'] is not None:
            first = self.call('memory.read', memory_id=saved['id'], offset=first['next_offset'], limit=500)
            parts.append(first['text'])
        self.assertEqual(''.join(parts), note)

    def test_graph_all_groups_but_private_scope_denied(self):
        from hakimi.memory.memory_store import store
        index = store(str(self.base))
        index.observe({'session_key': 'group:200', 'user_id': '33', 'message_id': 'g2',
                       'received_at': time.time(), 'text': '他群公开线索'})
        index.observe({'session_key': 'private:44', 'user_id': '44', 'message_id': 'p1',
                       'received_at': time.time(), 'text': '私聊秘密'})
        cross = self.call('memory.graph', person_id='33', scope='all_groups')
        self.assertIn('group:200', json.dumps(cross, ensure_ascii=False))
        private = self.call('memory.graph', memory_id='M2', scope='all_groups')
        if 'error' in private:
            self.assertIn('not visible', private['error'])
        else:
            self.assertNotIn('私聊秘密', json.dumps(private, ensure_ascii=False))


class PromptHabitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=here)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        (self.base / 'state').mkdir(parents=True, exist_ok=True)
        import shutil
        root = here.parent
        shutil.copy2(repo_file('style_guide.md'), self.base / 'style_guide.md')
        shutil.copy2(repo_file('group_profiles.json'), self.base / 'group_profiles.json')
        (self.base / 'config.json').write_text('{}', encoding='utf-8')
        self.now = time.time()
        remember_memory(self.base, 'group:100', {'kind': 'commitment', 'key': '周末',
                         'note': '周六一起整理线性代数笔记'}, {'22': {}}, now=self.now)

    def context(self, text):
        from hakimi.prompt.prompt_context import build_context
        payload = {'session_key': 'group:100', 'recent_context_messages': [
            {'session_key': 'group:100', 'message_id': '1', 'user_id': '22', 'text': text, 'nickname': '成员'}]}
        return build_context(payload, self.base)

    def test_once_only_opportunity_and_related_ids(self):
        context = self.context('记住：周六一起整理线性代数笔记')
        self.assertIn('memory_opportunity', context)
        opportunity = context['memory_opportunity']
        self.assertIn('约定', opportunity['reason'])
        self.assertTrue(opportunity['related_ids'])
        self.assertIn('reply.send', opportunity['guidance'])
        self.assertEqual(self.context('今晚吃什么') .get('memory_opportunity'), None)

    def test_prompt_rules_are_stable_and_do_not_claim_success_without_receipt(self):
        from hakimi.prompt.prompt_context import render_prompt
        prompt = render_prompt({'session_key': 'group:100', 'recent_context_messages': [
            {'session_key': 'group:100', 'message_id': '1', 'user_id': '22', 'text': '记住这个'}]}, self.base)
        for phrase in ('reply.send结束本轮之前', '只有memory.remember工具回执成功', '不每轮机械写',
                       'reflection必须标为主观理解', 'source_excerpt', '不把别群的话当成本群的话'):
            self.assertIn(phrase, prompt)


class ClientFailFastTests(unittest.TestCase):
    def test_absent_service_fails_fast_and_disabled_client_never_dials(self):
        import socket
        from hakimi.memory.memory_embedding_client import EmbeddingClient
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        client = EmbeddingClient(f'http://127.0.0.1:{port}', timeout=0.2)
        started = time.perf_counter()
        self.assertFalse(client.health())
        with self.assertRaises(EmbeddingUnavailable):
            client.embed(['语义查询'], mode='query')
        self.assertLess(time.perf_counter() - started, 1.5)
        disabled = EmbeddingClient(disabled=True)
        self.assertFalse(disabled.health())
        with self.assertRaises(EmbeddingUnavailable):
            disabled.embed(['x'], mode='document')


class EvaluationDatasetTests(unittest.TestCase):
    def test_dataset_has_enough_topics_paraphrases_and_negatives(self):
        self.assertGreaterEqual(len(memory_embedding_eval.DATASET), 12)
        topics = [item['topic'] for item in memory_embedding_eval.DATASET]
        self.assertEqual(len(topics), len(set(topics)))
        for item in memory_embedding_eval.DATASET:
            self.assertGreaterEqual(len(item['paraphrases']), 2)
            self.assertTrue(item['negative'])
        pairs = [(item['anchor'], paraphrase) for item in memory_embedding_eval.DATASET for paraphrase in item['paraphrases']]
        zero_overlap = sum(memory_embedding_eval.lexical_overlap(a, b) == 0 for a, b in pairs)
        self.assertGreaterEqual(zero_overlap, 10)


if __name__ == '__main__':
    unittest.main()
