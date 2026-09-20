"""A skipped stable release must be legible, and a promotion must be recognised.

execute-release.yml decided whether a push to main was the promotion by matching
the head commit subject against "chore: promote testing to main". The promotion
branch carries exactly that subject, so a squash of the single-commit promotion
PR inherits it -- but a merge commit produces

    Merge pull request #181 from projectbluefin/auto/promote-testing-to-main

which does not match. And the promotion PR's own footer recommends
`gh pr merge --merge --admin`, i.e. the case that fails.

The failure was invisible: check-trigger succeeded, execute was skipped, the
workflow reported green, main advanced and no :stable tag moved -- identical to
what an ordinary merge to main looks like. Only checking whether the tag had
actually moved would reveal it.

These tests pin both halves of the fix: a commit from the promotion branch is a
promotion whatever its subject, and every outcome annotates itself. They also
pin the trust boundary: head.ref is attacker-chosen on a fork pull request, so
only branches in this repository count as the promotion branch.
"""
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/release-trigger.py"

spec = importlib.util.spec_from_file_location("release_trigger", SCRIPT)
release_trigger = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_trigger)

BRANCH = release_trigger.PROMOTION_BRANCH
PREFIX = release_trigger.PROMOTION_PREFIX


class DecideTests(unittest.TestCase):
    def test_the_promotion_subject_is_recognised(self):
        ok, notes = release_trigger.decide("push", PREFIX, [])
        self.assertTrue(ok)
        self.assertTrue(any("::notice" in n for n in notes))

    def test_a_subject_with_the_pr_number_appended_still_matches(self):
        # GitHub's squash UI commonly appends " (#181)".
        ok, _ = release_trigger.decide("push", f"{PREFIX} (#181)", [])
        self.assertTrue(ok)

    def test_a_merge_commit_is_recognised_by_branch_and_warns(self):
        """The case that motivated this: subject does not match, but it is real."""
        subject = f"Merge pull request #181 from projectbluefin/{BRANCH}"
        ok, notes = release_trigger.decide("push", subject, [BRANCH])
        self.assertTrue(ok, "a merge of the promotion PR is still a promotion")
        joined = "\n".join(notes)
        self.assertIn("::warning", joined)
        # The warning has to name the remedy, not merely the symptom.
        self.assertIn("squash", joined)

    def test_an_ordinary_merge_is_not_a_promotion_and_says_so(self):
        ok, notes = release_trigger.decide(
            "push", "fix: enable the btrfs kernel module (#158)", ["fix/ogc-kernel-btrfs"]
        )
        self.assertFalse(ok)
        joined = "\n".join(notes)
        # The whole point: the skip explains itself rather than being a bare
        # skipped job that looks the same as a failed promotion.
        self.assertIn("::notice", joined)
        self.assertIn("No :stable tag was moved", joined)

    def test_a_manual_dispatch_is_always_a_promotion(self):
        ok, notes = release_trigger.decide("workflow_dispatch", "", [])
        self.assertTrue(ok)
        self.assertIn("::notice", "\n".join(notes))

    def test_only_the_first_line_of_the_message_is_read(self):
        # A commit body mentioning the phrase must not trigger a release.
        message = "fix: something\n\nThis reverts chore: promote testing to main\n"
        ok, _ = release_trigger.decide("push", message, [])
        self.assertFalse(ok, "the subject decides, not the body")

    def test_every_outcome_produces_at_least_one_annotation(self):
        cases = [
            ("push", PREFIX, []),
            ("push", "Merge pull request #1 from x/" + BRANCH, [BRANCH]),
            ("push", "chore: unrelated", []),
            ("workflow_dispatch", "", []),
        ]
        for event, message, refs in cases:
            with self.subTest(message=message[:40]):
                _, notes = release_trigger.decide(event, message, refs)
                self.assertTrue(notes, "a silent outcome is the bug being fixed")


