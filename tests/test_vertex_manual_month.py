import json
import tempfile
import time
import unittest
from pathlib import Path

from hakimi.models import vertex_route as vr


class ManualVertexBudget(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.now = time.time()
        self.config = dict(enabled=True, gateway_directory=str(vr.PRODUCTION_DIRECTORY),
                           model=vr.MODEL, month_limit_usd=None, fallback='dsh')
        (self.state / vr.ROUTE_STATE).write_text(json.dumps(self.config))
        self.payload = dict(schema_version=1, project=vr.PROJECT, model=vr.MODEL, currency='USD',
                            period=vr._pacific_month(self.now), timezone=vr.TIMEZONE, limit_usd=None,
                            spent_usd='130.25', reserved_usd='2', remaining_usd=None, blocked=False,
                            block_reason=None, measurement=vr.MEASUREMENT, price_verified_at=vr.PRICE_VERIFIED_AT,
                            official_spend_cap={'configured': True, 'name': 'external'},
                            credits={'official_remaining_usd': None, 'source': 'not_connected', 'note': ''},
                            as_of=vr._now_iso(self.now), next_reset_at=vr._now_iso(self.now + 86400))

    def roundtrip(self):
        route = vr.load(self.root)
        self.assertIsNone(route.error)
        budget = vr.validate_budget_payload(self.payload, now=self.now)
        status = vr.build_status(route, budget=budget, now=self.now)
        (self.state / vr.BUDGET_STATE).write_text(json.dumps(status))
        return vr.console_status(self.root, now=self.now)

    def test_over_old_cap_reports_usage_without_blocking(self):
        status = self.roundtrip()
        self.assertTrue(status['fresh'])
        self.assertFalse(status['blocked'])
        self.assertIsNone(status['remaining_usd'])
        self.assertIsNone(status['limit_usd'])
        self.assertEqual(status['spent_usd'], '130.250000000')
        self.assertTrue(status['official_spend_cap']['configured'])

    def test_operator_pause_still_blocks(self):
        self.payload.update(blocked=True, block_reason='manual_pause')
        status = self.roundtrip()
        self.assertTrue(status['blocked'])
        self.assertEqual(status['block_reason'], 'manual_pause')

    def test_old_gateway_status_during_upgrade(self):
        self.payload.update(limit_usd='110', remaining_usd='0', blocked=True,
                            block_reason='vertex_monthly_budget_exhausted')
        self.assertTrue(self.roundtrip()['blocked'])

    def test_missing_or_mixed_fields_are_invalid(self):
        for field in ('remaining_usd', 'limit_usd'):
            payload = dict(self.payload)
            del payload[field]
            with self.assertRaises(vr.GatewayUnavailable):
                vr.validate_budget_payload(payload, now=self.now)
        self.payload['remaining_usd'] = '0'
        with self.assertRaises(vr.GatewayUnavailable):
            self.roundtrip()
