import json
import tempfile
import unittest
from pathlib import Path

from bili_companion.domain import Evidence, Scope
from bili_companion.memory import MemoryStore
from bili_companion.memory.sources import linked_refs, media_refs
from bili_companion.runtime.storage import Database
from bili_companion.sync import SyncStore, seal_packet
from bili_companion.integrations.service import write_snapshot
from bili_companion.integrations.qq_context import context_for


class MemorySourcesTests(unittest.TestCase):
    def setUp(self):
        (Path(__file__).resolve().parents[1]/'state'/'test_tmp').mkdir(parents=True,exist_ok=True)

    def test_only_native_known_identity_becomes_a_public_link(self):
        self.assertEqual(media_refs(Scope('a','video','unknown')), ['bilibili:video:unknown'])
        self.assertFalse(any('https:' in r for r in linked_refs(['bilibili:video:unknown:session:s'])))
        self.assertIn('https://www.bilibili.com/video/BV1xx411c7mD',
                      media_refs(Scope('a','video','BV1xx411c7mD')))
        self.assertEqual(media_refs(Scope('a','private','BV1xx411c7mD')), [])

    def test_historical_ref_survives_qq_budget_and_is_searchable(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'state'/'test_tmp') as directory:
            root=Path(directory)
            now=1800000000
            store=SyncStore(Database(root/'bridge.db'),endpoint='qq',local_id='q',peer_id='b',clock=lambda:now)
            identity=dict(source_id='old',revision=1,scope='self')
            fact=dict(**identity,text='听见钢琴旋律。'*150,
                source_refs=['bilibili:video:BV1xx411c7mD:session:old'],
                coverage=[dict(start=1,end=5,modality='audio')],attribution='observation',
                created_at=now-1,importance=0,truncated=False)
            store.receive(seal_packet(dict(version=1,packet_id='p',origin='bilibili',recipient='qq',
                origin_id='b',recipient_id='q',kind='watch_digest',created_at=now,expires_at=now+100,
                source_ids=[identity],facts=[fact])))
            write_snapshot(store,root/'state/bili_bridge_context.json')
            result=context_for(root,'BV1xx411c7mD',now)['bridge_memory_data']
            self.assertIn('来源：BV1xx411c7mD',result[0]['summary'])
            self.assertIn('听见',result[0]['summary'])
            self.assertIn('https://www.bilibili.com/video/BV1xx411c7mD',result[0]['source_refs'])
            self.assertEqual(result[0]['revision'],1)
            self.assertLessEqual(len(json.dumps(result,ensure_ascii=False,separators=(',',':'))),1000)
            # The deployed helper searches summary alone. Verify that compatibility
            # path too, without requiring a QQ process restart.
            import inspect
            import bili_companion.integrations.qq_context as helper
            old_source=inspect.getsource(helper).replace(
                "row['summary'] + ' ' + ' '.join(row['source_refs'])", "row['summary']")
            namespace={}
            exec(compile(old_source,'historical_qq_helper','exec'),namespace)
            old_result=namespace['context_for'](root,'BV1xx411c7mD',now)['bridge_memory_data']
            self.assertIn('BV1xx411c7mD',old_result[0]['summary'])
            self.assertIn('https://www.bilibili.com/video/BV1xx411c7mD',old_result[0]['source_refs'])

    def test_native_recall_includes_scope_source_without_other_scope(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'state'/'test_tmp') as directory:
            memory=MemoryStore(Database(Path(directory)/'native.db'))
            scope=Scope('a','video','BV1xx411c7mD')
            memory.append(Evidence('e',scope,'s','钢琴',1,0,3,'audio','observation'))
            memory.revise(scope,'钢琴回忆',['e'])
            records=memory.recall(scope,'钢琴')
            self.assertEqual(len(records),2)
            self.assertTrue(all('https://www.bilibili.com/video/BV1xx411c7mD' in r['source_refs'] for r in records))
            self.assertEqual(memory.recall(Scope('other','video',scope.identity),'钢琴'),[])

