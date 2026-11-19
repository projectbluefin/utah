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

The daily schedule follows the owner's rule instead -- release when :stable is
over a week old and :testing has something new -- and ScheduledReleaseTests
drive it against a fake registry and a fake Actions API.
"""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_a_scheduled_run_is_not_judged_as_a_commit(self):
        # A scheduled run has no head commit; scheduled_release decides it from
        # the registry and the last release, so decide refuses rather than
        # calling it "not a promotion".
        with self.assertRaises(ValueError):
            release_trigger.decide("schedule", "", [])

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


NOW = dt.datetime(2026, 11, 9, 6, 2, tzinfo=dt.timezone.utc)
REGISTRY = "ghcr.io/projectbluefin"
REPO = "projectbluefin/utah"
FLAVORS = [v["image"] for v in json.loads(
    subprocess.run(["python3", str(ROOT / "scripts/flavors.py"), "releases"],
                   capture_output=True, text=True, check=True).stdout)]
VARIANTS = [{"image": i, "source_tag": "testing", "target_tag": "stable"} for i in FLAVORS]


def stamp(when: dt.datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeWorld:
    """GHCR tags plus execute-release runs, behind subprocess-shaped calls.

    Answers the real command lines the script issues (`skopeo inspect
    --no-tags --format {{.Digest}}`, `gh api --paginate .../runs`, `gh api
    graphql` with the retag query on stdin), so a wrong path, filter, check
    name or registry ref fails here, not in a scheduled run.
    """

    def __init__(self):
        self.tags: dict[str, str] = {}
        self.runs: list[dict] = []  # newest first, as the API lists them
        self.check_runs: dict[str, list[dict]] = {}  # node id -> every check run
        self.calls: list[list[str]] = []
        self.queries: list[dict] = []
        self.skopeo_error: str | None = None

    def tag(self, image: str, tag: str, digest: str) -> None:
        self.tags[f"{REGISTRY}/{image}:{tag}"] = "sha256:" + digest * 64

    def all_flavors(self, testing: str, stable: str | None) -> None:
        for image in FLAVORS:
            self.tag(image, "testing", testing)
            if stable:
                self.tag(image, "stable", stable)

    def release(self, run_id: int, retagged: dt.datetime | None, *, conclusion="SUCCESS",
                attempts: int = 1) -> None:
        """An execute-release run; `retagged` is when its retag step finished."""
        node = f"WFR_{run_id}"
        checks = [{"name": "check-trigger", "steps": [
            {"name": "Run python3 scripts/release-trigger.py", "conclusion": "SUCCESS",
             "completedAt": stamp(NOW)}]}]
        # Every attempt is a check run of its own. A later attempt that did not
        # retag (say its release gate failed) must not hide the earlier one.
        checks += [{"name": release_trigger.RETAG_JOB, "steps": []}] * (attempts - 1)
        if retagged:
            checks.insert(1, {"name": release_trigger.RETAG_JOB, "steps": [
                {"name": "Re-verify cosign signatures before promotion", "conclusion": "SUCCESS",
                 "completedAt": stamp(retagged - dt.timedelta(seconds=10))},
                {"name": release_trigger.RETAG_STEP, "conclusion": conclusion,
                 "completedAt": stamp(retagged)},
            ]})
        self.runs.append({"id": run_id, "node_id": node,
                          "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}"})
        self.check_runs[node] = checks

    def gh_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["gh", "api"]]

    def _graphql(self, body: dict) -> dict:
        self.queries.append(body)
        query, variables = body["query"], body["variables"]
        assert "checkType: ALL" in query, "a rerun's earlier attempts would be hidden"
        assert len(variables["ids"]) <= 100, "GraphQL resolves at most 100 ids"
        nodes = []
        for node in variables["ids"]:
            runs = [{"steps": {"nodes": c["steps"]}} for c in self.check_runs[node]
                    if c["name"] == variables["job"]]
            nodes.append({"databaseId": int(node.removeprefix("WFR_")),
                          "checkSuite": {"checkRuns": {"nodes": runs}}})
        return {"data": {"nodes": nodes}}

    def __call__(self, argv, input=None, **_):
        self.calls.append(argv)
        if argv[:5] == ["skopeo", "inspect", "--no-tags", "--format", "{{.Digest}}"]:
            if self.skopeo_error:
                return subprocess.CompletedProcess(argv, 1, "", self.skopeo_error)
            ref = argv[5].removeprefix("docker://")
            if ref in self.tags:
                return subprocess.CompletedProcess(argv, 0, self.tags[ref] + "\n", "")
            return subprocess.CompletedProcess(argv, 1, "", "manifest unknown")
        if argv == ["gh", "api", "graphql", "--input", "-"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self._graphql(json.loads(input))), "")
        if argv[:3] == ["gh", "api", "--paginate"] and argv[3].startswith(
                f"repos/{REPO}/actions/workflows/execute-release.yml/runs?"):
            out = "".join(json.dumps(run) + "\n" for run in self.runs)
            return subprocess.CompletedProcess(argv, 0, out, "")
        raise AssertionError(f"unexpected command {argv}")


def run_main(world: FakeWorld, event: str = "schedule", message: str = "", run_id: str = "999"):
    """Drive main() as check-trigger does; returns (rc, stdout, GITHUB_OUTPUT)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "github_output"
        out.touch()
        env = {"GITHUB_OUTPUT": str(out), "EVENT": event, "MESSAGE": message, "SHA": "",
               "REPOSITORY": REPO, "REGISTRY": REGISTRY, "VARIANTS": json.dumps(VARIANTS),
               "GITHUB_RUN_ID": run_id}
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(stdout):
            rc = release_trigger.main([], runner=world, now=NOW)
        return rc, stdout.getvalue(), out.read_text()


