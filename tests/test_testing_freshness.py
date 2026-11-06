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
        when, found = freshness.last_promotion("r", [run(3), run(2), run(1)], jobs.__getitem__)
        self.assertEqual(found["id"], 1)
        self.assertEqual(when, dt.datetime(2026, 10, 30, 10, 49, tzinfo=dt.timezone.utc))

    def test_in_progress_runs_are_skipped(self):
        jobs = {2: [promote_job(at="2026-11-06T16:00:00Z")], 1: [promote_job()]}
        _, found = freshness.last_promotion("r", [run(2, "in_progress"), run(1)], jobs.__getitem__)
        self.assertEqual(found["id"], 1)

    def test_no_promotion_in_the_window_is_reported_as_none(self):
        jobs = {1: [{"name": "verify-provenance (utah)", "conclusion": "failure"}]}
        self.assertEqual(freshness.last_promotion("r", [run(1)], jobs.__getitem__), (None, None))


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
        latest = describe = freshness.describe_latest(run(9), [{"name": "luks (utah)", "conclusion": "failure"}])
        text = freshness.body(None, None, NOW, 36, describe)
        self.assertTrue(text.startswith(freshness.MARKER))
        self.assertIn("- `luks (utah)`", latest)
        self.assertIn("No promotion found", text)


if __name__ == "__main__":
    unittest.main()
