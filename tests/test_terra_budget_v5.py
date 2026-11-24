import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

if os.name == 'nt':
    with patch.dict(sys.modules, {'fcntl': MagicMock()}):
        supervisor = importlib.import_module('cloud_supervisor')
else:
    from hakimi.ops import cloud_supervisor as supervisor


class TerraBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.now = 1000
        self.day = supervisor._utc_day(self.now)
        self.scope = patch.multiple(supervisor, ROOT=self.root, STATE=self.state, RUNTIME=self.state)
        self.scope.start()
        self.addCleanup(self.scope.stop)

    def _raw_budget(self, value):
        (self.state / 'budget.json').write_text(json.dumps(value), encoding='utf-8')

    def _write_budget(self, **values):
        budget = {'day': self.day, 'tokens': 0, 'used_tokens': 0, 'reserved_tokens': 0,
                  'style_calls': 0, 'fault_calls': 0,
                  'ceiling': supervisor.DAILY_BASE_TOKENS, 'expansions': 0}
        budget.update(values)
        self._raw_budget(budget)
        return budget

    def _read_budget(self):
        return json.loads((self.state / 'budget.json').read_text(encoding='utf-8'))

    def test_old_113040_usage_and_twelve_style_calls_can_resume(self):
        self._raw_budget({'day': self.day, 'tokens': 113040, 'style_calls': 12, 'fault_calls': 0})
        with patch.object(supervisor, 'consult',
                          return_value=({'answer': '正常', 'uncertainty': ''},
                                        {'input_tokens': 1000, 'output_tokens': 100})):
            self.assertEqual(supervisor.budgeted_consult({}, 'style', self.now)['answer'], '正常')
        budget = self._read_budget()
        self.assertEqual(budget['tokens'], 114140)
        self.assertEqual(budget['used_tokens'], 114140)
        self.assertEqual(budget['reserved_tokens'], 0)
        self.assertEqual(budget['style_calls'], 13)
        self.assertEqual(budget['ceiling'], supervisor.DAILY_BASE_TOKENS)
        self.assertEqual(budget['expansions'], 0)

    def test_near_budget_grows_by_one_increment_and_settles_actual_usage(self):
        self._write_budget(tokens=490000, used_tokens=490000, style_calls=40)
        with patch.object(supervisor, 'consult',
                          return_value=({'answer': '正常', 'uncertainty': ''},
                                        {'input_tokens': 100, 'output_tokens': 10})):
            supervisor.budgeted_consult({}, 'style', self.now)
        budget = self._read_budget()
        self.assertEqual(budget['ceiling'], 800000)
        self.assertEqual(budget['expansions'], 1)
        self.assertEqual(budget['tokens'], 490110)
        self.assertEqual(budget['used_tokens'], 490110)
        self.assertEqual(budget['reserved_tokens'], 0)
        self.assertEqual(budget['style_calls'], 41)

    def test_style_cannot_reserve_into_one_hundred_thousand_health_tokens(self):
        tokens = supervisor.DAILY_MAX_TOKENS - supervisor.HEALTH_RESERVE_TOKENS - supervisor.TOKEN_RESERVATION + 1
        self._write_budget(tokens=tokens, used_tokens=tokens, ceiling=supervisor.DAILY_MAX_TOKENS)
        with patch.object(supervisor, 'consult') as model:
            with self.assertRaisesRegex(RuntimeError, 'daily_budget_exhausted'):
                supervisor.budgeted_consult({}, 'style', self.now)
        model.assert_not_called()
        budget = self._read_budget()
        self.assertEqual(budget['tokens'], tokens)
        self.assertEqual(budget['style_calls'], 0)
        self.assertEqual(budget['ceiling'], supervisor.DAILY_MAX_TOKENS)
        status = supervisor.budget_status(self.now)
        self.assertGreaterEqual(status['health_allowance'], supervisor.HEALTH_RESERVE_TOKENS)
        self.assertLess(status['style_allowance'], supervisor.TOKEN_RESERVATION)

    def test_absolute_ceiling_stops_fault_calls(self):
        tokens = supervisor.DAILY_MAX_TOKENS - 5000
        self._write_budget(tokens=tokens, used_tokens=tokens, fault_calls=5,
                           ceiling=supervisor.DAILY_MAX_TOKENS, expansions=7)
        with patch.object(supervisor, 'consult') as model:
            with self.assertRaisesRegex(RuntimeError, 'daily_budget_exhausted'):
                supervisor.budgeted_consult({}, 'fault', self.now)
        model.assert_not_called()
        budget = self._read_budget()
        self.assertEqual(budget['tokens'], tokens)
        self.assertEqual(budget['fault_calls'], 5)
        self.assertEqual(budget['ceiling'], supervisor.DAILY_MAX_TOKENS)
        self.assertEqual(budget['expansions'], 7)

    def test_failed_call_keeps_full_reservation_and_call_counter(self):
        self._write_budget()
        with patch.object(supervisor, 'consult', side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError):
                supervisor.budgeted_consult({}, 'fault', self.now)
        budget = self._read_budget()
        self.assertEqual(budget['tokens'], supervisor.TOKEN_RESERVATION)
        self.assertEqual(budget['used_tokens'], supervisor.TOKEN_RESERVATION)
        self.assertEqual(budget['reserved_tokens'], 0)
        self.assertEqual(budget['fault_calls'], 1)

    def test_unknown_or_invalid_usage_keeps_full_reservation(self):
        invalid = (None, [], {}, {'input_tokens': None, 'output_tokens': 5},
                   {'input_tokens': True, 'output_tokens': 5},
                   {'input_tokens': -1, 'output_tokens': 5},
                   {'input_tokens': '10', 'output_tokens': 5})
        for usage in invalid:
            with self.subTest(usage=usage):
                self._write_budget()
                with patch.object(supervisor, 'consult', return_value=({'answer': '正常'}, usage)):
                    supervisor.budgeted_consult({}, 'fault', self.now)
                budget = self._read_budget()
                self.assertEqual(budget['tokens'], supervisor.TOKEN_RESERVATION)
                self.assertEqual(budget['used_tokens'], supervisor.TOKEN_RESERVATION)
                self.assertEqual(budget['reserved_tokens'], 0)

    def test_day_rollover_starts_a_fresh_baseline(self):
        old_day = self.day
        self._write_budget(tokens=113040, used_tokens=113040, style_calls=12, fault_calls=3,
                           ceiling=800000, expansions=2)
        tomorrow = self.now + 86400
        with patch.object(supervisor, 'consult',
                          return_value=({'answer': '正常'},
                                        {'input_tokens': 600, 'output_tokens': 100})):
            supervisor.budgeted_consult({}, 'style', tomorrow)
        budget = self._read_budget()
        self.assertNotEqual(budget['day'], old_day)
        self.assertEqual(budget['day'], supervisor._utc_day(tomorrow))
        self.assertEqual(budget['tokens'], 700)
        self.assertEqual(budget['used_tokens'], 700)
        self.assertEqual(budget['reserved_tokens'], 0)
        self.assertEqual(budget['style_calls'], 1)
        self.assertEqual(budget['fault_calls'], 0)
        self.assertEqual(budget['ceiling'], supervisor.DAILY_BASE_TOKENS)
        self.assertEqual(budget['expansions'], 0)

    def test_exact_usage_settlement_including_zero(self):
        cases = (({'input_tokens': 3456, 'output_tokens': 789}, 4245),
                 ({'input_tokens': 15000, 'output_tokens': 0}, 15000),
                 ({'input_tokens': 0, 'output_tokens': 0}, 0))
        for usage, expected in cases:
            with self.subTest(usage=usage):
                self._write_budget()
                with patch.object(supervisor, 'consult', return_value=({'answer': '正常'}, usage)):
                    supervisor.budgeted_consult({}, 'fault', self.now)
                budget = self._read_budget()
                self.assertEqual(budget['tokens'], expected)
                self.assertEqual(budget['used_tokens'], expected)
                self.assertEqual(budget['reserved_tokens'], 0)

    def test_style_check_resumes_after_the_old_twelve_call_guard(self):
        state = {'active': True, 'next_due': 0, 'budget_day': self.day,
                 'daily_calls': 12, 'interval_minutes': 5}
        sample = {'dsh_ids': ['1'], 'dsh': ['一句'], 'gemini': ['原口吻'],
                  'baseline_source': '真实样本'}
        with patch.object(supervisor, 'takeover', return_value=True), \
                patch.object(supervisor, 'capture_samples', return_value=sample), \
                patch.object(supervisor, 'budgeted_consult',
                             return_value={'answer': '正常：自然接话'}) as model:
            supervisor.style_check(state, self.now)
        self.assertEqual(state['daily_calls'], 13)
        self.assertEqual(state['attempt_count'], 1)
        model.assert_called_once()

    def test_report_exposes_budget_metadata_without_expanding_it(self):
        self._write_budget(tokens=123456, used_tokens=120000, reserved_tokens=3456,
                           style_calls=4, fault_calls=1, ceiling=800000, expansions=1)
        (self.state / 'style.json').write_text(
            json.dumps({'daily_calls': 12, 'next_due': 0}), encoding='utf-8')
        (self.state / 'latest.json').write_text('{"status":"healthy"}', encoding='utf-8')
        capture = io.StringIO()
        with patch.object(supervisor, 'inspect', return_value=[]), \
                patch.object(supervisor, 'budgeted_consult') as model, \
                patch.object(supervisor.time, 'time', return_value=self.now), \
                patch.object(sys, 'argv', ['supervisor', '--report']), \
                contextlib.redirect_stdout(capture):
            supervisor.main()
        value = json.loads(capture.getvalue())
        self.assertEqual(value['report']['status'], 'healthy')
        budget = value['budget']
        self.assertEqual(value['health']['budget'], budget)
        self.assertEqual(budget['effective_ceiling'], 800000)
        self.assertEqual(budget['absolute_ceiling'], supervisor.DAILY_MAX_TOKENS)
        self.assertEqual(budget['used_tokens'], 120000)
        self.assertEqual(budget['reserved_tokens'], 3456)
        self.assertEqual(budget['style_calls'], 4)
        self.assertEqual(budget['style_call_limit'], supervisor.STYLE_CALL_LIMIT)
        self.assertEqual(budget['fault_calls'], 1)
        self.assertEqual(budget['fault_call_limit'], supervisor.FAULT_CALL_LIMIT)
        self.assertEqual(budget['style_allowance'],
                         800000 - supervisor.HEALTH_RESERVE_TOKENS - 123456)
        self.assertEqual(budget['health_allowance'], 800000 - 123456)
        self.assertEqual(budget['health_reserve_tokens'], supervisor.HEALTH_RESERVE_TOKENS)
        self.assertEqual(budget['expansions'], 1)
        self.assertEqual(budget['style_daily_calls'], 12)
        self.assertEqual(budget['style_daily_call_limit'], supervisor.STYLE_CALL_LIMIT)
        model.assert_not_called()
        stored = self._read_budget()
        self.assertEqual(stored['tokens'], 123456)
        self.assertEqual(stored['ceiling'], 800000)
        self.assertEqual(stored['expansions'], 1)


if __name__ == '__main__':
    unittest.main()
