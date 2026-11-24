"""Tests for scripts/graduation-check.py.

The graduation check measures advisory gates against a bar and reports the ones
that have passed long enough to become blocking. The only pure, network-free
logic is grade_runs: it counts the passes since the last failure and the failure
share over the lookback window. These tests cover a gate that is ready, one that
is too new, one that failed recently, one with a noisy failure rate, and the
per-criterion graduation overrides.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "graduation_check", ROOT / "scripts" / "graduation-check.py")
graduation_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(graduation_check)
gc = graduation_check


def run(conclusion, status="completed"):
    return {"status": status, "conclusion": conclusion}


BAR = {
    "required_consecutive_passes": 3,
    "maximum_failure_rate": 0.10,
    "lookback_runs": 20,
}


class GradeRunsTests(unittest.TestCase):
    def test_ready_when_passing_streak_and_low_failures(self):
        # Ten newest runs pass, one older run failed: the streak is 10 and the
        # single failure is only ~9% of the window, under the 10% bar.
        runs = [run("success")] * 10 + [run("failure")]
        result = gc.grade_runs(runs, BAR)
        self.assertTrue(result["ready"])
        self.assertEqual(result["consecutive_passes"], 10)
        self.assertEqual(result["failure_rate"], round(1 / 11, 3))

    def test_not_ready_too_few_consecutive_passes(self):
        runs = [run("success"), run("success"), run("failure")]
        result = gc.grade_runs(runs, BAR)
        self.assertFalse(result["ready"])
        self.assertEqual(result["consecutive_passes"], 2)

    def test_recent_failure_breaks_streak(self):
        # Newest-first: the failure is the most recent run, so the streak is 0
        # and the failure share over the window is 1/4 = 0.25 > 0.10.
        runs = [run("failure"), run("success"), run("success"), run("success")]
        result = gc.grade_runs(runs, BAR)
        self.assertEqual(result["consecutive_passes"], 0)
        self.assertFalse(result["ready"])
        self.assertEqual(result["failure_rate"], 0.25)

    def test_noise_runs_are_neither_pass_nor_failure(self):
        # A skipped/neutral run does not count as a pass, so it breaks the
        # consecutive streak, but it is not a failure either.
        runs = [run("skipped", status="skipped"),
                run("success"), run("success"), run("success")]
        result = gc.grade_runs(runs, BAR)
        self.assertEqual(result["consecutive_passes"], 3)
        self.assertEqual(result["failure_rate"], 0.0)

    def test_failure_rate_boundary_is_inclusive(self):
        # 1 failure in 10 runs = exactly 0.10, which is allowed.
        runs = [run("success")] * 9 + [run("failure")]
        result = gc.grade_runs(runs, BAR)
        self.assertEqual(result["failure_rate"], 0.1)
        self.assertTrue(result["ready"])

    def test_lookback_limits_window(self):
        # 25 runs, 1 failure, but lookback is 20: the failure is outside the
        # window, so the window is all passes.
        runs = [run("success")] * 20 + [run("failure")] * 5
        result = gc.grade_runs(runs, BAR)
        self.assertEqual(result["runs_seen"], 20)
        self.assertEqual(result["failure_rate"], 0.0)

    def test_empty_runs(self):
        result = gc.grade_runs([], BAR)
        self.assertFalse(result["ready"])
        self.assertEqual(result["consecutive_passes"], 0)


class GraduationBarTests(unittest.TestCase):
    def test_overrides_applied(self):
        crit = {"graduation": {"required_consecutive_passes": 2}}
        bar = gc.graduation_bar(BAR, crit)
        self.assertEqual(bar["required_consecutive_passes"], 2)
        # Unoverridden fields fall back to the global bar.
        self.assertEqual(bar["maximum_failure_rate"], 0.10)


class GateJobConclusionTests(unittest.TestCase):
    def test_all_matrix_legs_pass_returns_success(self):
        # A matrix gate with two passing legs counts as a pass, so the
        # graduation check can mark it READY (this is what the function used to
        # never return before the fix).
        jobs = [
            {"name": "e2e (${{ matrix.image }})", "conclusion": "success"},
            {"name": "e2e (${{ matrix.image }})", "conclusion": "success"},
        ]
        run = {"jobs_url": "https://api.github.com/repos/o/r/actions/runs/1/jobs"}
        with patch.object(gc, "_http_get_json",
                          side_effect=lambda url, token: {"jobs": jobs}):
            self.assertEqual(gc._run_gate_conclusion("t", run, "e2e"), "success")

    def test_a_failed_leg_reports_the_failure(self):
        jobs = [
            {"name": "e2e (fedora)", "conclusion": "success"},
            {"name": "e2e (rhel)", "conclusion": "failure"},
        ]
        run = {"jobs_url": "https://api.github.com/repos/o/r/actions/runs/1/jobs"}
        with patch.object(gc, "_http_get_json",
                          side_effect=lambda url, token: {"jobs": jobs}):
            self.assertEqual(gc._run_gate_conclusion("t", run, "e2e"), "failure")

    def test_unrelated_prefix_job_is_not_matched(self):
        # e2e-cleanup must not be confused with the e2e gate.
        jobs = [{"name": "e2e-cleanup", "conclusion": "success"}]
        run = {"jobs_url": "https://api.github.com/repos/o/r/actions/runs/1/jobs"}
        with patch.object(gc, "_http_get_json",
                          side_effect=lambda url, token: {"jobs": jobs}):
            self.assertIsNone(gc._run_gate_conclusion("t", run, "e2e"))


if __name__ == "__main__":
    unittest.main()
