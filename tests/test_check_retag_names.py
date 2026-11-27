"""Offline tests for the pinned reusable release workflow contract."""

import contextlib
import importlib.util
import io
import unittest
from unittest.mock import patch
from urllib.error import URLError
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_retag_names", ROOT / "scripts/check-retag-names.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


TEST_COMMIT = "a" * 40
CALLER = f"jobs:\n  execute:\n    uses: owner/repo/.github/workflows/release.yml@{TEST_COMMIT}\n"

class RetagNamesTests(unittest.TestCase):
    def run_check(self, workflow):
        stderr = io.StringIO()
        with patch.object(Path, "read_text", return_value=CALLER), \
             patch.object(check, "fetch_workflow", return_value=workflow), contextlib.redirect_stderr(stderr):
            result = check.main()
        return result, stderr.getvalue()

    def test_names_present_in_same_job(self):
        workflow = "jobs:\n  release:\n    name: Promote release\n    steps:\n      - name: Promote digests to target tags\n"
        self.assertEqual(self.run_check(workflow)[0], 0)

    def test_renamed_job_fails(self):
        status, message = self.run_check("jobs:\n  release:\n    name: Renamed\n    steps: []\n")
        self.assertEqual(status, 1)
        self.assertIn("Promote release", message)
        self.assertIn(TEST_COMMIT, message)

    def test_renamed_step_fails_even_if_another_job_has_it(self):
        workflow = ("jobs:\n  release:\n    name: Promote release\n    steps:\n      - name: Renamed\n"
                    "  other:\n    steps:\n      - name: Promote digests to target tags\n")
        status, message = self.run_check(workflow)
        self.assertEqual(status, 1)
        self.assertIn("Promote digests to target tags", message)
        self.assertIn(TEST_COMMIT, message)

    def test_download_failure_retries_three_times(self):
        stderr = io.StringIO()
        with patch.object(check.urllib.request, "urlopen", side_effect=URLError("offline")) as fetch, contextlib.redirect_stderr(stderr):
            status = check.main()
        self.assertEqual(status, 2)
        self.assertEqual(fetch.call_count, 3)
        self.assertIn("could not fetch", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
