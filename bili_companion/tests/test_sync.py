from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from bili_companion.runtime.storage import Database
from bili_companion.sync import SourceItem, SourceBatch, SyncStore, SyncWorker, seal_packet, validate_packet
from bili_companion.sync.compress import compress, compress_batches
from bili_companion.sync.types import canonical


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.

    def __call__(self):
        return self.now


class Transport:
    def __init__(self, peer):
        self.peer, self.calls, self.fail_after_receive = peer, 0, False

    def send(self, packet):
        self.calls += 1
        self.peer.receive(packet)
        if self.fail_after_receive:
            self.fail_after_receive = False
            raise TimeoutError('test: acknowledgement lost')
        return True


class SyncTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]/'state'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.db = Database(Path(self.temp.name)/'sync.sqlite3')
        self.qq = SyncStore(self.db, endpoint='qq', local_id='hakimi-qq', peer_id='hakimi-bili', clock=self.clock)
        self.bili = SyncStore(self.db, endpoint='bilibili', local_id='hakimi-bili', peer_id='hakimi-qq', clock=self.clock)
        self.item = SourceItem('native-1', 1, 'self', 'preference', '合成测试偏好：喜欢简短表达。', self.clock()-300,
                               source_refs=('qq:public-note:1',))

    def packet(self, item=None):
        facts = compress([item or self.item], endpoint='qq', now=self.clock())
        checkpoint = self.qq.status()['checkpoint']
        self.qq.stage(facts, expected_checkpoint=checkpoint, checkpoint=checkpoint+'1')
        return self.qq.pending()[-1]

    def test_schema_integrity_limits_and_observation_requirements(self):
        packet = self.packet()
        self.assertIs(validate_packet(packet, now=self.clock()), packet)
        tampered = json.loads(canonical(packet))
        tampered['facts'][0]['text'] = 'changed'
        with self.assertRaises(ValueError):
            validate_packet(tampered)
        for changes in ({'version':True}, {'expires_at':float('nan')}, {'extra':'no'},
                        {'facts':'wrong'}, {'origin':'bilibili'}, {'created_at':float('inf')}):
            bad = {**packet, **changes}
            with self.assertRaises((ValueError, TypeError)):
                validate_packet(bad)
        bad = json.loads(canonical(packet))
        bad['facts'][0]['text'] = 'Cookie=private-value'
        with self.assertRaises(ValueError):
            validate_packet(seal_packet(bad))
        bad = json.loads(canonical(packet))
        bad['source_ids'][0]['scope'] = bad['facts'][0]['scope'] = 'private:someone'
        with self.assertRaises(ValueError):
            validate_packet(seal_packet(bad))
        self.assertEqual(compress([self.item], endpoint='bilibili', now=self.clock()), [])
        watch = replace(self.item, kind='watch_note', source_refs=('bilibili:video:BV_SYNTHETIC:1',),
                        coverage=({'start':0, 'end':30, 'modality':'audio'},))
        self.assertEqual(len(compress([watch], endpoint='bilibili', now=self.clock())), 1)

    def test_lossy_longterm_batches_latest_revision_and_default_exclusions(self):
        old = replace(self.item, created_at=self.clock()-400*86400, text='旧长期公开偏好'*100)
        facts = compress([old], endpoint='qq', now=self.clock())
        self.assertEqual(facts[0]['created_at'], old.created_at)
        self.assertEqual(facts[0]['text'], old.text[:360])
        self.assertTrue(facts[0]['truncated'])
        self.assertEqual(compress([replace(old, kind='episode')], endpoint='qq', now=self.clock()), [])
        exclusions = [replace(self.item, kind='person'), replace(self.item, kind='chat'),
                      replace(self.item, private=True), replace(self.item, imported=True),
                      replace(self.item, scope='private:1'), replace(self.item, text='api_key=secret')]
        self.assertEqual(compress(exclusions, endpoint='qq', now=self.clock()), [])
        newer_revision = replace(self.item, revision=2, created_at=self.item.created_at-100, text='新修订仍保留旧发生时间')
        self.assertEqual(compress([self.item, newer_revision], endpoint='qq', now=self.clock())[0]['revision'], 2)
        items = tuple(replace(self.item, source_id=str(i), text=f'合成公开事实{i}') for i in range(23))
        batches = compress_batches(items, endpoint='qq', now=self.clock())
        self.assertEqual(sum(len(batch) for batch in batches), 23)
        transport = Transport(self.bili)
        worker = SyncWorker(self.qq, lambda _:SourceBatch(items, 'all-23'), transport)
        self.assertEqual(worker.run_once()['state'], 'synced')
        with self.db.connect() as db:
            count = db.execute('SELECT COUNT(*) FROM sync_facts WHERE channel=?', (self.bili.channel,)).fetchone()[0]
        self.assertEqual(count, 23)

    def test_idempotent_restart_ack_loss_and_source_revision_conflicts(self):
        transport = Transport(self.bili)
        transport.fail_after_receive = True
        source_calls = []
        def source(checkpoint):
            source_calls.append(checkpoint)
            return SourceBatch((self.item,), 'scan-1')
        worker = SyncWorker(self.qq, source, transport)
        self.assertEqual(worker.run_once()['state'], 'retry')
        self.assertEqual(self.qq.status()['checkpoint'], 'scan-1')
        self.assertEqual(len(self.bili.recall()), 1)
        self.clock.now += 5
        reopened = SyncStore(self.db, endpoint='qq', local_id='hakimi-qq', peer_id='hakimi-bili', clock=self.clock)
        worker = SyncWorker(reopened, source, transport)
        self.assertEqual(worker.run_once()['sent'], 1)
        self.assertEqual(source_calls, [''])
        self.assertEqual(len(self.bili.recall()), 1)
        packet = self.packet(replace(self.item, source_id='another'))
        self.bili.receive(packet)
        bad = json.loads(canonical(packet))
        bad['packet_id'] = 'new-id'
        bad['facts'][0]['text'] = 'same source revision altered'
        with self.assertRaises(ValueError):
            self.bili.receive(seal_packet(bad))

    def test_due_empty_skip_refresh_expiry_delete_and_old_packet_replay(self):
        transport = Transport(self.bili)
        worker = SyncWorker(self.qq, lambda _:SourceBatch((self.item,), 'stable'), transport)
        self.assertEqual(worker.run_once()['sent'], 1)
        old_packet = self.qq.pending()
        self.assertEqual(worker.run_once()['state'], 'not_due')
        self.clock.now += 21600
        self.assertEqual(worker.run_once()['sent'], 0)
        self.assertEqual(transport.calls, 1)
        self.clock.now += 6*86400
        self.assertEqual(worker.run_once()['sent'], 1)  # Same revision low-frequency lease refresh.
        self.assertEqual(len(self.bili.recall()), 1)
        self.qq.retract('self', self.item.source_id, 1)
        tombstone = next(p for p in self.qq.pending() if p['kind']=='retraction')
        self.bili.receive(tombstone)
        self.assertEqual(self.bili.recall(), [])
        # A fresh wrapper around an old source revision cannot resurrect a tombstone.
        replay = self.packet(replace(self.item, source_id='temporary'))
        replay['source_ids'][0]['source_id'] = self.item.source_id
        replay['facts'][0]['source_id'] = self.item.source_id
        self.bili.receive(seal_packet(replay))
        self.assertEqual(self.bili.recall(), [])
        self.clock.now += 8*86400
        self.bili.cleanup()
        self.assertEqual(self.bili.recall(), [])
        with self.assertRaises(ValueError):
            self.bili.receive(tombstone)

    def test_isolation_loop_prevention_and_total_bridge_budget(self):
        packet = self.packet(replace(self.item, text='合成摘要原句'*500))
        self.bili.receive(packet)
        records = self.bili.recall(budget=700)
        self.assertTrue(records)
        self.assertLessEqual(len(canonical(records)), 700)
        self.assertTrue(records[0]['is_digest'])
        self.assertEqual(records[0]['source_refs'], ['qq:public-note:1'])
        self.assertEqual(self.bili.recall(scope='private:someone'), [])
        other = SyncStore(self.db, endpoint='bilibili', local_id='another-account', peer_id='hakimi-qq', clock=self.clock)
        self.assertEqual(other.recall(), [])
        with self.assertRaises(ValueError):
            other.receive(packet)
        changed_origin = {**packet, 'origin_id':'attacker'}
        with self.assertRaises(ValueError):
            self.bili.receive(seal_packet(changed_origin))
        imported = replace(self.item, imported=True, source_refs=('qq:public-note:1',))
        self.assertEqual(compress([imported], endpoint='bilibili', now=self.clock()), [])

    def test_transaction_rollback_and_fenced_worker_after_interruption(self):
        facts = compress([self.item], endpoint='qq', now=self.clock())
        original = self.qq._stage
        def interrupted(db, facts, sources, kind):
            original(db, facts, sources, kind)
            raise RuntimeError('simulated crash before checkpoint commit')
        with patch.object(self.qq, '_stage', side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.qq.stage(facts, expected_checkpoint='', checkpoint='must-not-commit')
        self.assertEqual(self.qq.status()['checkpoint'], '')
        self.assertEqual(self.qq.pending(), [])
        lease_a = self.qq.claim()
        self.clock.now += 121
        lease_b = self.qq.claim()
        self.assertNotEqual(lease_a, lease_b)
        for operation in (
            lambda:self.qq.stage(facts, expected_checkpoint='', checkpoint='stale', lease_token=lease_a),
            lambda:self.qq.finish(success=True, interval_seconds=30, lease_token=lease_a),
            lambda:self.qq.renew(lease_token=lease_a)):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(self.qq.status()['lease_token'], lease_b)

    def test_highest_revision_does_not_regress_after_expiry(self):
        packet = self.packet(replace(self.item, revision=2))
        self.bili.receive(packet)
        self.clock.now += 8*86400
        self.bili.cleanup()
        packet['packet_id'] = 'late-old-source'
        packet['created_at'] = self.clock()
        packet['expires_at'] = self.clock()+86400
        packet['source_ids'][0]['revision'] = packet['facts'][0]['revision'] = 1
        self.bili.receive(seal_packet(packet))
        self.assertEqual(self.bili.recall(), [])

    def test_long_excerpt_is_stable_when_snapshot_order_changes(self):
        items = tuple(replace(self.item, source_id=f'long-{i}', text=f'合成摘录{i}'+('文本'*250),
                              created_at=self.clock()-i-100) for i in range(7))
        batches = compress_batches(items, endpoint='qq', now=self.clock())
        self.assertEqual([len(f['text']) for b in batches for f in b], [360]*7)
        self.qq.stage_batches(batches, expected_checkpoint='', checkpoint='first')
        expanded = (replace(self.item, source_id='new', created_at=self.clock()-1,
                            text='新合成资料'+('新文本'*150)), *items)
        second = compress_batches(expanded, endpoint='qq', now=self.clock())
        self.qq.stage_batches(second, expected_checkpoint='first', checkpoint='second')
        with self.db.connect() as db:
            count = db.execute('SELECT COUNT(*) FROM sync_active_export WHERE channel=?', (self.qq.channel,)).fetchone()[0]
        self.assertEqual(count, 8)


if __name__ == '__main__':
    unittest.main()
