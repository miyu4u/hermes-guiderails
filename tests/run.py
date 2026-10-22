"""Run tests in a disposable Hermes home without installing or repairing Hermes."""
import argparse
import atexit
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
CATEGORIES = ("unit", "integration", "e2e")


def is_native_test(test):
    test = getattr(test, "test_case", test)
    return test.__class__.__module__.startswith("e2e.")


class CaseResult(unittest.TextTestResult):
    """Count cases independently from individual subtest outcomes."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.case_outcomes = {}
        self.native_cases = 0
        self.subtest_failures = 0
        self.subtest_errors = 0
        self.subtest_skips = 0
        self.fixture_errors = 0
        self.fixture_skips = 0

    def startTest(self, test):
        super().startTest(test)
        self.case_outcomes[test] = "passed"
        self.native_cases += bool(is_native_test(test))

    def _mark(self, test, outcome):
        test = getattr(test, "test_case", test)
        if test not in self.case_outcomes:
            return False
        priority = {"passed": 0, "skipped": 1, "failure": 2, "error": 3}
        if priority[outcome] > priority[self.case_outcomes[test]]:
            self.case_outcomes[test] = outcome
        return True

    def addFailure(self, test, err):
        self._mark(test, "failure")
        super().addFailure(test, err)

    def addError(self, test, err):
        if not self._mark(test, "error"):
            self.fixture_errors += 1
        super().addError(test, err)

    def addSkip(self, test, reason):
        if hasattr(test, "test_case"):
            self.subtest_skips += 1
        if not self._mark(test, "skipped"):
            self.fixture_skips += 1
        super().addSkip(test, reason)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            if issubclass(err[0], test.failureException):
                self.subtest_failures += 1
                self._mark(test, "failure")
            else:
                self.subtest_errors += 1
                self._mark(test, "error")
        super().addSubTest(test, subtest, err)


def native_checkout_state(checkout):
    """Return the checkout revision and cleanliness, or (None, False)."""
    try:
        def git(*args):
            return subprocess.check_output(
                ["git", "-C", str(checkout), *args],
                text=True, stderr=subprocess.DEVNULL, timeout=5,
            ).strip()

        if Path(git("rev-parse", "--show-toplevel")).resolve() != checkout.resolve():
            return None, False
        revision = git("rev-parse", "HEAD")
        clean = not git("status", "--porcelain", "--untracked-files=all")
        return revision, clean
    except (OSError, subprocess.SubprocessError):
        return None, False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--category", choices=CATEGORIES, help="run only the selected test category")
    parser.add_argument("-k", action="append", default=[], metavar="PATTERN",
                        help="select test names containing PATTERN (or matching a * wildcard); repeatable")
    parser.add_argument("--report", default="test-results.json")
    parser.add_argument("--require-native", action="store_true",
                        help="fail unless the native Hermes suite ran without skips")
    parser.add_argument("--native-sha", help="full 40-character Hermes commit SHA required for acceptance")
    args = parser.parse_args()
    if args.require_native and args.category not in (None, "e2e"):
        parser.error("--require-native needs the e2e category")
    if args.require_native and not args.native_sha:
        parser.error("--require-native requires --native-sha")
    if args.native_sha and not re.fullmatch(r"[0-9a-fA-F]{40}", args.native_sha):
        parser.error("--native-sha must be a full 40-character commit SHA")
    root = Path(os.environ.get("TMPDIR", "")).resolve()
    if (not root.is_dir() or any(not os.environ.get(key) or Path(os.environ[key]).resolve() != root
                                 for key in ("TMPDIR", "TMP", "TEMP"))
            or Path(tempfile.gettempdir()).resolve() != root):
        parser.error("TMPDIR, TMP and TEMP must name the same existing isolated directory")
    report_path = (root / args.report).resolve()
    if not report_path.is_relative_to(root):
        parser.error("--report must stay within TMPDIR")
    home = root / "harness-home"
    home.mkdir(parents=True, exist_ok=True)
    for key in list(os.environ):
        if (key.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIALS"))
                or key.startswith(("HERMES_SESSION_", "HERMES_KANBAN_"))):
            os.environ.pop(key)
    source = str(TESTS.parent / "src")
    pythonpath = os.environ.get("PYTHONPATH")
    os.environ.update(TMPDIR=str(root), TMP=str(root), TEMP=str(root),
                      PYTHONPATH=os.pathsep.join(filter(None, (source, str(TESTS), pythonpath))),
                      HOME=str(home), USERPROFILE=str(home), HERMES_REAL_HOME=str(home),
                      HERMES_HOME=str(home), HERMES_TEST_SANDBOX_HOME=str(home),
                      XDG_CONFIG_HOME=str(home / ".config"), XDG_CACHE_HOME=str(home / ".cache"),
                      XDG_DATA_HOME=str(home / ".local/share"), TERMINAL_ENV="local",
                      TERMINAL_HOME_MODE="profile", TERMINAL_CWD=str(home), HERMES_DISABLE_LAZY_INSTALLS="1",
                      PYTHONDONTWRITEBYTECODE="1")
    sys.dont_write_bytecode = True
    tempfile.tempdir = str(root)
    sys.path.insert(0, source)
    core = importlib.util.find_spec("hermes_cli")
    core_revision = None
    core_clean = False
    checkout = None
    if core and core.origin:
        checkout = Path(core.origin).resolve().parent.parent
        core_revision, core_clean = native_checkout_state(checkout)
    if args.require_native and (not core_clean or core_revision != args.native_sha.lower()):
        parser.error("--require-native needs a clean Hermes checkout at the exact --native-sha")
    with ExitStack() as boundary:
        if core is not None:
            import hermes_constants
            boundary.enter_context(patch("hermes_constants._get_platform_default_hermes_home", return_value=home))
            for target in ("hermes_cli.venv_sync.prepare_launch", "pm.environments.activate_dependencies",
                           "hermes_cli._early_recovery.recover_if_needed"):
                boundary.enter_context(patch(target, return_value=None))
            boundary.enter_context(patch("hermes_cli._early_recovery.restore_interrupted_pull", return_value=False))
            boundary.enter_context(patch("hermes_cli.env_loader.load_hermes_dotenv", return_value=[]))
            import hermes_bootstrap  # noqa: F401
            if importlib.util.find_spec("hermes_logging") is not None:
                from hermes_logging import setup_logging
                setup_logging()
        start = TESTS / args.category if args.category else TESTS
        loader = unittest.TestLoader()
        loader.testNamePatterns = [pattern if "*" in pattern else f"*{pattern}*" for pattern in args.k] or None
        suite = loader.discover(str(start), pattern=args.pattern, top_level_dir=str(TESTS))
        result = unittest.TextTestRunner(verbosity=2, resultclass=CaseResult).run(suite)
        atexit._run_exitfuncs()
    native_skips = [str(test) for test, _ in result.skipped if is_native_test(test)]
    report = {
        "tests": result.testsRun,
        "passed": list(result.case_outcomes.values()).count("passed"),
        "failures": list(result.case_outcomes.values()).count("failure"),
        "errors": list(result.case_outcomes.values()).count("error"),
        "skipped": list(result.case_outcomes.values()).count("skipped"),
        "subtest_failures": result.subtest_failures, "subtest_errors": result.subtest_errors,
        "subtest_skipped": result.subtest_skips, "fixture_errors": result.fixture_errors,
        "fixture_skipped": result.fixture_skips,
        "native_tests": result.native_cases, "native_skipped": native_skips, "pattern": args.pattern,
        "test_name_patterns": args.k,
        "category": args.category or "all",
        "python": sys.version, "interpreter": sys.executable,
        "hermes_checkout": str(checkout) if checkout else None,
        "hermes_revision": core_revision, "hermes_clean": core_clean, "tempfile": str(root),
        "fixture_boundary": "native discovery, agent loop, file handlers and SQLite; offline model; installation disabled",
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    native_ok = not args.require_native or (result.native_cases and not result.skipped)
    return 0 if result.testsRun and result.wasSuccessful() and native_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
