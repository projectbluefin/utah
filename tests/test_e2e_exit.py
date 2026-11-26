"""The E2E harnesses' exit codes and wall-clock deadline (iso/scripts/e2e-exit.sh).

A job killed by timeout-minutes uploads nothing, and a harness that exits 1
for every failure says nothing about where it failed. The harnesses must fail
with exit 75 and their evidence before the deadline, and name the failing
phase in the exit code. The driver below follows the harnesses' own pattern:
source the library, trap cleanup on EXIT, set E2E_PHASE_EXIT per phase.
"""
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "iso/scripts/e2e-exit.sh"

DRIVER = f"""
set -euo pipefail
source {LIB}
cleanup() {{
    local status=$?
    trap '' USR1
    echo "cleanup ran" >&2
    exit "$(e2e_status "${{status}}")"
}}
trap cleanup EXIT
fail() {{ echo "FAIL: $*" >&2; exit 1; }}
deadline_reached() {{ e2e_exit "${{E2E_EXIT_DEADLINE}}" "deadline reached in ${{E2E_PHASE_EXIT}}"; }}
e2e_deadline_start deadline_reached
E2E_PHASE_EXIT=13
"""


def drive(body: str, **env) -> tuple[subprocess.CompletedProcess, float]:
    start = time.monotonic()
    result = subprocess.run(["bash", "-c", DRIVER + body], capture_output=True, text=True,
                            env={**os.environ, **env}, timeout=60)
    return result, time.monotonic() - start


class ExitCodeTests(unittest.TestCase):
    def test_a_fail_call_reports_the_phase(self):
        result, _ = drive('fail "no SSH"')
        self.assertEqual(result.returncode, 13, result.stderr)
        self.assertIn("cleanup ran", result.stderr)

    def test_a_bare_set_e_failure_reports_the_phase_too(self):
        result, _ = drive("false")
        self.assertEqual(result.returncode, 13, result.stderr)

    def test_an_explicit_code_is_kept(self):
        result, _ = drive('bash -c "exit 16" || e2e_exit "$?" "nested harness failed"')
        self.assertEqual(result.returncode, 16, result.stderr)

    def test_success_stays_zero(self):
        result, _ = drive("true")
        self.assertEqual(result.returncode, 0, result.stderr)


class DeadlineTests(unittest.TestCase):
    def test_a_blocked_harness_fails_with_75_at_the_deadline(self):
        # The foreground command would outlive the job; the watcher must get
        # the harness out with its cleanup (evidence) run, well before that.
        result, elapsed = drive("sleep 50", E2E_WALL_CLOCK_DEADLINE=str(int(time.time()) + 2),
                                E2E_DEADLINE_GRACE="1")
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertIn("deadline reached in 13", result.stderr)
        self.assertIn("cleanup ran", result.stderr)
        self.assertLess(elapsed, 20)

    def test_a_short_command_finishes_before_the_handler_runs(self):
        result, elapsed = drive("sleep 3; echo finished", E2E_WALL_CLOCK_DEADLINE=str(int(time.time()) + 1),
                                E2E_DEADLINE_GRACE="10")
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertNotIn("finished", result.stdout)
        self.assertLess(elapsed, 10)

    def test_a_passed_deadline_fails_at_once(self):
        result, elapsed = drive("sleep 30", E2E_WALL_CLOCK_DEADLINE=str(int(time.time()) - 1))
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertLess(elapsed, 5)

    def test_no_deadline_means_no_watcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            listing = Path(tmp) / "jobs"
            for deadline, expected in (("", ""), (str(int(time.time()) + 600), "watcher")):
                result, _ = drive(f'jobs -p > "{listing}"', E2E_WALL_CLOCK_DEADLINE=deadline)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual("watcher" if listing.read_text().strip() else "", expected)

    def test_the_watcher_does_not_hold_the_output_pipe_open(self):
        # The workflow tees the harness output; a watcher left holding stdout
        # would hang the step until the deadline.
        result, elapsed = drive("true", E2E_WALL_CLOCK_DEADLINE=str(int(time.time()) + 600))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 10)

    def test_a_malformed_deadline_is_rejected(self):
        result, _ = drive("true", E2E_WALL_CLOCK_DEADLINE="tomorrow")
        self.assertEqual(result.returncode, 1)
        self.assertIn("epoch seconds", result.stderr)


class HarnessWiringTests(unittest.TestCase):
    def wiring(self, script: str) -> str:
        text = (ROOT / "iso/scripts" / script).read_text()
        self.assertIn('source "${ROOT}/iso/scripts/e2e-exit.sh"', text)
        self.assertIn('exit "$(e2e_status "${status}")"', text)
        self.assertLess(text.index("trap cleanup EXIT"), text.index("e2e_deadline_start deadline_reached"))
        for name in ("qemu-system-x86_64 not found", "OVMF variable store not found"):
            self.assertIn(f'e2e_exit "${{E2E_EXIT_DEPENDENCY}}" "{name}"', text)
        return text

    def test_each_luks_phase_sets_its_code(self):
        text = self.wiring("luks-e2e.sh")
        for n in range(1, 8):
            self.assertRegex(text, rf"E2E_PHASE_EXIT={10 + n}\necho \"=== Phase {n}/7:")

    def test_each_lifecycle_phase_sets_its_code_and_keeps_the_install_code(self):
        text = self.wiring("lifecycle-e2e.sh")
        for n, name in enumerate(("baseline", "staged", "upgraded", "rollback", "complete"), start=1):
            self.assertIn(f'ACTIVE_PHASE="{name}"\nE2E_PHASE_EXIT={20 + n}\n', text)
        self.assertIn('|| e2e_exit "$?" "the install harness (luks-e2e.sh) failed', text)


if __name__ == "__main__":
    unittest.main()
