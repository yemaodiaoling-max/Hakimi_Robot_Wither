"""Run optional components independently, as in a minimal installation."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


class ModuleBoundaryTests(unittest.TestCase):
    def run_isolated(self, code):
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, HAKIMI_ROOT=temp, HAKIMI_BASE=temp, HAKIMI_GATEWAY_DIR=temp,
                       PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
            process = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=temp, env=env,
                                     text=True, capture_output=True, timeout=15)
            self.assertEqual(process.returncode, 0, process.stderr)

    def test_console_settings_routes_work_without_memory_database(self):
        self.run_isolated("""
            import sys
            sys.modules['hakimi.console.console_db'] = None
            from hakimi.setup import panel_api
            from hakimi.console import setup_api
            from hakimi.console.errors import ApiError
            assert panel_api is setup_api
            assert panel_api.activity_payload() == {'available': False, 'reason': 'module_disabled'}
            try:
                setup_api.setup_post('/api/unknown', {})
            except ApiError as exc:
                assert (exc.status, exc.code) == (404, 'not_found')
            else:
                raise AssertionError('unknown settings route succeeded')
        """)

    def test_budget_report_does_not_require_model_client_or_http_server(self):
        self.run_isolated("""
            import sys
            from pathlib import Path
            sys.modules['hakimi.models.vertex_route'] = None
            sys.modules['hakimi.gateway.server'] = None
            from hakimi.gateway.ledger import budget_payload
            report = budget_payload(Path.cwd(), {'monthly_limit_usd': '1'}, now=0)
            assert report['spent_usd'] == '0.000000000' and not report['blocked']
        """)