class HeadRefLookupTests(unittest.TestCase):
    REPO = "projectbluefin/utah"

    def lookup(self, stdout, returncode=0):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, returncode, stdout, "")

        refs = release_trigger.pull_request_head_refs(
            "deadbeef", self.REPO, runner=runner
        )
        return refs, calls

    def test_the_head_refs_come_from_the_commit_s_pull_requests(self):
        refs, calls = self.lookup(f"{self.REPO}\t{BRANCH}\n{self.REPO}\tother\n")
        self.assertEqual(refs, [BRANCH, "other"])
        self.assertIn(f"repos/{self.REPO}/commits/deadbeef/pulls", calls[0])

    def test_a_fork_branch_with_the_promotion_name_is_ignored(self):
        """head.ref is attacker-chosen on a fork; only this repo's branches count."""
        refs, _ = self.lookup(f"attacker/utah\t{BRANCH}\n")
        self.assertEqual(refs, [])
        ok, notes = release_trigger.decide(
            "push", f"Merge pull request #9 from attacker/{BRANCH}", refs
        )
        self.assertFalse(ok, "a fork must not be able to cut a :stable release")
        self.assertIn("No :stable tag was moved", "\n".join(notes))

    def test_a_deleted_fork_reporting_no_repo_is_ignored(self):
        refs, _ = self.lookup(f"\t{BRANCH}\n")
        self.assertEqual(refs, [])

    def test_the_real_promotion_branch_survives_the_fork_filter(self):
        refs, _ = self.lookup(
            f"attacker/utah\t{BRANCH}\n{self.REPO}\t{BRANCH}\n"
        )
        self.assertEqual(refs, [BRANCH])
        ok, _ = release_trigger.decide(
            "push", f"Merge pull request #181 from {self.REPO}/{BRANCH}", refs
        )
        self.assertTrue(ok)

    def test_a_failed_lookup_is_not_a_promotion_rather_than_a_crash(self):
        """No token, rate limit, or a commit with no PR must not break the run."""
        refs, _ = self.lookup("", returncode=1)
        self.assertEqual(refs, [])
        ok, notes = release_trigger.decide("push", "fix: whatever", refs)
        self.assertFalse(ok)
        self.assertIn("::notice", "\n".join(notes))


class EndToEndTests(unittest.TestCase):
    def run_script(self, **env_extra):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "github_output"
            out.touch()
            env = dict(os.environ, GITHUB_OUTPUT=str(out), SHA="", REPOSITORY="",
                       **env_extra)
            result = subprocess.run(
                ["python3", str(SCRIPT)], capture_output=True, text=True, env=env
            )
            return result, out.read_text()

    def test_the_promotion_subject_writes_true_to_github_output(self):
        result, written = self.run_script(EVENT="push", MESSAGE=PREFIX)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("is-promotion=true", written)

    def test_an_ordinary_push_writes_false_to_github_output(self):
        result, written = self.run_script(EVENT="push", MESSAGE="fix: a thing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("is-promotion=false", written)
        self.assertIn("No :stable tag was moved", result.stdout)


class WorkflowWiringTests(unittest.TestCase):
    WORKFLOW = ROOT / ".github/workflows/execute-release.yml"

    def setUp(self):
        import yaml
        self.doc = yaml.safe_load(self.WORKFLOW.read_text())

    def test_check_trigger_runs_the_script_and_not_an_inline_regex(self):
        steps = self.doc["jobs"]["check-trigger"]["steps"]
        runs = " ".join(s.get("run", "") for s in steps)
        self.assertIn("scripts/release-trigger.py", runs)
        # The inline regex is what hid the skip; it must not come back.
        self.assertNotIn("=~ ^chore", runs)

    def test_the_lookup_has_the_permission_and_token_it_needs(self):
        job = self.doc["jobs"]["check-trigger"]
        self.assertEqual(job["permissions"].get("pull-requests"), "read")
        step = next(s for s in job["steps"] if "release-trigger.py" in s.get("run", ""))
        for name in ("GH_TOKEN", "EVENT", "MESSAGE", "SHA", "REPOSITORY"):
            with self.subTest(env=name):
                self.assertIn(name, step["env"])

    def test_execute_still_gates_on_the_output(self):
        self.assertEqual(
            self.doc["jobs"]["execute"]["if"],
            "needs.check-trigger.outputs.is-promotion == 'true'",
        )


if __name__ == "__main__":
    unittest.main()
