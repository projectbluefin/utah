"""testing-freshness.py must judge :testing by when it last moved, not by image dates.

The alarm exists because :testing froze for a week with every build green. It
must find the last run whose promotion step actually moved the tag (a green
run that skipped the move does not count), call a frozen tag stale, and treat
"no promotion found" as stale rather than fresh. The gh plumbing is exercised
by the workflow; these drive the decision with recorded job shapes, and
MainTests drives main() against a fake gh to pin what it opens, refreshes,
closes, and never touches in --dry-run.
"""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("testing_freshness", ROOT / "scripts/testing-freshness.py")
freshness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(freshness)

NOW = dt.datetime(2026, 11, 6, 17, 0, tzinfo=dt.timezone.utc)
MAX_AGE = dt.timedelta(hours=36)


def promote_job(move="success", job="success", at="2026-10-30T10:49:00Z"):
    return {"name": "promote-to-testing", "conclusion": job, "steps": [
        {"name": "Decide whether the tested commit may advance :testing", "conclusion": "success",
         "completed_at": at},
        {"name": "Move :testing to the tested digests", "conclusion": move, "completed_at": at},
    ]}


def run(run_id, status="completed"):
    return {"id": run_id, "status": status, "html_url": f"https://example/{run_id}",
            "created_at": "2026-11-06T00:00:00Z", "conclusion": "failure"}


class LastPromotionTests(unittest.TestCase):
    def test_finds_the_newest_run_that_moved_the_tag(self):
        jobs = {
            3: [{"name": "luks (utah)", "conclusion": "failure"}],
            2: [promote_job(move="skipped")],  # green, but the ratchet held the tag
            1: [promote_job(at="2026-10-30T10:49:00Z")],
        }
        when, found = freshness.last_promotion([run(3), run(2), run(1)], jobs.__getitem__)
        self.assertEqual(found["id"], 1)
        self.assertEqual(when, dt.datetime(2026, 10, 30, 10, 49, tzinfo=dt.timezone.utc))

    def test_in_progress_runs_are_skipped(self):
        jobs = {2: [promote_job(at="2026-11-06T16:00:00Z")], 1: [promote_job()]}
        _, found = freshness.last_promotion([run(2, "in_progress"), run(1)], jobs.__getitem__)
        self.assertEqual(found["id"], 1)

    def test_no_promotion_in_the_window_is_reported_as_none(self):
        jobs = {1: [{"name": "verify-provenance (utah)", "conclusion": "failure"}]}
        self.assertEqual(freshness.last_promotion([run(1)], jobs.__getitem__), (None, None))


    def test_the_move_step_time_wins_over_run_dates_and_conclusion(self):
        # The run failed a later job and started days earlier; the tag still moved when the step ended.
        jobs = {1: [promote_job(at="2026-11-06T09:15:00Z"), {"name": "testsuite", "conclusion": "failure"}]}
        when, _ = freshness.last_promotion([run(1)], jobs.__getitem__)
        self.assertEqual(when, dt.datetime(2026, 11, 6, 9, 15, tzinfo=dt.timezone.utc))

    def test_a_failed_promote_job_does_not_count(self):
        jobs = {1: [promote_job(job="failure")]}
        self.assertEqual(freshness.last_promotion([run(1)], jobs.__getitem__), (None, None))


class SignalSourceTests(unittest.TestCase):
    """The names the script looks for must exist in the workflow that moves :testing.

    A renamed job or step would otherwise read as "no promotion found" forever.
    """

    def test_workflow_still_has_the_promote_job_and_move_step(self):
        workflow = (ROOT / ".github/workflows" / freshness.WORKFLOW).read_text()
        self.assertIn(f"\n  {freshness.PROMOTE_JOB}:\n", workflow)
        self.assertIn(f"- name: {freshness.MOVE_STEP}\n", workflow)
        self.assertIn("docker://${ref%@*}:testing", workflow)


class StaleTests(unittest.TestCase):
    def test_threshold_edges(self):
        self.assertFalse(freshness.is_stale(NOW - dt.timedelta(hours=35, minutes=59), NOW, MAX_AGE))
        self.assertTrue(freshness.is_stale(NOW - dt.timedelta(hours=36, minutes=1), NOW, MAX_AGE))

    def test_the_week_long_freeze_is_stale(self):
        self.assertTrue(freshness.is_stale(freshness.parse_time("2026-10-30T10:49:00Z"), NOW, MAX_AGE))

    def test_no_promotion_found_is_stale_not_fresh(self):
        self.assertTrue(freshness.is_stale(None, NOW, MAX_AGE))


