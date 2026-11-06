"""testing-freshness.py must call a frozen :testing stale and a fresh one fresh.

The alarm exists because :testing froze for a week with every build green. A
wrong cut-off or an unreadable tag counted as fresh would bring that silence
back, so the decision and the timestamp parsing are tested on their own; the
gh plumbing is exercised by the workflow.
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


class StaleTests(unittest.TestCase):
    def test_only_images_older_than_the_threshold_are_stale(self):
        ages = {
            "utah": NOW - dt.timedelta(hours=35, minutes=59),
            "utah-gaming": NOW - dt.timedelta(hours=36, minutes=1),
        }
        self.assertEqual(set(freshness.stale(ages, NOW, MAX_AGE)), {"utah-gaming"})

    def test_an_unreadable_tag_is_stale_not_fresh(self):
        self.assertEqual(set(freshness.stale({"utah-nvidia": None}, NOW, MAX_AGE)), {"utah-nvidia"})

    def test_the_week_long_freeze_is_stale_for_every_flavor(self):
        frozen = freshness.parse_created("2026-10-30T10:14:07Z")
        ages = dict.fromkeys(["utah", "utah-nvidia", "utah-gaming", "utah-nvidia-gaming"], frozen)
        self.assertEqual(len(freshness.stale(ages, NOW, MAX_AGE)), 4)


class ParseCreatedTests(unittest.TestCase):
    def test_skopeo_nanosecond_timestamps_parse(self):
        # skopeo prints nine fractional digits; fromisoformat takes at most six.
        when = freshness.parse_created("2026-11-06T15:57:30.123456789Z")
        self.assertEqual(when, dt.datetime(2026, 11, 6, 15, 57, 30, 123456, tzinfo=dt.timezone.utc))

    def test_whole_second_timestamps_parse(self):
        self.assertEqual(freshness.parse_created("2026-10-30T10:14:07Z"),
                         dt.datetime(2026, 10, 30, 10, 14, 7, tzinfo=dt.timezone.utc))


class BodyTests(unittest.TestCase):
    def test_body_carries_the_marker_the_issue_is_found_by(self):
        ages = {"utah": None}
        text = freshness.body(ages, {"utah"}, NOW, 36, "Latest post-testing-e2e: failure.")
        self.assertTrue(text.startswith(freshness.MARKER))
        self.assertIn("| `utah` | unreadable | **stale** |", text)


if __name__ == "__main__":
    unittest.main()
