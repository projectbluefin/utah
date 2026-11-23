"""Tests for scripts/detect-dead-gates.py.

The dead-gate detector asks the GitHub API what the latest run of each gate's
job did, and reports the gates that are dead. The pure logic is classify_gate:
it turns a latest-run summary into a state (healthy, failed, stale, never).
These tests cover each state and the freshness window.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import unittest
from datetime import timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "detect_dead_gates", ROOT / "scripts" / "detect-dead-gates.py")
detect_dead_gates = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(detect_dead_gates)
ddg = detect_dead_gates


def summary(has_run, conclusion, age_seconds=None):
    if age_seconds is None:
        last_date = None if not has_run else dt.datetime.now(timezone.utc)
    else:
        last_date = (dt.datetime.now(timezone.utc)
                     - dt.timedelta(seconds=age_seconds))
    return {
        "has_run": has_run,
        "last_conclusion": conclusion if has_run else None,
        "last_date": last_date,
    }


class ClassifyGateTests(unittest.TestCase):
    def test_healthy(self):
        cls = ddg.classify_gate(summary(True, "success"), sla_days=2)
        self.assertEqual(cls["state"], "healthy")

    def test_failed(self):
        cls = ddg.classify_gate(summary(True, "failure"), sla_days=2)
        self.assertEqual(cls["state"], "failed")

    def test_never_ran(self):
        cls = ddg.classify_gate(summary(False, None), sla_days=2)
        self.assertEqual(cls["state"], "never")

    def test_stale_when_older_than_sla(self):
        cls = ddg.classify_gate(summary(True, "success",
                                        age_seconds=3 * 86400),
                                sla_days=2)
        self.assertEqual(cls["state"], "stale")

    def test_not_stale_within_sla(self):
        cls = ddg.classify_gate(summary(True, "success",
                                        age_seconds=86400),
                                sla_days=2)
        self.assertEqual(cls["state"], "healthy")

    def test_failure_beats_age(self):
        # A recent failure is reported as failed even though it is within the
        # freshness window.
        cls = ddg.classify_gate(summary(True, "failure",
                                        age_seconds=3600),
                                sla_days=2)
        self.assertEqual(cls["state"], "failed")


class TapTests(unittest.TestCase):
    def test_emit_tap(self):
        reports = [
            {"id": "a", "state": "healthy", "detail": "ok"},
            {"id": "b", "state": "failed", "detail": "red"},
        ]
        lines = ddg.emit_tap(reports).splitlines()
        self.assertEqual(lines[0], "TAP version 13")
        self.assertEqual(lines[1], "1..2")
        self.assertTrue(lines[2].startswith("ok 1 - a: ok"))
        self.assertTrue(lines[3].startswith("not ok 2 - b: red"))


if __name__ == "__main__":
    unittest.main()
