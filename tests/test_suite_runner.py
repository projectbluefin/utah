#!/usr/bin/env python3
"""Prove the host-side test gate reaches nested suites and can fail.

`tests/unit/` was invisible to CI: `just check` called `just test`, which ran
`python3 -m unittest discover -s tests -p 'test_*.py'`, and that skips any
subdirectory it cannot import as a package. Adding 35 test cases under
`tests/unit/` changed neither the reported test count nor the exit status.

These tests exercise `tests/run_suite.py` against a synthetic tree rather than
against this repository's own suite, so they assert the runner's behaviour
directly: a passing nested test is executed, and a failing nested test makes
the runner exit non-zero. The last test pins `just test` to the runner, because
a gate that is not wired into `check` protects nothing no matter how correct it
is.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO = TESTS_DIR.parent
RUNNER = TESTS_DIR / "run_suite.py"

PASSING = """\
import unittest


class Passing(unittest.TestCase):
    def test_ok(self):
        self.assertTrue(True)
"""

FAILING = """\
import unittest


class Failing(unittest.TestCase):
    def test_not_ok(self):
        self.fail("nested suite ran")
"""


def write_tree(root: Path, files: dict[str, str]) -> None:
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def run_runner(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RUNNER), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


class TestRunnerReachesNestedSuites(unittest.TestCase):
    def test_runner_exists_and_is_tracked(self):
        self.assertTrue(RUNNER.is_file(), f"Missing {RUNNER}")
        tracked = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "--error-unmatch",
             str(RUNNER.relative_to(REPO))],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            tracked.returncode, 0,
            "tests/run_suite.py must be tracked or CI will not have it",
        )

    def test_nested_directory_is_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_tree(root, {
                "test_top.py": PASSING,
                "unit/test_nested.py": PASSING,
                "unit/deeper/test_deeper.py": PASSING,
            })
            dirs = _discover(root)
            self.assertEqual(
                dirs,
                sorted([root, root / "unit", root / "unit" / "deeper"]),
                "every directory holding test_*.py must be discovered",
            )

    def test_failing_nested_test_fails_the_run(self):
        """The regression itself: this exited 0 under `unittest discover`."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_tree(root, {
                "test_top.py": PASSING,
                "unit/test_nested.py": FAILING,
            })
            result = run_runner(root)
            self.assertNotEqual(
                result.returncode, 0,
                "a failing test under a subdirectory must fail the run:\n"
                f"{result.stdout}\n{result.stderr}",
            )
            self.assertIn("nested suite ran", result.stderr)

    def test_passing_tree_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_tree(root, {
                "test_top.py": PASSING,
                "unit/test_nested.py": PASSING,
            })
            result = run_runner(root)
            self.assertEqual(
                result.returncode, 0,
                f"{result.stdout}\n{result.stderr}",
            )

    def test_empty_tree_is_an_error_not_a_silent_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_runner(Path(tmp))
            self.assertEqual(result.returncode, 1)
            self.assertIn("no test_*.py files found", result.stderr)


class TestJustfileUsesTheRunner(unittest.TestCase):
    def setUp(self):
        self.justfile = (REPO / "Justfile").read_text(encoding="utf-8")

    def test_test_recipe_invokes_the_runner(self):
        self.assertIn(
            "tests/run_suite.py", self.justfile,
            "the `test` recipe must run tests/run_suite.py, otherwise nested "
            "suites stop being executed again",
        )

    def test_test_recipe_does_not_use_bare_discovery(self):
        self.assertNotIn(
            "unittest discover -s tests", self.justfile,
            "`unittest discover -s tests` silently skips tests/ subdirectories",
        )

    def test_check_recipe_runs_the_test_recipe(self):
        self.assertIn(
            "just test", self.justfile,
            "`check` must call `test`; build.yml only runs `just check`",
        )


def _discover(root: Path) -> list[Path]:
    sys.path.insert(0, str(TESTS_DIR))
    try:
        import run_suite
    finally:
        sys.path.pop(0)
    return run_suite.discover_test_dirs(root)


if __name__ == "__main__":
    unittest.main()
