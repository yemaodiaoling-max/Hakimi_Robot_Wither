import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hakimi.gateway import config, ledger, server
from hakimi.models.vertex_route import validate_budget_payload


class GatewayLedgerTests(unittest.TestCase):
    def test_record_and_budget_agree_across_los_angeles_month_end(self):
        before_reset = datetime(2026, 8, 1, 6, 59, 59, tzinfo=timezone.utc).timestamp()
        after_reset = before_reset + 1
        settings = {
            "monthly_limit_usd": "0.000020",
            "prices": {"upstream": {"input_usd_per_million": "2", "output_usd_per_million": "4"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            # Separate clock reads would pair the July timestamp with an August period.
            with patch.object(ledger.time, "time", side_effect=[before_reset, after_reset]):
                server.record(
                    base, settings, request_id="r1", model="internal", upstream_model="upstream",
                    usage={"promptTokenCount": 4, "candidatesTokenCount": 2, "thoughtsTokenCount": 1,
                           "cachedContentTokenCount": 1}, status=200,
                )
            self.assertEqual(ledger.month_spend(base, before_reset), Decimal("0.000020"))
            self.assertEqual(ledger.month_spend(base, after_reset), Decimal(0))
            self.assertTrue(ledger.monthly_budget_exhausted(base, settings, before_reset))
            self.assertFalse(ledger.monthly_budget_exhausted(base, settings, after_reset))
            with closing(sqlite3.connect(base / ledger.LEDGER_NAME)) as db:
                row = db.execute("SELECT at, period, cached_tokens FROM usage WHERE id='r1'").fetchone()
            self.assertEqual(row, (before_reset, "2026-07", 1))
            july = validate_budget_payload(ledger.budget_payload(base, settings, before_reset), now=before_reset)
            august = validate_budget_payload(ledger.budget_payload(base, settings, after_reset), now=after_reset)
            self.assertTrue(july["blocked"])
            self.assertEqual(july["next_reset_at"], "2026-08-01T07:00:00Z")
            self.assertFalse(august["blocked"])

    def test_month_tokens_reports_cached_total_without_changing_cost(self):
        settings = {"prices": {"upstream": {"input_usd_per_million": "2", "output_usd_per_million": "4"}}}
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self.assertEqual(ledger.month_tokens(base)["cached_tokens"], 0)
            for ident, cached in (("a", 30), ("b", 0)):
                ledger.record(base, settings, request_id=ident, model="m", upstream_model="upstream",
                              usage={"promptTokenCount": 100, "candidatesTokenCount": 5,
                                     "cachedContentTokenCount": cached}, status=200)
            tokens = ledger.month_tokens(base)
            self.assertEqual(tokens, {"requests": 2, "input_tokens": 200, "output_tokens": 10, "cached_tokens": 30})
            self.assertEqual(ledger.month_spend(base), Decimal("0.00044"))

    def test_http_rejects_generation_at_cap_but_allows_count_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config.save_upstream_key("upstream-test", base)
            settings = config.load_config(base)
            settings["upstream"]["protocol"] = "openai_compatible"
            settings["monthly_limit_usd"] = "1"
            settings["prices"] = {"gemini-4-argon": {"input_usd_per_million": "1000000"}}
            config.save_config(settings, base)
            ledger.record(base, settings, request_id="r1", model="gemini-4-argon",
                          upstream_model="gemini-4-argon", usage={"promptTokenCount": 1}, status=200)
            handler = server.GatewayHandler.__new__(server.GatewayHandler)
            handler.server = SimpleNamespace(base=base, key="gateway-test")
            handler.client_address = ("127.0.0.1", 0)
            handler.headers = {"Authorization": "Bearer gateway-test", "Content-Length": "2"}
            handler._send = Mock()

            handler.path = "/budget"
            handler.do_GET()
            response_code, payload = handler._send.call_args.args
            self.assertEqual(response_code, 200)
            self.assertTrue(payload["blocked"])

            target = "/v1beta/models/gemini-4-argon:"
            handler.path = target + "generateContent"
            handler.rfile = BytesIO(b"{}")
            handler.do_POST()
            response_code, payload, _ = handler._send.call_args.args
            self.assertEqual(response_code, 403)
            self.assertEqual(payload["error"]["message"], ledger.MONTHLY_EXHAUSTED)

            handler.path = target + "countTokens"
            handler.rfile = BytesIO(b"{}")
            handler.do_POST()
            response_code, payload, _ = handler._send.call_args.args
            self.assertEqual((response_code, payload), (200, {"totalTokens": 1}))
            self.assertEqual(ledger.month_tokens(base)["requests"], 1)
