"""Sampling uses the exact production QQ allowlist."""
import random
import unittest
import sqlite3
from contextlib import closing

import test_qq_integration as fixtures
from bili_companion.integrations.qq_interest import QQInterestSampler, imported_records
from bili_companion.integrations.service import LocalTransport, tick
import json
from unittest.mock import patch
from bili_companion.sync import SyncWorker


class InterestSamplingTests(unittest.TestCase):
    setUp = fixtures.QQIntegrationTests.setUp
    tearDown = fixtures.QQIntegrationTests.tearDown
    row = fixtures.QQIntegrationTests.row
    def worker(self):
        self.source = QQInterestSampler(self.native, self.qq, rng=random.Random(7))
        return SyncWorker(self.qq, self.source, LocalTransport(self.bili), interval_seconds=7200)

    def test_bounded_sampling_no_false_deletion_and_no_repeat(self):
        for i in range(1, 10):
            self.row(i, text='科学新发现' + chr(0x4e10+i)*240)
        worker = self.worker()
        worker.run_once(force=True)
        first = self.bili.snapshot_records()
        self.assertLessEqual(len(first), 6)
        self.assertLessEqual(sum(len(r['summary']) for r in first), 1000)
        self.assertTrue(all(len(r['summary']) <= 200 for r in first))
        for _ in range(12):
            worker.run_once(force=True)
        all_records = self.bili.snapshot_records()
        self.assertEqual(len(all_records), 9)
        self.assertTrue({r['source_id'] for r in first} <= {r['source_id'] for r in all_records})
        with self.qq.database.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sync_tombstones').fetchone()[0], 0)
        self.assertEqual(self.source.last_state, 'no_unread')
        self.assertTrue(all(r['read_at'] == self.now for r in imported_records(self.bili)))

    def test_deleted_and_expired_sources_retract_but_failure_keeps_history(self):
        self.row(1, expires=self.now+10)
        worker = self.worker()
        worker.run_once(force=True)
        self.assertEqual(len(self.bili.snapshot_records()), 1)
        original = self.source.path
        self.source.path = self.root/'missing.sqlite3'
        self.assertEqual(worker.run_once(force=True)['state'], 'retry')
        self.assertEqual(len(self.bili.snapshot_records()), 1)
        self.source.path = original
        self.now += 11
        worker.run_once(force=True)
        self.assertEqual(self.bili.snapshot_records(), [])
        with closing(sqlite3.connect(self.native)) as db, db:
            db.execute('UPDATE records SET expires=0,text=? WHERE id=1', ('重新公开的科学发现',))
        worker.run_once(force=True)
        self.assertEqual(self.bili.snapshot_records()[0]['revision'], 2)

    def test_no_keyword_candidates_and_fake_compatibility(self):
        self.row(1, text='12345 !!!')
        self.worker().run_once(force=True)
        self.assertEqual(self.source.last_state, 'no_keyword_candidates')
        self.assertEqual(self.bili.snapshot_records(), [])
        self.assertEqual(imported_records(object()), [])

    def test_seven_day_selected_renewal_is_bounded_and_restores_expired_inbox(self):
        for i in range(1, 10):
            self.row(i, text='科学新发现' + chr(0x4e10+i)*240)
        worker = self.worker()
        for _ in range(12):
            worker.run_once(force=True)
        self.assertEqual(len(self.bili.snapshot_records()), 9)
        self.assertTrue(all(r['truncated'] for r in self.bili.snapshot_records()))
        self.now += 7*86400+1
        self.bili.cleanup()
        self.assertEqual(self.bili.snapshot_records(), [])
        self.assertEqual(worker.run_once(force=True)['state'], 'synced')
        restored = self.bili.snapshot_records()
        self.assertGreater(len(restored), 0)
        self.assertLessEqual(len(restored), 6)
        self.assertLessEqual(sum(len(r['summary']) for r in restored), 1000)
        self.assertTrue(all(r['revision'] == 1 for r in restored))

    def test_failed_delivery_retries_same_bounded_packet_not_new_sample(self):
        for i in range(1, 10):
            self.row(i, text='科学新发现' + chr(0x4e10+i)*240)
        worker = self.worker()
        class BrokenTransport:
            def send(self, packet):
                raise OSError('private error must never be printed')
        worker.transport = BrokenTransport()
        self.assertEqual(worker.run_once(force=True)['state'], 'retry')
        first = self.qq.pending()
        self.assertLessEqual(sum(len(p['facts']) for p in first), 6)
        self.assertLessEqual(sum(len(f['text']) for p in first for f in p['facts']), 1000)
        worker.transport = LocalTransport(self.bili, self.qq)
        self.assertEqual(worker.run_once(force=True)['state'], 'synced')
        self.assertEqual({p['packet_id'] for p in first}, {r['packet_id'] for r in self.bili.snapshot_records()})
        self.assertLessEqual(len(self.bili.snapshot_records()), 6)

    def test_oversized_audit_fails_before_any_retraction_or_state_change(self):
        self.row(1)
        worker = self.worker()
        worker.run_once(force=True)
        before = self.bili.snapshot_records()
        baseline = self.qq.status()['checkpoint']
        record = dict(next(self.source.rows()))
        def oversized():
            for i in range(1001):
                yield {**record, 'source_id': 'qq:capacity'+str(i)}
        self.source.rows = oversized
        self.assertEqual(worker.run_once(force=True)['state'], 'retry')
        self.assertEqual(self.qq.status()['checkpoint'], baseline)
        self.assertEqual(self.bili.snapshot_records(), before)
        self.assertEqual(self.qq.pending(), [])

    def test_service_migrates_six_hour_due_and_keeps_half_hour_tick_quiet(self):
        from bili_companion.integrations.service import tick
        from bili_companion.runtime.storage import Database
        from bili_companion.sync import SyncStore
        cfg = dict(state_dir=str(self.root/'service_state'), qq_identity='q', bilibili_identity='b',
            qq_database=str(self.native), qq_snapshot=str(self.root/'snapshot.json'))
        self.row(1)
        result = tick(cfg, clock=lambda: self.now, rng=random.Random(7))
        self.assertEqual(result['qq']['state'], 'synced')
        self.assertNotIn('keyword', result['qq'])
        qq = SyncStore(Database(self.root/'service_state/qq_bridge.sqlite3'), endpoint='qq',
            local_id='q', peer_id='b', clock=lambda:self.now)
        with qq.database.connect(write=True) as db:
            db.execute('UPDATE sync_state SET next_due=last_sync+21600 WHERE channel=?', (qq.channel,))
        self.now += 1800
        self.assertEqual(tick(cfg, clock=lambda:self.now)['qq']['state'], 'not_due')
        self.now += 5400
        self.assertIn(tick(cfg, clock=lambda:self.now)['qq']['state'], {'empty','synced'})
        self.assertEqual(qq.status()['next_due'], self.now+7200)


    def test_tick_audits_real_passes_not_timer_or_duplicate_receipts(self):
        self.row(1,text='科学共享摘记')
        config=dict(state_dir=str(self.root/'bridge'),qq_identity='q',bilibili_identity='b',
                    qq_database=str(self.native),qq_snapshot=str(self.root/'snapshot.json'))
        tick(config,clock=lambda:self.now,rng=random.Random(7))
        path=self.root/'bridge'/'bilibili_bridge.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            first=db.execute('SELECT read_at,keyword,status,records_json FROM qq_memory_reads').fetchall()
        self.assertEqual(len(first),1)
        self.assertEqual(first[0][2],'first_import')
        self.assertEqual(json.loads(first[0][3])[0]['summary'],'科学共享摘记')
        tick(config,clock=lambda:self.now,rng=random.Random(7))
        with closing(sqlite3.connect(path)) as db, db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM qq_memory_reads').fetchone()[0],1)
        tick(config,force=True,clock=lambda:self.now,rng=random.Random(7))
        with closing(sqlite3.connect(path)) as db, db:
            rows=db.execute('SELECT status,records_json FROM qq_memory_reads ORDER BY id').fetchall()
        self.assertEqual(rows[-1],('no_new_content','[]'))


    def test_failed_packet_retry_preserves_keyword_and_bounded_receipts(self):
        for i in range(1,10):
            self.row(i,text='科学共享摘要' + chr(0x4e10+i)*240)
        config=dict(state_dir=str(self.root/'bridge'),qq_identity='q',bilibili_identity='b',
                    qq_database=str(self.native),qq_snapshot=str(self.root/'snapshot.json'))
        with patch.object(LocalTransport,'send',side_effect=OSError('offline')):
            result=tick(config,force=True,clock=lambda:self.now,rng=random.Random(7))
        self.assertEqual(result['qq']['state'],'retry')
        tick(config,force=True,clock=lambda:self.now,rng=random.Random(7))
        with closing(sqlite3.connect(self.root/'bridge'/'bilibili_bridge.sqlite3')) as db:
            rows=db.execute('SELECT keyword,status,records_json FROM qq_memory_reads ORDER BY id').fetchall()
        self.assertEqual(rows[0][1],'delivery_pending')
        self.assertEqual(rows[1][1],'first_import')
        self.assertTrue(rows[1][0])
        self.assertEqual(rows[1][0],rows[0][0])
        records=json.loads(rows[1][2])
        self.assertTrue(records)
        self.assertLessEqual(len(records),6)
        self.assertLessEqual(sum(len(x['summary']) for x in records),1000)
