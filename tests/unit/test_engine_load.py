"""Isolate load probes so a stuck matcher cannot block the test runner."""

from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from guiderails.engine import InspectionError, Limits, RuleEngine
from guiderails.rules import PatternError

from unit.test_engine import document, rule

SCOPE = 'artifact.text'


class EngineLoadTests(unittest.TestCase):
    def run_isolated(self, name):
        try:
            result = subprocess.run(
                [sys.executable, '-B', str(Path(__file__).resolve()), f'{type(self).__name__}.{name}'],
                capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            self.fail(f'{name}: isolated process did not finish before the 15-second watchdog')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_super_bulk_text_is_bounded_and_next_inspection_completes(self):
        self.run_isolated('probe_bulk_text')

    def test_complex_regex_compile_and_search_are_bounded(self):
        self.run_isolated('probe_complex_regex')

    def probe_bulk_text(self):
        engine = RuleEngine.from_yaml(document(rule()))
        with self.assertRaisesRegex(InspectionError, '^candidate_too_large$'):
            engine.inspect('가' * (8 * 1024 * 1024), scope=SCOPE, first=True)  # 24 MiB of UTF-8

        # At the configured limit, accept the benign input or fail closed on
        # the deadline; neither outcome may wedge the next inspection.
        near_limit = 'x' * (engine.limits.max_candidate_bytes - 6) + '금지'
        try:
            hits = engine.inspect(near_limit, scope=SCOPE, first=True)
        except InspectionError as exc:
            self.assertIn(str(exc), ('inspection_deadline', 'inspection_limit'))
        else:
            hit, = hits
            self.assertEqual((hit.start, hit.end), (len(near_limit) - 2, len(near_limit)))
        self.assertEqual([hit.rule_id for hit in engine.inspect('금지', scope=SCOPE)], ['demo.rule'])

    def probe_complex_regex(self):
        expression = r'(?:' + '|'.join(f'item{i:03d}' for i in range(400)) + r'):[0-9]{4}'
        spec = document(rule(
            matcher={'type': 'regex', 'pattern': expression, 'flags': [], 'normalization': 'none'},
            tests={'positive': ['item399:1234'], 'negative': ['unrelated']},
        ))
        engine = RuleEngine.from_yaml(spec)
        self.assertEqual([(hit.start, hit.end) for hit in engine.inspect('item399:1234', scope=SCOPE)], [(0, 12)])
        with self.assertRaisesRegex(PatternError, '^regex_compile_timeout$'):
            RuleEngine.from_yaml(spec, limits=Limits(compile_timeout_seconds=1e-9))

        # Reuse the successful compilation so that only the actual search is
        # subject to the intentionally tiny budget.
        bounded = RuleEngine(engine.snapshot, limits=Limits(regex_timeout_ms=1e-6))
        with self.assertRaisesRegex(InspectionError, '^regex_timeout$'):
            bounded.inspect('item999:9876 ' * 40_000, scope=SCOPE, first=True)
        self.assertEqual(engine.inspect('unrelated', scope=SCOPE), ())

    def test_first_completed_violation_stops_slower_rules_and_leaves_no_match_work_queued(self):
        rules = [rule('demo.slow', priority=200), rule('demo.fast')]
        rules.extend(rule(f'demo.remaining-{index:02}', priority=1) for index in range(12))
        engine = RuleEngine.from_yaml(document(*rules))
        inspect_rule = engine._matches
        slow_started = threading.Event()
        slow_cancelled = threading.Event()
        threads = {}

        def trace_match(text, entry, *args, **kwargs):
            rule_id = entry['id']
            threads[rule_id] = threading.get_ident()
            if rule_id == 'demo.fast':
                if not slow_started.wait(1):
                    raise AssertionError('fast rule ran without the slow rule in flight')
                return inspect_rule(text, entry, *args, **kwargs)
            if rule_id == 'demo.slow':
                slow_started.set()
                if kwargs['stop'].wait(2):
                    slow_cancelled.set()
            else:
                kwargs['stop'].wait(2)
            return []

        with patch.object(engine, '_matches', side_effect=trace_match):
            hits = engine.inspect('금지', scope=SCOPE, first=True)
        self.assertEqual([hit.rule_id for hit in hits], ['demo.fast'])
        self.assertTrue(slow_cancelled.wait(1))
        self.assertNotEqual(threads['demo.slow'], threads['demo.fast'])
        self.assertNotIn(threading.get_ident(), threads.values())
        self.assertLessEqual(len(threads), 4)


if __name__ == '__main__':
    unittest.main()