class BodyTests(unittest.TestCase):
    def test_body_carries_the_marker_and_the_failed_jobs(self):
        describe = freshness.describe_latest(run(9), [{"name": "luks (utah)", "conclusion": "failure"}])
        since = NOW - MAX_AGE - freshness.RUN_SLACK
        text = freshness.body(None, None, since, NOW, 36, describe)
        self.assertTrue(text.startswith(freshness.MARKER))
        self.assertIn("- `luks (utah)`", text)
        self.assertIn("started since 2026-11-04 05:00 UTC moved the tag", text)


class IssueLookupTests(unittest.TestCase):
    def test_finds_the_issue_by_its_body_marker(self):
        issues = [{"number": 7, "body": "unrelated"}, {"number": 9, "body": None},
                  {"number": 12, "body": freshness.MARKER + "\nstale"}]
        self.assertEqual(freshness.marked_issue(issues), 12)

    def test_no_marked_issue(self):
        self.assertIsNone(freshness.marked_issue([{"number": 7, "body": "unrelated"}]))


REPO = "example/utah"


class FakeGh:
    """Stands in for gh_text: answers the three API reads, records every issue write."""

    def __init__(self, runs, jobs, issues=()):
        self.runs, self.jobs, self.issues = runs, jobs, list(issues)
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "issue":
            return "https://github.com/example/utah/issues/99\n"
        url = args[2] if args[1] == "--paginate" else args[1]
        if "/actions/workflows/" in url:
            return "".join(json.dumps(r) + "\n" for r in self.runs)
        if url.endswith("/jobs?per_page=100"):
            run_id = int(url.split("/runs/")[1].split("/")[0])
            return json.dumps({"jobs": self.jobs[run_id]})
        if "/issues?state=open" in url:
            return "".join(json.dumps(i) + "\n" for i in self.issues)
        raise AssertionError(f"unexpected gh call: {args}")

    def writes(self):
        return [c for c in self.calls if c[0] == "issue"]

    def reads(self, fragment):
        return [c for c in self.calls if c[0] == "api" and any(fragment in a for a in c)]


