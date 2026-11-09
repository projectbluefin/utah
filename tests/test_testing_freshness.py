"""testing-freshness.py must judge :testing by when it last moved, not by image dates.

The alarm exists because :testing froze for a week with every build green. It
must find the last run whose promotion step actually moved the tag (a green
run that skipped the move does not count), call a frozen tag stale, and treat
"no promotion found" as stale rather than fresh. The gh plumbing is exercised
by the workflow; these drive the decision with recorded job shapes.
"""
import datetime as dt
import importlib.util
import unittest
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


if __name__ == "__main__":
    unittest.main()
