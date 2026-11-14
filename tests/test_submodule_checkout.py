"""The contract checkout retries transient failures from extension hosts."""

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
BUILD = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
SCRIPT = (ROOT / ".github/scripts/update-submodules-with-retry.sh").read_text()


class ContractSubmoduleCheckoutTests(unittest.TestCase):
    def test_checkout_defers_submodules_to_retry_step(self):
        steps = BUILD["jobs"]["contract"]["steps"]
        checkout = next(step for step in steps if step.get("name") == "Checkout")
        self.assertFalse(checkout["with"]["submodules"])
        retry = next(step for step in steps if step.get("name") == "Checkout submodules with retry")
        self.assertEqual(retry["run"], "bash .github/scripts/update-submodules-with-retry.sh")

    def test_retry_script_serializes_and_retries_submodule_fetches(self):
        self.assertIn("max_attempts=5", SCRIPT)
        self.assertIn("submodule.fetchJobs=1", SCRIPT)
        self.assertIn("submodule update --init --recursive", SCRIPT)
        self.assertIn("sleep \"$delay\"", SCRIPT)


if __name__ == "__main__":
    unittest.main()
