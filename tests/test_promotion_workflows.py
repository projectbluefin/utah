"""The workflows must call the promotion script the way the script expects.

scripts/promotion-ratchet.py is tested against a fake registry in
test_promotion_ratchet.py. That proves the decision, not the plumbing: a step
that stops passing SOURCE_SHA, a copy that no longer waits for the decision, or
a release that retags `:testing` again after the gate verified something else
would all leave those tests green. These tests read the workflow YAML and drive
the script with exactly the environment each step declares.
"""
import contextlib
import io
import json
import os
import re
import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_promotion_ratchet as fixtures  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
E2E = yaml.safe_load((ROOT / ".github/workflows/post-testing-e2e.yml").read_text())
RELEASE = yaml.safe_load((ROOT / ".github/workflows/execute-release.yml").read_text())
SCRIPT = "scripts/promotion-ratchet.py"


def steps_running(job, command):
    return [s for s in job["steps"] if command in s.get("run", "")]


def step_named(job, name):
    return next(s for s in job["steps"] if s.get("name") == name)


class PromoteToTestingTests(unittest.TestCase):
    job = E2E["jobs"]["promote-to-testing"]

    def test_the_tip_equality_check_is_gone(self):
        text = (ROOT / ".github/workflows/post-testing-e2e.yml").read_text()
        self.assertNotIn("commits/testing", text)
        self.assertNotIn("Superseded build", text)

    def test_evidence_is_recorded_then_decided_then_copied_in_that_order(self):
        names = [s.get("name", "") for s in self.job["steps"]]
        record = next(i for i, n in enumerate(names) if n.startswith("Record E2E evidence"))
        decide = next(i for i, n in enumerate(names) if n.startswith("Decide whether"))
        move = next(i for i, n in enumerate(names) if n.startswith("Move :testing"))
        self.assertLess(record, decide)
        self.assertLess(decide, move)

    def test_tags_move_only_when_the_ratchet_says_promote(self):
        move = step_named(self.job, "Move :testing to the tested digests")
        self.assertIn("steps.ratchet.outputs.action == 'promote'", move["if"])
        self.assertEqual(step_named(self.job, "Decide whether the tested commit may advance :testing")["id"], "ratchet")
        self.assertIn("skopeo copy --all --preserve-digests", move["run"])

    def test_nothing_is_written_from_a_ref_other_than_main(self):
        for step in self.job["steps"]:
            if step.get("name") == "Explain why nothing is published":
                self.assertEqual(step["if"], "github.ref != 'refs/heads/main'")
            else:
                self.assertIn("github.ref == 'refs/heads/main'", step["if"], step)

    def test_the_decision_code_comes_from_the_workflow_ref_not_the_tested_commit(self):
        """The tested commit may predate the script; the ratchet must be today's."""
        checkout = next(s for s in self.job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@"))
        self.assertNotIn("ref", checkout.get("with", {}))

    def test_the_job_can_write_statuses_and_stays_behind_the_full_matrix(self):
        self.assertEqual(self.job["permissions"]["statuses"], "write")
        self.assertEqual(set(self.job["needs"]), {"resolve", "luks", "production-iso"})
        self.assertNotIn("if", self.job)

    def test_promotions_are_serialised_by_the_workflow_concurrency_group(self):
        self.assertEqual(E2E["concurrency"]["group"], "utah-iso-e2e")
        self.assertFalse(E2E["concurrency"]["cancel-in-progress"])

    def test_this_job_is_the_only_writer_of_the_testing_tag(self):
        writers = []
        for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
            if re.search(r"skopeo copy[^\n]*:testing", path.read_text()):
                writers.append(path.name)
        self.assertEqual(writers, ["post-testing-e2e.yml"])


class ReleaseGateWiringTests(unittest.TestCase):
    gate = RELEASE["jobs"]["e2e-evidence"]
    execute = RELEASE["jobs"]["execute"]

    def test_the_release_waits_for_the_gate_and_retags_what_it_verified(self):
        self.assertIn("e2e-evidence", self.execute["needs"])
        self.assertEqual(self.execute["with"]["variants"], "${{ needs.e2e-evidence.outputs.variants }}")
        self.assertEqual(self.gate["outputs"]["variants"], "${{ steps.gate.outputs.variants }}")
        self.assertEqual(self.gate["if"], self.execute["if"])

    def test_the_gate_can_read_the_registry_and_statuses_and_nothing_more(self):
        self.assertEqual(self.gate["permissions"], {"contents": "read", "packages": "read", "statuses": "read"})

    def test_the_gate_is_the_script_and_never_the_branch_tip(self):
        text = (ROOT / ".github/workflows/execute-release.yml").read_text()
        self.assertIn(f"python3 {SCRIPT} release-gate", text)
        self.assertNotIn("commits/testing", text)
        self.assertNotIn("heads/testing", text)

    def test_the_release_gate_testsuite_pin_is_unchanged(self):
        self.assertEqual(self.execute["with"]["gate_suites"], "smoke,common")
        self.assertTrue(self.execute["with"]["run_release_gate"])


class EnvironmentContractTests(unittest.TestCase):
    """Run the script with exactly the env each workflow step declares."""

    def expand(self, value, matrix):
        replacements = {
            "${{ github.token }}": "token",
            "${{ github.repository }}": fixtures.REPO,
            "${{ needs.resolve.outputs.sha }}": fixtures.sha("tested"),
            "${{ needs.resolve.outputs.matrix }}": json.dumps(matrix),
            "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}":
                "https://github.com/projectbluefin/utah/actions/runs/1",
            "ghcr.io/${{ github.repository_owner }}": fixtures.PREFIX,
            "${{ needs.variants.outputs.list }}": json.dumps(
                [{"image": i, "source_tag": "testing", "target_tag": "stable"} for i in fixtures.FLAVORS]),
        }
        self.assertIn(value, replacements, f"unmodelled expression in workflow env: {value}")
        return replacements[value]

    def run_step(self, step, world, matrix):
        command = re.search(r"promotion-ratchet\.py (\S+)", step["run"]).group(1)
        env = {k: self.expand(v, matrix) for k, v in step["env"].items()}
        saved = dict(os.environ)
        try:
            os.environ.update(env)
            os.environ.pop("GITHUB_OUTPUT", None)
            with contextlib.redirect_stdout(io.StringIO()):
                return fixtures.ratchet.main([command], runner=world)
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_record_and_decide_steps_receive_everything_the_script_reads(self):
        job = E2E["jobs"]["promote-to-testing"]
        world = fixtures.world_with_history()
        fixtures.publish_promoted(world, "promoted")
        matrix = fixtures.tested_matrix(world, "tested")
        record = steps_running(job, f"{SCRIPT} record")[0]
        decide = steps_running(job, f"{SCRIPT} decide")[0]
        self.assertEqual(self.run_step(record, world, matrix), 0)
        self.assertEqual(len(world.posted), len(fixtures.FLAVORS) + 1)
        self.assertEqual(self.run_step(decide, world, matrix), 0)

    def test_release_gate_step_receives_everything_the_script_reads(self):
        world = fixtures.world_with_history()
        matrix = fixtures.tested_matrix(world, "tested")
        fixtures.ratchet.record(matrix, fixtures.sha("tested"), "https://example/run/1",
                                fixtures.ratchet.Registry(world), fixtures.ratchet.GitHub(fixtures.REPO, world))
        for entry in matrix["include"]:
            world.promote(entry["image"], entry["digest"])
        step = next(s for s in RELEASE["jobs"]["e2e-evidence"]["steps"] if s.get("id") == "gate")
        self.assertEqual(self.run_step(step, world, matrix), 0)


class ScheduledReleaseTests(unittest.TestCase):
    """The daily stable check releases through the same gate as any other release."""

    def test_a_daily_schedule_triggers_the_release_workflow(self):
        triggers = RELEASE.get("on", RELEASE.get(True))
        crons = [entry["cron"] for entry in triggers["schedule"]]
        self.assertEqual(len(crons), 1)
        minute, hour, dom, month, dow = crons[0].split()
        # Once a day: scripts/release-trigger.py decides whether :stable is
        # over a week old, so the cron itself is no longer the cadence.
        self.assertEqual((dom, month, dow), ("*", "*", "*"))
        self.assertRegex(minute + hour, r"^\d+$")

    def test_the_scheduled_run_still_passes_through_the_evidence_gate(self):
        # A schedule must not become a way around the gate: the trigger check
        # feeds the same e2e-evidence job, which feeds the release.
        self.assertEqual(RELEASE["jobs"]["e2e-evidence"]["needs"], ["check-trigger", "variants"])
        self.assertIn("e2e-evidence", RELEASE["jobs"]["execute"]["needs"])

    def test_releases_are_serialized_and_never_cancelled_mid_retag(self):
        self.assertEqual(RELEASE["concurrency"]["group"], "utah-stable-release")
        self.assertIs(RELEASE["concurrency"]["cancel-in-progress"], False)


if __name__ == "__main__":
    unittest.main()