class ScheduledReleaseTests(unittest.TestCase):
    """The daily schedule releases only when :stable is over a week old and
    :testing points at something new; every other day it says why not."""

    def test_the_release_set_is_the_four_flavors(self):
        self.assertEqual(len(FLAVORS), 4)

    def test_stale_stable_and_new_testing_releases(self):
        world = FakeWorld()
        world.all_flavors(testing="b", stable="a")
        world.release(101, NOW - dt.timedelta(days=7, hours=1))
        rc, stdout, written = run_main(world)
        self.assertEqual(rc, 0)
        self.assertIn("is-promotion=true", written)
        self.assertIn("::notice title=stable release::", stdout)
        self.assertIn("7.0 days ago", stdout)
        self.assertIn("actions/runs/101", stdout)

    def test_fresh_stable_releases_nothing_and_says_why(self):
        world = FakeWorld()
        world.all_flavors(testing="b", stable="a")
        world.release(101, NOW - dt.timedelta(days=2))
        rc, stdout, written = run_main(world)
        self.assertEqual(rc, 0)
        self.assertIn("is-promotion=false", written)
        self.assertIn("::notice title=no stable release::", stdout)
        self.assertIn(":stable is 2.0 days old, not over 7 days", stdout)
        self.assertIn("2026-11-14 06:02 UTC", stdout, "the notice says when it will release")
        self.assertIn("No :stable tag was moved", stdout)

    def test_exactly_a_week_is_not_over_a_week(self):
        world = FakeWorld()
        world.all_flavors(testing="b", stable="a")
        world.release(101, NOW - dt.timedelta(days=7))
        _, _, written = run_main(world)
        self.assertIn("is-promotion=false", written)

    def test_stale_stable_with_nothing_new_releases_nothing_and_says_why(self):
        world = FakeWorld()
        world.all_flavors(testing="a", stable="a")
        world.release(101, NOW - dt.timedelta(days=30))
        rc, stdout, written = run_main(world)
        self.assertEqual(rc, 0)
        self.assertIn("is-promotion=false", written)
        self.assertIn("::notice title=no stable release::Nothing new", stdout)
        self.assertEqual(world.gh_calls(), [], "the registry decided it; no API scan needed")

    def test_one_flavor_with_new_testing_is_something_new(self):
        world = FakeWorld()
        world.all_flavors(testing="a", stable="a")
        world.tag(FLAVORS[-1], "testing", "b")
        rc, stdout, written = run_main(world)
        self.assertEqual(rc, 0)
        self.assertIn("is-promotion=true", written)
        self.assertIn(f"new digests for {FLAVORS[-1]}.", stdout)

    def test_missing_stable_is_stale_and_releases(self):
        world = FakeWorld()
        world.all_flavors(testing="b", stable=None)
        rc, stdout, written = run_main(world)
        self.assertEqual(rc, 0)
        self.assertIn("is-promotion=true", written)
        self.assertIn("has no :stable tag yet", stdout)
        self.assertEqual(world.gh_calls(), [])

    def test_a_flavor_without_stable_makes_the_set_stale_even_after_a_fresh_release(self):
        world = FakeWorld()
        world.all_flavors(testing="a", stable="a")
        del world.tags[f"{REGISTRY}/{FLAVORS[1]}:stable"]
        world.release(101, NOW - dt.timedelta(hours=3))
        _, stdout, written = run_main(world)
        self.assertIn("is-promotion=true", written)
        self.assertIn(f"{FLAVORS[1]} has no :stable tag yet", stdout)

    def test_no_retag_in_the_window_is_stale(self):
        world = FakeWorld()
        world.all_flavors(testing="b", stable="a")
        world.release(102, None)  # ordinary pushes: check-trigger only
        world.release(101, None)
        rc, stdout, written = run_main(world)
        self.assertEqual(rc, 0)
        self.assertIn("is-promotion=true", written)
        self.assertIn("no execute-release run retagged :stable in the last 8.0 days", stdout)

    def test_the_scan_covers_a_week_plus_slack_and_reads_every_attempt(self):
        world = FakeWorld()
        world.all_flavors(testing="b", stable="a")
        world.release(101, NOW - dt.timedelta(days=1), attempts=2)
        _, _, written = run_main(world)
        self.assertIn("is-promotion=false", written)
        runs, graphql = world.gh_calls()
        self.assertIn("created=%3E%3D2026-11-01T06:02:00Z", runs[3])
        self.assertEqual(graphql, ["gh", "api", "graphql", "--input", "-"])
        self.assertEqual(world.queries[0]["variables"]["job"], "execute / Promote release")

    def test_the_latest_retag_decides(self):
        world = FakeWorld()
        world.all_flavors(testing="b", stable="a")
        world.release(103, None)
        world.release(102, NOW - dt.timedelta(days=3))
        world.release(101, NOW - dt.timedelta(days=7, hours=12))
        _, stdout, written = run_main(world)
        self.assertIn("is-promotion=false", written)
        self.assertIn("actions/runs/102", stdout)

    def test_a_week_of_merges_is_read_in_batches_of_at_most_100_runs(self):
        world = FakeWorld()
        world.all_flavors(testing="b", stable="a")
        for run_id in range(1250, 1000, -1):  # ordinary pushes
            world.release(run_id, None)
        world.release(1000, NOW - dt.timedelta(days=6))
        _, stdout, written = run_main(world)
        self.assertIn("is-promotion=false", written)
        self.assertIn("actions/runs/1000", stdout)
        self.assertEqual([len(q["variables"]["ids"]) for q in world.queries], [100, 100, 51])

    def test_a_failed_retag_step_does_not_count_as_a_release(self):
        """The step fails if any flavor failed to move: the oldest flavor decides."""
        world = FakeWorld()
        world.all_flavors(testing="b", stable="a")
        world.release(102, NOW - dt.timedelta(days=1), conclusion="FAILURE")
        world.release(101, NOW - dt.timedelta(days=7, hours=1))
        _, stdout, written = run_main(world)
        self.assertIn("is-promotion=true", written)
        self.assertIn("actions/runs/101", stdout)

    def test_this_run_is_not_scanned(self):
        world = FakeWorld()
        world.all_flavors(testing="b", stable="a")
        world.release(999, None)
        world.release(101, None)
        run_main(world, run_id="999")
        self.assertEqual(world.queries[0]["variables"]["ids"], ["WFR_101"])

    def test_an_unresolved_run_fails_the_run_rather_than_reading_as_stale(self):
        world = FakeWorld()
        world.all_flavors(testing="b", stable="a")
        world.release(101, NOW - dt.timedelta(days=1))
        world.runs.append({"id": 100, "node_id": "WFR_100", "html_url": "x"})
        world.check_runs["WFR_100"] = []
        world._graphql = lambda body: {"data": {"nodes": [None, None]}}
        rc, stdout, written = run_main(world)
        self.assertEqual(rc, 1)
        self.assertIn("did not resolve runs", stdout)
        self.assertNotIn("is-promotion", written)

    def test_an_unreadable_registry_fails_the_run_and_releases_nothing(self):
        world = FakeWorld()
        world.all_flavors(testing="b", stable="a")
        world.skopeo_error = "unauthorized: authentication required"
        rc, stdout, written = run_main(world)
        self.assertEqual(rc, 1)
        self.assertIn("::error title=scheduled release check failed::", stdout)
        self.assertNotIn("is-promotion", written)

    def test_the_stable_image_created_time_is_never_read(self):
        """config `created` is SOURCE_DATE_EPOCH, not when :stable moved."""
        world = FakeWorld()
        world.all_flavors(testing="b", stable="a")
        world.release(101, NOW - dt.timedelta(days=2))
        run_main(world)
        skopeo = [c for c in world.calls if c[0] == "skopeo"]
        self.assertTrue(skopeo)
        self.assertTrue(all(c[:5] == ["skopeo", "inspect", "--no-tags", "--format", "{{.Digest}}"]
                            for c in skopeo))

    def test_manual_dispatch_and_promotion_merge_are_unchanged(self):
        """Neither reads the registry or the release history: they always decide as before."""
        world = FakeWorld()
        world.all_flavors(testing="a", stable="a")  # nothing new, and :stable fresh
        world.release(101, NOW - dt.timedelta(hours=1))
        for event, message in (("workflow_dispatch", ""), ("push", f"{PREFIX} (#200)")):
            with self.subTest(event=event):
                world.calls.clear()
                rc, _, written = run_main(world, event=event, message=message)
                self.assertEqual(rc, 0)
                self.assertIn("is-promotion=true", written)
                self.assertEqual(world.calls, [])


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

    def test_the_scheduled_check_gets_the_flavors_registry_and_permissions(self):
        job = self.doc["jobs"]["check-trigger"]
        self.assertEqual(job["needs"], ["variants"])
        self.assertEqual(job["permissions"].get("actions"), "read")
        self.assertEqual(job["permissions"].get("checks"), "read")
        self.assertEqual(job["permissions"].get("packages"), "read")
        step = next(s for s in job["steps"] if "release-trigger.py" in s.get("run", ""))
        self.assertEqual(step["env"]["VARIANTS"], "${{ needs.variants.outputs.list }}")
        self.assertEqual(step["env"]["REGISTRY"], "ghcr.io/${{ github.repository_owner }}")
        login = next(s for s in job["steps"] if "skopeo login" in s.get("run", ""))
        self.assertEqual(login["if"], "github.event_name == 'schedule'")

    def test_the_schedule_checks_daily(self):
        triggers = self.doc.get("on", self.doc.get(True))
        self.assertEqual([e["cron"] for e in triggers["schedule"]], ["0 6 * * *"])

    def test_the_retag_names_were_verified_against_the_pinned_reusable_release(self):
        # A rename in projectbluefin/actions would make every run read as
        # "no retag", which releases daily instead of weekly.
        uses = self.doc["jobs"]["execute"]["uses"]
        self.assertEqual(uses.rsplit("@", 1)[1], release_trigger.RETAG_NAMES_VERIFIED_AT)


if __name__ == "__main__":
    unittest.main()
