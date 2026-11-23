"""Tests for scripts/green_contract.py.

The green contract reads .github/green-criteria.yml and the workflow files its
gates point at, and fails when a declared gate does not really block. These
tests cover the shipped criteria passing, plus one synthetic repo per rule:
missing workflow/job/script, an unreachable workflow, a blocking gate that is
not in the promotion needs, a blocking gate that can fail silently, and a
scheduled gate whose cadence is slower than its freshness window. They also
check the cron-interval helper used by the staleness rule.
"""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import green_contract as gc  # noqa: E402
import yaml


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def make_repo(tmp: Path, *, criteria, workflow_text, promote_needs,
              workflow_path=".github/workflows/post-testing-e2e.yml",
              promote_if=None):
    write(tmp / ".github" / "green-criteria.yml", f"version: 1\n\n"
              f"criteria:\n{criteria}")
    # The contract reads the promote-to-testing job's `needs` from the same
    # workflow file, so merge that job into the workflow's jobs without
    # clobbering the gate jobs the test supplies. When no explicit `if:` is
    # given, build one that requires each needed job to succeed, mirroring the
    # shipped promote job.
    wf = (yaml.safe_load(workflow_text) if isinstance(workflow_text, str)
          else workflow_text)
    promote = {"needs": list(promote_needs),
               "runs-on": "ubuntu-26.04",
               "steps": [{"run": "echo ok"}]}
    if promote_if is not None:
        promote["if"] = promote_if
    elif promote_needs:
        promote["if"] = " && ".join(
            f"needs.{n}.result == 'success'" for n in promote_needs)
    wf.setdefault("jobs", {})["promote-to-testing"] = promote
    write(tmp / workflow_path, yaml.safe_dump(wf))
    return tmp


