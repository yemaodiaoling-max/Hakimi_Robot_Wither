from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from hakimi.tools import drawing_accounting as costs

NY = ZoneInfo('America/New_York')


class DrawingQuotaTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.first = datetime(2026, 11, 23, 10, 7, 15, tzinfo=NY).timestamp()

    def at(self, when):
        return patch.object(costs.time, 'time', return_value=when)

    def test_window_begins_on_first_admitted_request_and_survives_reopening(self):
        with self.at(self.first - 600):
            self.assertIsNone(costs.quota_status(self.base)['window_reset_at'])
        with self.at(self.first):
            costs.reserve(self.base, {})
        with self.at(self.first + 1199):
            costs.reserve(self.base, {})
            for _ in range(3):
                with self.assertRaises(costs.BudgetError) as blocked:
                    costs.reserve(self.base, {})
                self.assertEqual(blocked.exception.code, 'drawing_window_images')
            self.assertEqual(costs.quota_status(self.base)['available_at'], self.first + 1200)
        with self.at(self.first + 1200):
            quota = costs.quota_status(self.base)
            self.assertEqual((quota['window_remaining'],quota['daily_remaining']), (2,18))
            self.assertIsNone(quota['window_reset_at'])
        with self.at(self.first + 1800):
            costs.reserve(self.base, {})
            self.assertEqual(costs.quota_status(self.base)['window_reset_at'], self.first + 3000)

    def test_twenty_a_day_and_a_window_spanning_midnight(self):
        today = datetime(2026,11,23,0,0,tzinfo=NY)
        midnight = (today+timedelta(days=1)).timestamp()
        for window in range(9):
            with self.at(today.timestamp()+window*1200):
                for _ in range(2):
                    costs.reserve(self.base,{})
        with self.at(midnight - 60):
            for _ in range(2):
                costs.reserve(self.base,{})
            with self.assertRaises(costs.BudgetError) as blocked:
                costs.reserve(self.base,{})
            self.assertEqual(blocked.exception.code, 'drawing_day_images')
            self.assertEqual(costs.quota_status(self.base)['available_at'],midnight+1140)
        with self.at(midnight):
            quota=costs.quota_status(self.base)
            self.assertEqual((quota['daily_remaining'],quota['window_remaining']), (20,0))
            with self.assertRaises(costs.BudgetError):
                costs.reserve(self.base,{})
        with self.at(midnight+1140):
            costs.reserve(self.base,{})
            self.assertEqual(costs.quota_status(self.base)['daily_remaining'],19)

    def test_global_across_racing_calls_with_unknown_and_rejected_provider_receipts(self):
        with self.at(self.first):
            def request(_):
                try:
                    return costs.reserve(self.base,{})
                except costs.BudgetError as exc:
                    self.assertEqual(exc.code,'drawing_window_images')
            with ThreadPoolExecutor(6) as workers:
                accepted=[x for x in workers.map(request,range(9)) if x]
            self.assertEqual(len(set(accepted)),2)
            costs.finish(self.base,accepted[0],None)
            costs.finish(self.base,accepted[1],None,rejected=True)
            self.assertEqual(costs.quota_status(self.base)['window_remaining'],0)

    def test_no_send_releases_slot_and_money_denial_does_not_start_window(self):
        with self.at(self.first):
            with self.assertRaises(costs.BudgetError):
                costs.reserve(self.base,{'reserve_usd':'.01','monthly_usd_limit':'0'})
            self.assertIsNone(costs.quota_status(self.base)['window_reset_at'])
            reserved=costs.reserve(self.base,{})
            costs.finish(self.base,reserved,None,not_submitted=True)
            quota=costs.quota_status(self.base)
            self.assertEqual((quota['daily_remaining'],quota['window_remaining']),(20,2))
            self.assertEqual(quota['window_reset_at'],self.first+1200)

