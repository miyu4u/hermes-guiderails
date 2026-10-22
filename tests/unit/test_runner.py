import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import run


class RunnerAccountingTests(unittest.TestCase):
    def test_cases_are_distinct_from_subtest_outcomes(self):
        class Examples(unittest.TestCase):
            def test_pass(self):
                pass

            def test_many_failures(self):
                for number in range(6):
                    with self.subTest(number=number):
                        self.fail("fixture")

            def test_subtest_error_and_skip(self):
                with self.subTest(kind="skip"):
                    self.skipTest("fixture")
                with self.subTest(kind="error"):
                    raise ValueError("fixture")

            def test_case_skip(self):
                self.skipTest("fixture")

            def test_case_error(self):
                raise ValueError("fixture")

            def test_case_failure(self):
                self.fail("fixture")

        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=run.CaseResult).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(Examples)
        )
        outcomes = list(result.case_outcomes.values())
        self.assertEqual(result.testsRun, 6)
        self.assertEqual({name: outcomes.count(name) for name in ("passed", "failure", "error", "skipped")},
                         {"passed": 1, "failure": 2, "error": 2, "skipped": 1})
        self.assertEqual((result.subtest_failures, result.subtest_errors, result.subtest_skips), (6, 1, 1))
        self.assertFalse(result.wasSuccessful())

    def test_fixture_error_does_not_create_a_case(self):
        class BrokenFixture(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise RuntimeError("fixture")

            def test_never_runs(self):
                pass

        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=run.CaseResult).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(BrokenFixture)
        )
        self.assertEqual(result.testsRun, 0)
        self.assertEqual(result.case_outcomes, {})
        self.assertEqual(result.fixture_errors, 1)
        self.assertFalse(result.wasSuccessful())

    def test_native_subtest_skip_belongs_to_parent(self):
        native = type("NativeCase", (unittest.TestCase,), {"__module__": "e2e.example"})
        parent = native()
        self.assertTrue(run.is_native_test(SimpleNamespace(test_case=parent)))

    def test_fixture_skip_is_not_a_case_or_subtest(self):
        class SkippedFixture(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise unittest.SkipTest("fixture")

            def test_never_runs(self):
                pass

        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=run.CaseResult).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(SkippedFixture)
        )
        self.assertEqual(result.testsRun, 0)
        self.assertEqual((result.fixture_skips, result.subtest_skips), (1, 0))
        self.assertEqual(result.case_outcomes, {})


class CheckoutTests(unittest.TestCase):
    def test_clean_and_dirty_checkout(self):
        checkout = Path("/fixture/hermes")
        sha = "a" * 40
        with patch.object(run.subprocess, "check_output", side_effect=[str(checkout), sha, ""]) as git:
            self.assertEqual(run.native_checkout_state(checkout), (sha, True))
            self.assertIn("--untracked-files=all", git.call_args.args[0])
        with patch.object(run.subprocess, "check_output", side_effect=[str(checkout), sha, "?? new.py"]):
            self.assertEqual(run.native_checkout_state(checkout), (sha, False))
        with patch.object(run.subprocess, "check_output", side_effect=[str(checkout), sha, " M file.py"]):
            self.assertEqual(run.native_checkout_state(checkout), (sha, False))

    def test_rejects_non_checkout_and_git_failure(self):
        with patch.object(run.subprocess, "check_output", return_value="/fixture"):
            self.assertEqual(run.native_checkout_state(Path("/fixture/hermes")), (None, False))
        with patch.object(run.subprocess, "check_output", side_effect=subprocess.CalledProcessError(1, "git")):
            self.assertEqual(run.native_checkout_state(Path("/fixture/hermes")), (None, False))


class RunnerCliTests(unittest.TestCase):
    def invoke(self, root, args, *, core=None, state=(None, False), suite=None):
        loader = unittest.TestLoader()
        suite = suite if suite is not None else unittest.TestSuite()
        with patch.object(sys, "argv", ["run.py", *args]), \
                patch.dict(os.environ, {"TMPDIR": str(root), "TMP": str(root), "TEMP": str(root)}), \
                patch.object(run.tempfile, "gettempdir", return_value=str(root)), \
                patch.object(run.tempfile, "tempdir", str(root)), \
                patch.object(sys, "path", list(sys.path)), \
                patch.object(sys, "dont_write_bytecode", sys.dont_write_bytecode), \
                patch.object(run.importlib.util, "find_spec", return_value=core), \
                patch.object(run, "native_checkout_state", return_value=state), \
                patch.object(run.unittest, "TestLoader", return_value=loader), \
                patch.object(loader, "discover", return_value=suite), \
                patch.object(run.atexit, "_run_exitfuncs"), \
                patch("sys.stdout", new_callable=io.StringIO), \
                patch("sys.stderr", new_callable=io.StringIO):
            return run.main(), loader

    def test_explicit_sha_is_required_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for args in (["--require-native"], ["--require-native", "--native-sha", "abc"]):
                with self.subTest(args=args), self.assertRaises(SystemExit) as error:
                    self.invoke(root, args)
                self.assertEqual(error.exception.code, 2)

    def test_acceptance_rejects_missing_dirty_or_mismatched_checkout(self):
        sha = "a" * 40
        core = SimpleNamespace(origin="/fixture/hermes/hermes_cli/__init__.py")
        with tempfile.TemporaryDirectory() as directory:
            for spec, state in ((None, (None, False)), (core, (sha, False)), (core, ("b" * 40, True))):
                with self.subTest(state=state), self.assertRaises(SystemExit) as error:
                    self.invoke(Path(directory), ["--require-native", "--native-sha", sha], core=spec, state=state)
                self.assertEqual(error.exception.code, 2)

    def test_exploratory_selection_and_report(self):
        class Passing(unittest.TestCase):
            def test_example(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(Passing)
            code, loader = self.invoke(root, ["-k", "example", "-k", "other*"], suite=suite)
            report = json.loads((root / "test-results.json").read_text())
        self.assertEqual(code, 0)
        self.assertEqual(loader.testNamePatterns, ["*example*", "other*"])
        self.assertEqual(report["test_name_patterns"], ["example", "other*"])
        self.assertEqual((report["tests"], report["passed"], report["failures"]), (1, 1, 0))

    def test_selection_patterns_filter_test_names(self):
        class Examples(unittest.TestCase):
            def test_first(self):
                pass

            def test_second(self):
                pass

            def test_unselected(self):
                pass

        loader = unittest.TestLoader()
        loader.testNamePatterns = ["*first*", "*second"]
        selected = list(loader.loadTestsFromTestCase(Examples))
        self.assertEqual([test._testMethodName for test in selected], ["test_first", "test_second"])


if __name__ == "__main__":
    unittest.main()