def hours_ago(hours):
    when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class MainTests(unittest.TestCase):
    """main() decides whether to open, refresh, close or leave the alarm issue."""

    def main(self, fake, *argv):
        out = io.StringIO()
        with mock.patch.object(freshness, "gh_text", fake), \
                mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": REPO}), \
                contextlib.redirect_stdout(out):
            code = freshness.main(list(argv))
        return code, out.getvalue()

    def stale(self, issues=()):
        jobs = {2: [{"name": "luks (utah)", "conclusion": "failure"}],
                1: [promote_job(at=hours_ago(72))]}
        return FakeGh([run(2), run(1)], jobs, issues)

    def fresh(self, issues=()):
        return FakeGh([run(1)], {1: [promote_job(at=hours_ago(2))]}, issues)

    def marked(self, number=41):
        return {"number": number, "body": freshness.MARKER + "\nold text"}

    def test_fresh_tag_and_no_issue_writes_nothing(self):
        fake = self.fresh()
        code, out = self.main(fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])
        self.assertIn("fresh (max age 36 h)", out)

    def test_fresh_tag_closes_the_open_alarm(self):
        fake = self.fresh([{"number": 7, "body": "unrelated"}, self.marked(41)])
        self.main(fake)
        (close,) = fake.writes()
        self.assertEqual(close[:5], ("issue", "close", "41", "-R", REPO))
        self.assertIn("--reason", close)
        self.assertEqual(close[close.index("--reason") + 1], "completed")
        self.assertIn("moved again", close[close.index("--comment") + 1])

    def test_stale_tag_opens_one_labelled_issue(self):
        fake = self.stale()
        self.main(fake)
        (create,) = fake.writes()
        self.assertEqual(create[:4], ("issue", "create", "-R", REPO))
        self.assertEqual(create[create.index("--title") + 1], ":testing has not advanced for over 36 hours")
        labels = [create[i + 1] for i, a in enumerate(create) if a == "--label"]
        self.assertEqual(labels, freshness.LABELS)
        text = create[create.index("--body") + 1]
        self.assertTrue(text.startswith(freshness.MARKER))
        self.assertIn("- `luks (utah)`", text)  # failed jobs of the newest run, not the promoted one

    def test_stale_tag_refreshes_the_open_alarm_instead_of_opening_another(self):
        fake = self.stale([self.marked(41)])
        self.main(fake)
        (edit,) = fake.writes()
        self.assertEqual(edit[:5], ("issue", "edit", "41", "-R", REPO))
        self.assertTrue(edit[edit.index("--body") + 1].startswith(freshness.MARKER))

    def test_unmarked_open_ci_issues_are_not_mistaken_for_the_alarm(self):
        fake = self.stale([{"number": 7, "body": "some other area/ci bug"}])
        self.main(fake)
        self.assertEqual([w[1] for w in fake.writes()], ["create"])

    def test_no_runs_in_the_window_is_stale(self):
        fake = FakeGh([], {})
        self.main(fake)
        (create,) = fake.writes()
        self.assertIn("No post-testing-e2e run started in that time.", create[create.index("--body") + 1])

    def test_dry_run_never_reads_or_writes_issues(self):
        for fake in (self.stale([self.marked()]), self.fresh([self.marked()])):
            code, _ = self.main(fake, "--dry-run")
            self.assertEqual(code, 0)
            self.assertEqual(fake.writes(), [])
            self.assertEqual(fake.reads("/issues?"), [])

    def test_dry_run_prints_the_body_it_would_post(self):
        _, out = self.main(self.stale(), "--dry-run")
        self.assertTrue(out.startswith(freshness.MARKER))

    def test_max_age_hours_sets_the_threshold_and_the_title(self):
        jobs = {1: [promote_job(at=hours_ago(20))]}
        fake = FakeGh([run(1)], jobs)
        self.main(fake)  # 20 h is fresh under the 36 h default
        self.assertEqual(fake.writes(), [])
        fake = FakeGh([run(1)], jobs)
        self.main(fake, "--max-age-hours", "12")
        (create,) = fake.writes()
        self.assertEqual(create[create.index("--title") + 1], ":testing has not advanced for over 12 hours")

    def test_runs_are_scanned_back_past_the_threshold_by_the_slack(self):
        fake = self.fresh()
        before = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        self.main(fake, "--max-age-hours", "10")
        after = dt.datetime.now(dt.timezone.utc)
        (listing,) = fake.reads("/actions/workflows/")
        url = next(a for a in listing if "created=" in a)
        since = freshness.parse_time(url.split("created=%3E%3D")[1])
        window = dt.timedelta(hours=10) + freshness.RUN_SLACK
        self.assertLessEqual(before - window - dt.timedelta(seconds=1), since)
        self.assertLessEqual(since, after - window)
        self.assertIn(f"/{freshness.WORKFLOW}/", url)

    def test_jobs_are_fetched_once_per_run(self):
        # Run 1 is both the newest run and the promoted one; describe_latest must reuse its jobs.
        fake = FakeGh([run(1)], {1: [promote_job(at=hours_ago(72))]})
        self.main(fake)
        self.assertEqual(len(fake.reads("/runs/1/jobs")), 1)


class GhPlumbingTests(unittest.TestCase):
    """A failed gh call must stop the check, never read as "nothing found"."""

    def completed(self, returncode, stdout="", stderr=""):
        return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_a_failed_gh_call_raises_with_its_stderr(self):
        with mock.patch.object(freshness.subprocess, "run",
                               return_value=self.completed(1, stderr="gh: Server Error (HTTP 502)")):
            with self.assertRaisesRegex(RuntimeError, "HTTP 502"):
                freshness.gh("api", "repos/x/actions/runs/1/jobs")

    def test_empty_output_is_none_and_json_is_parsed(self):
        with mock.patch.object(freshness.subprocess, "run", return_value=self.completed(0, "  \n")):
            self.assertIsNone(freshness.gh("api", "x"))
        with mock.patch.object(freshness.subprocess, "run", return_value=self.completed(0, '{"jobs": []}')):
            self.assertEqual(freshness.gh("api", "x"), {"jobs": []})

    def test_paginated_jq_lines_are_parsed_one_object_per_line(self):
        self.assertEqual(freshness.json_lines('{"a": 1}\n\n{"a": 2}\n'), [{"a": 1}, {"a": 2}])


if __name__ == "__main__":
    unittest.main()
