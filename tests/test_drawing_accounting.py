from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from hakimi.tools.drawing_accounting import BudgetError, finish, reserve, validate_limits


class DrawingAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.limits = {'reserve_usd': '.04', 'daily_usd_limit': '.08', 'monthly_usd_limit': '.20'}

    def row(self, ident):
        with closing(sqlite3.connect(self.base/'state'/'drawing_costs.sqlite3')) as db:
            return db.execute('SELECT status,actual,review FROM drawing_charges WHERE id=?', (ident,)).fetchone()

    def test_incomplete_receipt_holds_reservation_and_actual_releases_headroom(self):
        first = reserve(self.base, self.limits)
        finish(self.base, first, {'usageMetadata': {'totalTokenCount': 12}})
        next_call = reserve(self.base, self.limits)
        with self.assertRaises(BudgetError):
            reserve(self.base, self.limits)
        finish(self.base, next_call, None, not_submitted=True)
        self.assertEqual(self.row(next_call), ('not_submitted', 0, 0))
        new_call = reserve(self.base, self.limits)
        finish(self.base, new_call, {'usage': {'currency':'USD', 'cost':'.03'}})
        self.assertEqual(self.row(new_call), ('settled', 30_000_000, 0))

    def test_exact_overrun_boundary_and_idempotent_settlement(self):
        small = reserve(self.base, {'reserve_usd': '.04'})
        finish(self.base, small, {'billing':{'costUsd':'.14'}})
        finish(self.base, small, {'billing':{'costUsd':'.20'}})
        self.assertEqual(self.row(small), ('settled', 140_000_000, 0))
        larger = reserve(self.base, {'reserve_usd': '.04'})
        finish(self.base, larger, {'billing':{'costUsd':'.15'}})
        self.assertEqual(self.row(larger), ('settled', 150_000_000, 1))

    def test_opt_in_uses_one_atomic_reservation_per_admitted_call(self):
        def attempt(_):
            try:
                return reserve(self.base, dict(self.limits, daily_usd_limit='.04'))
            except BudgetError as error:
                self.assertEqual(error.code, 'drawing_daily_limit')
        with ThreadPoolExecutor(4) as pool:
            ids = [value for value in pool.map(attempt, range(6)) if value]
        self.assertEqual(len(set(ids)), 1)

    def test_unknown_cost_cannot_be_reported_as_zero(self):
        ident = reserve(self.base, {})
        finish(self.base, ident, {'cost': 0, 'usageMetadata': {'cost': 0, 'currency':'EUR'}})
        self.assertEqual(self.row(ident), ('pending', None, 0))
        with self.assertRaises(BudgetError):
            reserve(self.base, {'reserve_usd': '.04', 'monthly_usd_limit': '.06'})
        with self.assertRaises(ValueError):
            validate_limits({'daily_usd_limit': '.25'})

