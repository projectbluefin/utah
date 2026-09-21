"""The hand-written package counts in README.md and package-contract.md must
not be able to drift from the manifests the way they already did once:
packages/utah.toml's [unavailable] list grew from 4 to 9 entries while both
documents kept quoting 4.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-doc-counts.py"


def load_module():
    spec = importlib.util.spec_from_file_location("check_doc_counts", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CheckDocCountsTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_committed_docs_match_the_manifests(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_expected_counts_match_generate_site_data(self):
        bluefin, additions, unavailable = self.module.expected_counts()
        data = self.module.load_generator().build(ROOT)
        self.assertEqual(unavailable, data["totals"]["unavailable"])
        self.assertEqual(bluefin + additions, data["totals"]["installed"])
        # Every real gap the manifest documents is exactly the count checked --
        # this is the number that drifted (4 vs. 9) before this check existed.
        self.assertEqual(
            unavailable,
            len(self.module.load_generator().load_installer().section(
                ROOT / "packages/utah.toml", "unavailable")))

    def test_a_stale_readme_count_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale_readme = Path(tmp) / "README.md"
            text = (ROOT / "README.md").read_text()
            rewritten, count = re.subn(
                r"Genuinely unavailable \| \*\*\d+\*\*",
                "Genuinely unavailable | **4**", text)
            self.assertEqual(count, 1)
            stale_readme.write_text(rewritten)

            module = load_module()
            module.README = stale_readme
            self.assertEqual(module.main(), 1)

    def test_a_current_readme_count_passes(self):
        module = load_module()
        self.assertEqual(module.main(), 0)


if __name__ == "__main__":
    unittest.main()
