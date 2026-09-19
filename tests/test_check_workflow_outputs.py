"""Tests for scripts/check_workflow_outputs.py.

The workflow outputs check ensures that every job output referencing a step's
outputs (`steps.<id>.outputs`) points to a step id defined within that same job.
These tests verify that step ids do not leak across jobs, non-step outputs are
accepted, jobs without outputs are skipped, all dangling references are
reported, and the shipped workflows pass.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_workflow_outputs.py"


class WorkflowOutputsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def run_check(self, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=cwd or self.dir,
            capture_output=True,
            text=True,
        )

    def write_workflow(self, filename: str, content: str) -> Path:
        wf_dir = self.dir / ".github" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        target = wf_dir / filename
        target.write_text(content)
        return target

    def test_shipped_workflows_pass(self):
        result = self.run_check(cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("checked workflow job outputs: every referenced step id exists", result.stdout)

    def test_valid_step_reference_passes(self):
        self.write_workflow(
            "valid.yml",
            """name: Valid
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      artifact: ${{ steps.compile.outputs.dest }}
    steps:
      - id: compile
        run: echo "dest=bin" >> $GITHUB_OUTPUT
""",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("checked workflow job outputs: every referenced step id exists", result.stdout)

    def test_step_ids_do_not_leak_across_jobs(self):
        self.write_workflow(
            "cross_job.yml",
            """name: CrossJob
on: [push]
jobs:
  producer:
    runs-on: ubuntu-latest
    steps:
      - id: resolver
        run: echo "flavor=main" >> $GITHUB_OUTPUT
  consumer:
    runs-on: ubuntu-latest
    outputs:
      flavor: ${{ steps.resolver.outputs.flavor }}
    steps:
      - id: other_step
        run: echo "hello"
""",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            ".github/workflows/cross_job.yml: job consumer output flavor reads steps.resolver, which no step in that job defines",
            result.stderr,
        )

    def test_jobs_without_outputs_are_skipped(self):
        self.write_workflow(
            "no_outputs.yml",
            """name: NoOutputs
on: [push]
jobs:
  job1:
    runs-on: ubuntu-latest
    steps:
      - run: echo "no step id"
  job2:
    runs-on: ubuntu-latest
    outputs: {}
    steps:
      - id: step_a
        run: echo "has step id"
""",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_non_step_outputs_accepted(self):
        self.write_workflow(
            "non_step_outputs.yml",
            """name: NonStepOutputs
on: [push]
jobs:
  dispatch:
    runs-on: ubuntu-latest
    outputs:
      input_val: ${{ inputs.target_image }}
      sha: ${{ github.sha }}
      ref: ${{ github.ref_name }}
      env_val: ${{ env.MY_VARIABLE }}
      constant_str: "literal-value"
      number_val: 123
    steps:
      - run: echo "done"
""",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_dangling_reference_in_every_workflow_is_reported(self):
        self.write_workflow(
            "workflow_a.yml",
            """name: WorkflowA
on: [push]
jobs:
  job_a:
    runs-on: ubuntu-latest
    outputs:
      out_one: ${{ steps.missing_a1.outputs.val }}
      out_two: ${{ steps.missing_a2.outputs.val }}
    steps:
      - run: echo "test"
  job_b:
    runs-on: ubuntu-latest
    outputs:
      out_three: ${{ steps.missing_b.outputs.val }}
    steps:
      - run: echo "test"
""",
        )
        self.write_workflow(
            "workflow_b.yml",
            """name: WorkflowB
on: [push]
jobs:
  job_c:
    runs-on: ubuntu-latest
    outputs:
      out_four: ${{ steps.missing_c.outputs.val }}
    steps:
      - run: echo "test"
""",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("job job_a output out_one reads steps.missing_a1", result.stderr)
        self.assertIn("job job_a output out_two reads steps.missing_a2", result.stderr)
        self.assertIn("job job_b output out_three reads steps.missing_b", result.stderr)
        self.assertIn("job job_c output out_four reads steps.missing_c", result.stderr)

    def test_multiple_step_references_in_single_output_expression(self):
        self.write_workflow(
            "multi_ref.yml",
            """name: MultiRef
on: [push]
jobs:
  compose:
    runs-on: ubuntu-latest
    outputs:
      combined: "${{ steps.known.outputs.a }}-${{ steps.unknown.outputs.b }}"
    steps:
      - id: known
        run: echo "a=foo" >> $GITHUB_OUTPUT
""",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("job compose output combined reads steps.unknown", result.stderr)
        self.assertNotIn("steps.known", result.stderr)

    def test_empty_directory_or_no_workflows(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("checked workflow job outputs: every referenced step id exists", result.stdout)


if __name__ == "__main__":
    unittest.main()
