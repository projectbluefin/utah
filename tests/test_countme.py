"""Regression tests for the weekly countme analytics client.

The client (scripts/countme.sh) is exercised through its dry-run mode, which
prints the analytics URL instead of sending it, so the pure logic -- cohort
bucketing, os-release label derivation, and URL construction -- is covered
without any network access. The opt-out and first-run epoch paths are covered
by pointing the script's test overrides at a scratch directory.
"""
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "countme.sh"
ENDPOINT = "https://countme.projectbluefin.io/metalink"


def run_dry_run(tmp: Path, **env) -> str:
    """Run the client in dry-run mode and return the URL it would have sent."""
    overrides = {"COUNTME_DRY_RUN": "1", "COUNTME_OS_RELEASE": str(tmp / "nope")}
    overrides.update(env)
    full_env = {**os.environ, **overrides}
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=full_env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def bucket_of(url: str) -> int:
    for token in url.split("&"):
        if token.startswith("countme="):
            return int(token.split("=", 1)[1])
    raise AssertionError(f"no countme token in {url}")


class BucketTests(unittest.TestCase):
    def test_first_week_is_bucket_1(self):
        age = 6 * 86400
        url = run_dry_run(Path(tempfile.mkdtemp()), COUNTME_EPOCH=str(int(time.time()) - age))
        self.assertEqual(bucket_of(url), 1)

    def test_two_to_four_weeks_is_bucket_2(self):
        age = 14 * 86400
        url = run_dry_run(Path(tempfile.mkdtemp()), COUNTME_EPOCH=str(int(time.time()) - age))
        self.assertEqual(bucket_of(url), 2)

    def test_five_to_twenty_four_weeks_is_bucket_3(self):
        age = 90 * 86400
        url = run_dry_run(Path(tempfile.mkdtemp()), COUNTME_EPOCH=str(int(time.time()) - age))
        self.assertEqual(bucket_of(url), 3)

    def test_older_than_twenty_four_weeks_is_bucket_4(self):
        age = 400 * 86400
        url = run_dry_run(Path(tempfile.mkdtemp()), COUNTME_EPOCH=str(int(time.time()) - age))
        self.assertEqual(bucket_of(url), 4)


class BoundaryTests(unittest.TestCase):
    """Cohort boundaries are inclusive on the upper edge of each earlier bucket."""

    def setUp(self):
        self.now = int(time.time())

    def _url(self, age_days):
        return run_dry_run(
            Path(tempfile.mkdtemp()),
            COUNTME_EPOCH=str(self.now - age_days * 86400),
        )

    def test_zero_days_is_bucket_1(self):
        self.assertEqual(bucket_of(self._url(0)), 1)

    def test_exactly_seven_days_is_bucket_2(self):
        self.assertEqual(bucket_of(self._url(7)), 2)

    def test_just_below_seven_days_is_bucket_1(self):
        # 6 days, 23 hours -- still the first week.
        self.assertEqual(bucket_of(self._url(6)), 1)

    def test_exactly_28_days_is_bucket_3(self):
        self.assertEqual(bucket_of(self._url(28)), 3)

    def test_exactly_168_days_is_bucket_4(self):
        self.assertEqual(bucket_of(self._url(168)), 4)


class UrlFormatTests(unittest.TestCase):
    def test_url_has_every_required_parameter(self):
        url = run_dry_run(
            Path(tempfile.mkdtemp()),
            COUNTME_EPOCH=str(int(time.time())),
            COUNTME_REPO="utah",
            COUNTME_TAG="testing",
            COUNTME_FLAVOR="gaming",
            COUNTME_ARCH="x86_64",
        )
        expected = (
            f"{ENDPOINT}?repo=utah&tag=testing"
            f"&flavor=gaming&arch=x86_64&countme=1"
        )
        self.assertEqual(url, expected)

    def test_endpoint_and_parameter_order(self):
        url = run_dry_run(
            Path(tempfile.mkdtemp()),
            COUNTME_EPOCH=str(int(time.time())),
            COUNTME_REPO="utah",
            COUNTME_TAG="testing",
            COUNTME_FLAVOR="main",
            COUNTME_ARCH="x86_64",
        )
        self.assertTrue(
            url.startswith(f"{ENDPOINT}?repo=utah&tag=testing&flavor=main&arch=x86_64&countme="),
            url,
        )


class LabelDerivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "os-release").write_text(
            '# a comment\n'
            'ID=utah\n'
            'IMAGE_ID="utah"\n'
            'IMAGE_FLAVOR=\'gaming\'\n'
            'IMAGE_VERSION="testing"\n'
            'IMAGE_REF=ghcr.io/projectbluefin/utah:testing\n'
        )
        self.os_release = str(self.tmp / "os-release")

    def test_reads_labels_from_os_release(self):
        url = run_dry_run(
            self.tmp,
            COUNTME_EPOCH=str(int(time.time())),
            COUNTME_OS_RELEASE=self.os_release,
        )
        self.assertIn("repo=utah", url)
        self.assertIn("tag=testing", url)
        self.assertIn("flavor=gaming", url)

    def test_env_override_wins_over_os_release(self):
        url = run_dry_run(
            self.tmp,
            COUNTME_EPOCH=str(int(time.time())),
            COUNTME_OS_RELEASE=self.os_release,
            COUNTME_FLAVOR="main",
        )
        self.assertIn("flavor=main", url)

    def test_missing_image_id_falls_back_to_utah(self):
        (self.tmp / "bare-os").write_text("PRETTY_NAME=Utah\n")
        url = run_dry_run(
            self.tmp,
            COUNTME_EPOCH=str(int(time.time())),
            COUNTME_OS_RELEASE=str(self.tmp / "bare-os"),
        )
        self.assertIn("repo=utah", url)


class BehaviourTests(unittest.TestCase):
    def test_opt_out_skips_without_sending(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "disabled").touch()
        env = {
            "COUNTME_DRY_RUN": "1",
            "COUNTME_OS_RELEASE": str(tmp / "nope"),
            "COUNTME_DISABLE_FILE": str(tmp / "disabled"),
        }
        full_env = {**os.environ, **env}
        result = subprocess.run(
            ["bash", str(SCRIPT)], env=full_env, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(ENDPOINT, result.stdout)
        self.assertIn("opt-out", result.stderr)

    def test_first_run_creates_epoch_file(self):
        tmp = Path(tempfile.mkdtemp())
        epoch_file = tmp / "epoch"
        url = run_dry_run(
            tmp,
            COUNTME_EPOCH_FILE=str(epoch_file),
            COUNTME_OS_RELEASE=str(tmp / "nope"),
        )
        self.assertEqual(bucket_of(url), 1)
        self.assertTrue(epoch_file.is_file())
        self.assertRegex(epoch_file.read_text().strip(), r"^\d+$")

    def test_negative_age_is_clamped_to_bucket_1(self):
        # A clock that went backwards must not yield a negative age.
        url = run_dry_run(
            Path(tempfile.mkdtemp()),
            COUNTME_EPOCH=str(int(time.time()) + 86400),
            COUNTME_OS_RELEASE=str(Path(tempfile.mkdtemp()) / "nope"),
        )
        self.assertEqual(bucket_of(url), 1)


if __name__ == "__main__":
    unittest.main()
