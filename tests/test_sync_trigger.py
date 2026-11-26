"""sync-main-to-testing rebuilds the image only for pushes that can change it."""
import fnmatch
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/sync-main-to-testing.yml").read_text())
PATTERNS = WORKFLOW[True]["push"]["paths"]


def triggers(path):
    """GitHub's rule: the last pattern that matches decides; ! excludes."""
    hit = False
    for pattern in PATTERNS:
        negated = pattern.startswith("!")
        glob = pattern[1:] if negated else pattern
        if fnmatch.fnmatchcase(path, glob) or (glob.endswith("/**") and path.startswith(glob[:-2])):
            hit = not negated
    return hit


class SyncTriggerTests(unittest.TestCase):
    def test_image_inputs_rebuild(self):
        for path in ("Containerfile", "Containerfile.kernel", "packages/utah.toml",
                     "system_files/shared/usr/bin/x", "scripts/install-packages.py",
                     "config/flavors.json", "contracts/bluefin-desktop.toml", ".gitmodules",
                     "docs/agent/skills/x.md", "Justfile", "iso/live/src/configure-live.sh",
                     ".github/workflows/build.yml"):
            with self.subTest(path):
                self.assertTrue(triggers(path))

    def test_what_the_pipeline_writes_back_does_not(self):
        # Renovate's :testing repin, the E2E's README refresh, baseline refreshes.
        for path in ("iso/raw/Containerfile", "README.md", "docs/verification/index.md",
                     "docs/skills/ci-workflows.md", "baselines/GAP.md", "site/data/packages.json",
                     "tests/test_sync_trigger.py", ".github/workflows/image-installer.yml",
                     ".github/workflows/publish-installer.yml"):
            with self.subTest(path):
                self.assertFalse(triggers(path))

    def test_every_image_input_is_watched(self):
        justfile = (ROOT / "Justfile").read_text()
        line = next(l for l in justfile.splitlines() if "source_date_epoch=" in l and "git log" in l)
        inputs = line.split("-- ", 1)[1].split(")")[0].split()
        for path in inputs:
            with self.subTest(path):
                self.assertTrue(triggers(path) or triggers(path + "/x"))

    def test_the_nightly_sync_still_runs(self):
        self.assertIn("schedule", WORKFLOW[True])


if __name__ == "__main__":
    unittest.main()