class ShippedCriteriaTests(unittest.TestCase):
    def test_shipped_criteria_satisfy_the_contract(self):
        errors = gc.check_spec(gc.load_spec(ROOT), ROOT)
        self.assertEqual(errors, [], f"shipped criteria should be green:\n"
                                     f"\n".join(errors))


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _check(self, criteria, workflow_text, promote_needs,
               workflow_path=".github/workflows/post-testing-e2e.yml",
               promote_if=None):
        make_repo(self.root, criteria=criteria, workflow_text=workflow_text,
                  promote_needs=promote_needs, workflow_path=workflow_path,
                  promote_if=promote_if)
        spec = gc.load_spec(self.root)
        return gc.check_spec(spec, self.root)

    def test_missing_workflow_file(self):
        criteria = """  - id: g
    enforcement: blocking
    freshness_sla_days: 2
    gates:
      - workflow: .github/workflows/nope.yml
        job: lifecycle"""
        errors = self._check(criteria, "name: x\n", [])
        self.assertTrue(any("workflow not found" in e for e in errors))

    def test_missing_job(self):
        criteria = """  - id: g
    enforcement: blocking
    gates:
      - workflow: .github/workflows/post-testing-e2e.yml
        job: ghost"""
        workflow = "name: x\njobs:\n  real:\n    runs-on: ubuntu-26.04\n"
        errors = self._check(criteria, workflow, [])
        self.assertTrue(any("job 'ghost'" in e for e in errors))

    def test_missing_script(self):
        criteria = """  - id: g
    enforcement: blocking
    gates:
      - workflow: .github/workflows/post-testing-e2e.yml
        job: lifecycle
        script: iso/scripts/does-not-exist.sh"""
        workflow = ("name: x\njobs:\n  lifecycle:\n    runs-on: ubuntu-26.04\n")
        errors = self._check(criteria, workflow, ["lifecycle"])
        self.assertTrue(any("script not found" in e for e in errors))

    def test_unreachable_workflow(self):
        criteria = """  - id: g
    enforcement: advisory
    gates:
      - workflow: .github/workflows/post-testing-e2e.yml
        job: lifecycle"""
        # No `on:` at all.
        workflow = "name: x\njobs:\n  lifecycle:\n    runs-on: ubuntu-26.04\n"
        errors = self._check(criteria, workflow, [])
        self.assertTrue(any("not reachable" in e for e in errors))

    def test_blocking_gate_not_in_promotion_needs(self):
        criteria = """  - id: g
    enforcement: blocking
    gates:
      - workflow: .github/workflows/post-testing-e2e.yml
        job: lifecycle"""
        workflow = "name: x\njobs:\n  lifecycle:\n    runs-on: ubuntu-26.04\n"
        errors = self._check(criteria, workflow, ["resolve", "testsuite"])
        self.assertTrue(any("not in" in e and "needs" in e for e in errors))

    def test_blocking_gate_with_continue_on_error(self):
        criteria = """  - id: g
    enforcement: blocking
    gates:
      - workflow: .github/workflows/post-testing-e2e.yml
        job: lifecycle"""
        workflow = ("name: x\njobs:\n  lifecycle:\n"
                    "    runs-on: ubuntu-26.04\n"
                    "    continue-on-error: true\n")
        errors = self._check(criteria, workflow, ["lifecycle"])
        self.assertTrue(any("continue-on-error" in e for e in errors))

    def test_advisory_gate_need_not_be_in_needs(self):
        criteria = """  - id: g
    enforcement: advisory
    gates:
      - workflow: .github/workflows/post-testing-e2e.yml
        job: lifecycle"""
        workflow = ("name: x\n"
                    "on:\n"
                    "  workflow_dispatch: {}\n"
                    "jobs:\n  lifecycle:\n    runs-on: ubuntu-26.04\n")
        # lifecycle is NOT in the needs, but it is advisory, so this is green.
        self.assertEqual(self._check(criteria, workflow, ["resolve"]), [])

    def test_stale_schedule(self):
        criteria = """  - id: g
    enforcement: blocking
    freshness_sla_days: 1
    gates:
      - workflow: .github/workflows/post-testing-e2e.yml
        job: lifecycle"""
        workflow = ("name: x\n"
                    "on:\n"
                    "  schedule:\n"
                    "    - cron: '0 7 * * 0'  # weekly\n"
                    "  workflow_dispatch: {}\n"
                    "jobs:\n  lifecycle:\n    runs-on: ubuntu-26.04\n")
        errors = self._check(criteria, workflow, ["lifecycle"])
        self.assertTrue(any("freshness_sla_days" in e for e in errors))

    def test_fresh_schedule_ok(self):
        criteria = """  - id: g
    enforcement: blocking
    freshness_sla_days: 7
    gates:
      - workflow: .github/workflows/post-testing-e2e.yml
        job: lifecycle"""
        workflow = ("name: x\n"
                    "on:\n"
                    "  schedule:\n"
                    "    - cron: '0 7 * * *'  # daily\n"
                    "  workflow_dispatch: {}\n"
                    "jobs:\n  lifecycle:\n    runs-on: ubuntu-26.04\n")
        self.assertEqual(self._check(criteria, workflow, ["lifecycle"]), [])

    def test_promote_if_always_allows_failed_promotion(self):
        # A promote job that runs `if: always()` lets promotion proceed even
        # after the gate's job fails, so a dead gate would still graduate.
        criteria = """  - id: g
    enforcement: blocking
    gates:
      - workflow: .github/workflows/post-testing-e2e.yml
        job: lifecycle"""
        workflow = ("name: x\n"
                    "on:\n"
                    "  workflow_dispatch: {}\n"
                    "jobs:\n  lifecycle:\n    runs-on: ubuntu-26.04\n")
        errors = self._check(criteria, workflow, ["lifecycle"],
                             promote_if="always()")
        # A promote if: that runs always()/failure() lets promotion proceed
        # after the gate fails. The old check only caught this via "not
        # required to succeed"; now it also names the dangerous token.
        self.assertTrue(any("lets promotion proceed" in e for e in errors))
        self.assertTrue(any("not required to succeed" in e for e in errors))

    def test_promote_if_requires_gate_success(self):
        # A promote job whose if: requires the gate's job to succeed passes.
        criteria = """  - id: g
    enforcement: blocking
    gates:
      - workflow: .github/workflows/post-testing-e2e.yml
        job: lifecycle"""
        workflow = ("name: x\n"
                    "on:\n"
                    "  workflow_dispatch: {}\n"
                    "jobs:\n  lifecycle:\n    runs-on: ubuntu-26.04\n")
        self.assertEqual(
            self._check(criteria, workflow, ["lifecycle"],
                        promote_if="needs.lifecycle.result == 'success'"),
            [])

    def test_bare_cancelled_without_gate_success_is_caught(self):
        # A bare `!cancelled()` that does not also require the gate to succeed
        # lets a skipped or failed gate still promote. The token check below
        # does not flag `!cancelled()`, but the requires-success check does.
        criteria = """  - id: g
    enforcement: blocking
    gates:
      - workflow: .github/workflows/post-testing-e2e.yml
        job: lifecycle"""
        workflow = ("name: x\n"
                    "on:\n"
                    "  workflow_dispatch: {}\n"
                    "jobs:\n  lifecycle:\n    runs-on: ubuntu-26.04\n")
        errors = self._check(criteria, workflow, ["lifecycle"],
                             promote_if="!cancelled()")
        self.assertTrue(any("not required to succeed" in e for e in errors))

    def test_cancelled_with_gate_success_passes(self):
        # The shipped promote job uses `!cancelled()` alongside the gate-success
        # requirement; that combination is intentional and must stay green.
        criteria = """  - id: g
    enforcement: blocking
    gates:
      - workflow: .github/workflows/post-testing-e2e.yml
        job: lifecycle"""
        workflow = ("name: x\n"
                    "on:\n"
                    "  workflow_dispatch: {}\n"
                    "jobs:\n  lifecycle:\n    runs-on: ubuntu-26.04\n")
        promote_if = ("!cancelled() && needs.lifecycle.result == 'success'")
        self.assertEqual(
            self._check(criteria, workflow, ["lifecycle"], promote_if=promote_if),
            [])


class CronIntervalTests(unittest.TestCase):
    def test_daily_cron_gap_is_one_day(self):
        gap = gc.max_schedule_gap_days([{"cron": "41 7 * * *"}])
        self.assertEqual(gap, 1)

    def test_weekly_cron_gap_is_seven_days(self):
        gap = gc.max_schedule_gap_days([{"cron": "0 7 * * 0"}])
        self.assertEqual(gap, 7)

    def test_monthly_first_cron_gap_is_days_in_month(self):
        # Day 1 of every month: 1 Jan and 1 Feb 2024 are 31 days apart
        # (January has 31 days).
        gap = gc.max_schedule_gap_days([{"cron": "0 0 1 * *"}])
        self.assertEqual(gap, 31)

    def test_no_schedule_returns_none(self):
        self.assertIsNone(gc.max_schedule_gap_days(None))
        self.assertIsNone(gc.max_schedule_gap_days([]))

    def test_trigger_names_handles_yaml_boolean_on(self):
        # YAML parses the bare word ``on`` as the boolean True, so the trigger
        # names live one level under the ``True`` key.
        wf = {True: {"workflow_dispatch": {}}}
        self.assertEqual(gc.active_triggers(gc.workflow_on(wf)),
                         ["workflow_dispatch"])


if __name__ == "__main__":
    unittest.main()
