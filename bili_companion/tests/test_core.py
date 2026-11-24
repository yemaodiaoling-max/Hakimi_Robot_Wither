import concurrent.futures
from dataclasses import replace
from datetime import datetime, timezone
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError
from bili_companion.domain import Scope, Evidence, Candidate
from bili_companion.memory import MemoryStore
from bili_companion.orchestrator import Scheduler
from bili_companion.policy.budget import BudgetLedger, new_york_date
from bili_companion.policy.danmaku import ActionExecutor
from bili_companion.runtime.storage import Database


def timestamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


class CoreTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]/'state'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'test.sqlite3'
        self.db = Database(self.path)

    def test_budget_concurrency_restart_month_and_unknown(self):
        when = timestamp('2026-11-30T23:00:00')
        ledger = BudgetLedger(self.db, monthly='10', daily='10')
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            accepted = list(pool.map(lambda i: ledger.reserve(str(i), '1', when), range(30)))
        self.assertEqual(sum(accepted), 10)
        reopened = BudgetLedger(Database(self.path), monthly='10', daily='10')
        self.assertFalse(reopened.reserve('extra', '.01', when))
        for row in reopened.entries():
            reopened.ambiguous(row['id'])
        december = timestamp('2026-12-01T05:00:00')
        self.assertFalse(reopened.reserve('next', '.01', december))
        for row in reopened.entries():
            reopened.settle(row['id'], '.2')
        self.assertTrue(reopened.reserve('next', '1', december))
        self.assertFalse(reopened.reserve('next', '1', december))
        reopened.settle('next', '11')  # Real overrun is recorded, not discarded.
        self.assertFalse(reopened.reserve('blocked', '.01', december))
        self.assertEqual(reopened.entries()[-1]['actual'], 11_000_000)

    def test_timezone_without_windows_tzdata(self):
        with patch('bili_companion.policy.budget.ZoneInfo', side_effect=ZoneInfoNotFoundError):
            self.assertEqual(new_york_date(timestamp('2026-12-01T04:59:59')).month, 11)
            self.assertEqual(new_york_date(timestamp('2026-12-01T05:00:00')).month, 12)
            self.assertEqual(new_york_date(timestamp('2026-07-01T04:00:00')).hour, 0)

    def test_daily_occurrence_and_reply_cap_preserve_monthly_liability(self):
        ledger = BudgetLedger(self.db, monthly='2', daily='1')
        old = timestamp('2026-11-01T12:00:00')
        now = timestamp('2026-11-02T12:00:00')
        self.assertTrue(ledger.reserve('old', '1', old))
        ledger.ambiguous('old')
        ledger.reply_daily = 150000
        self.assertTrue(ledger.reserve('bili-model-comment-reply-first', '.1', now))
        self.assertFalse(ledger.reserve('bili-model-comment-reply-second', '.06', now))
        self.assertTrue(ledger.reserve('today', '.9', now))
        self.assertFalse(ledger.reserve('monthly-full', '.01', timestamp('2026-11-03T12:00:00')))

    def test_legacy_freeze_is_audit_only_after_restart(self):
        ledger = BudgetLedger(self.db)
        now = timestamp('2026-11-01T12:00:00')
        with self.db.connect(write=True) as db:
            db.execute("INSERT INTO budget_freeze VALUES ('legacy',0)")
        ledger = BudgetLedger(Database(self.path))
        self.assertTrue(ledger.reserve('before-ack', '.01', now))
        ledger.acknowledge_overrun('legacy')
        self.assertTrue(ledger.reserve('after-ack', '.01', now))

    def test_minor_video_overrun_records_actual_and_allows_next_day_live(self):
        ledger = BudgetLedger(self.db)
        yesterday = timestamp('2026-11-10T12:00:00')
        self.assertTrue(ledger.reserve('bili-model-video-observer', '.018524', yesterday))
        ledger.settle('bili-model-video-observer', '.018901')
        ledger.settle('bili-model-video-observer', '.018901')
        ledger = BudgetLedger(Database(self.path))
        self.assertEqual(ledger.entries()[0]['actual'], 18901)
        self.assertEqual(ledger.entries()[0]['state'], 'settled')
        self.assertTrue(ledger.reserve('bili-model-live-observer', '.02',
                                       timestamp('2026-11-11T12:00:00')))
        with self.db.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM budget_freeze').fetchone()[0], 0)

    def test_overrun_audit_threshold_never_freezes_future_calls(self):
        now = timestamp('2026-11-01T12:00:00')
        for index, (actual, audit) in enumerate([
                (109999, False), (110000, False), (110001, True),
                (510000, True)]):
            with self.subTest(actual=actual):
                database = Database(Path(self.temp.name)/f'threshold-{index}.sqlite3')
                ledger = BudgetLedger(database)
                self.assertTrue(ledger.reserve('call', '.01', now))
                ledger.settle('call', str(actual/1_000_000))
                ledger.settle('call', str(actual/1_000_000))
                ledger = BudgetLedger(database)
                self.assertEqual(ledger.entries()[0]['actual'], actual)
                self.assertTrue(ledger.reserve('next', '.001', now))
                with database.connect() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM budget_freeze').fetchone()[0], int(audit))
                    if audit:
                        self.assertEqual(db.execute('SELECT acknowledged FROM budget_freeze').fetchone()[0], 0)

    def test_minor_overrun_does_not_relax_hard_limits_or_unknown_liability(self):
        now = timestamp('2026-11-01T12:00:00')
        tomorrow = timestamp('2026-11-02T12:00:00')
        ledger = BudgetLedger(self.db, monthly='.03', daily='.02')
        self.assertTrue(ledger.reserve('video', '.018524', now))
        ledger.settle('video', '.018901')
        self.assertFalse(ledger.reserve('daily-over', '.0011', now))
        self.assertTrue(ledger.reserve('daily-exact', '.001099', now))
        ledger.ambiguous('daily-exact')
        self.assertFalse(ledger.reserve('monthly-over', '.010001', tomorrow))
        self.assertTrue(ledger.reserve('monthly-exact', '.01', tomorrow))
        ledger.ambiguous('monthly-exact')
        self.assertFalse(ledger.reserve('next-month', '.02', timestamp('2026-12-01T12:00:00')))

    def test_preempt_restart_out_of_order_disconnect(self):
        s = Scheduler(self.db, 'room')
        s.apply(dict(id='v', type='video', bvid='BV_demo', cid='1', position=42, watch_epoch=0))
        def status(i, value, seq, session='live1'):
            return s.apply(dict(id=i, type='live_status', room_id='room', session_id=session,
                                status=value, sequence=seq, at=seq*60))
        live = status('on', 'online', 1)
        self.assertEqual(live['bookmark']['position'], 42)
        epoch = live['watch_epoch']
        s.apply(dict(id='lost', type='stream_lost', session_id='live1', watch_epoch=epoch, sequence=1))
        s = Scheduler(Database(self.path), 'room')
        self.assertEqual(s.snapshot()['state'], 'RECOVERING')
        s.apply(dict(id='restored', type='stream_restored', session_id='live1', watch_epoch=epoch, sequence=3))
        s.apply(dict(id='late-lost', type='stream_lost', session_id='live1', watch_epoch=epoch, sequence=2))
        self.assertEqual(s.snapshot()['state'], 'WATCHING_PRIORITY_LIVE')
        s.apply(dict(id='stale-offline', type='live_status', room_id='room', status='offline',
                     sequence=2, at=120, received_at=500))
        self.assertEqual(s.snapshot()['offline_count'], 0)
        status('off1', 'offline', 3)
        status('old', 'offline', 2)
        self.assertEqual(s.snapshot()['offline_count'], 1)
        status('err', 'unknown', 4)
        status('off2', 'offline', 5)
        self.assertEqual(s.snapshot()['live_session'], 'live1')
        final = status('off3', 'offline', 6)
        self.assertEqual(final['video']['position'], 42)
        s.apply(dict(id='stale', type='stream_lost', session_id='live1', watch_epoch=epoch, sequence=2))
        self.assertEqual(s.snapshot()['state'], 'WATCHING_VIDEO')
        s.apply(dict(id='late-video', type='video', bvid='wrong', cid='1', watch_epoch=0))
        self.assertEqual(s.snapshot()['video']['bvid'], 'BV_demo')

    def test_memory_scope_and_revision_evidence(self):
        memory = MemoryStore(self.db)
        scope = Scope('a', 'live_room', 'one')
        evidence = Evidence('e', scope, 'live', '蓝色方块到达终点', 100, 0, 1, 'video', 'observation')
        memory.append(evidence)
        self.assertFalse(memory.append(evidence))
        with self.assertRaises(ValueError):
            memory.append(replace(evidence, text='changed'))
        for other in (Scope('a', 'live_room', 'two'), Scope('b', 'live_room', 'one'), Scope('a', 'live_room', 'one', 'qq')):
            self.assertEqual(memory.recall(other, '蓝色方块'), [])
            self.assertIsNone(memory.get('e', other, include_public=True))
            with self.assertRaises(ValueError):
                memory.revise(other, 'note', ['e'])
        note = memory.revise(scope, '方块终点总结', ['e'])
        with self.assertRaises(ValueError):
            memory.revise(scope, 'stale', ['e'], note_id=note)
        memory.delete(scope, 'e')
        self.assertEqual(memory.recall(scope, '终点'), [])

    def test_danmaku_stale_duplicate_restart_and_epoch(self):
        memory = MemoryStore(self.db)
        scope = Scope('a', 'live_room', 'room')
        memory.append(Evidence('e', scope, 'live', '方块到达终点', 100, 0, 1, 'video', 'observation'))
        config = dict(allowed_rooms=['room'], cooldown_seconds=90, hourly_danmaku_limit=12,
                      evidence_max_age_seconds=60, dry_run=True)
        executor = ActionExecutor(self.db, memory, config)
        current = dict(state='WATCHING_PRIORITY_LIVE', watch_epoch=2, live_session='live', room_id='room')
        candidate = Candidate('room', 'live', '到终点啦', ('e',), 150, 'key', 2)
        with self.assertRaises(ValueError):
            replace(candidate, expires_at=float('nan'))
        def check(c=candidate, now=110, epoch=2):
            return executor.preview(c, current, account_id='a', watch_epoch=epoch, now=now)
        self.assertEqual(check(epoch=1), 'inactive_session')
        self.assertEqual(check(replace(candidate, watch_epoch=1)), 'inactive_session')
        self.assertEqual(check(now=151), 'expired')
        self.assertEqual(check(replace(candidate, expires_at=200), now=170), 'stale_evidence')
        self.assertEqual(check(), 'dry_run')
        executor = ActionExecutor(Database(self.path), memory, config)
        self.assertEqual(check(), 'duplicate')
        self.assertEqual(check(replace(candidate, text='新候选', dedup_key='new')), 'cooldown')
        self.assertEqual(check(replace(candidate, room_id='other')), 'room_not_allowed')


if __name__ == '__main__':
    unittest.main()
