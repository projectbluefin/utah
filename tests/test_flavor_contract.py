"""The desktop contract must accept every flavor scripts/flavors.py builds.

contracts/bluefin-desktop.toml checks image-info.json inside a built image, so
its image-flavor and image-ref patterns restate the flavor roster and the
flavor-to-image-name map that config/flavors.json and scripts/flavors.py own.
Adding or renaming a flavor there without updating the contract would only
fail after a full image build. This pins the two together host-side.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLAVORS = ROOT / "scripts" / "flavors.py"
CONTRACT = ROOT / "contracts" / "bluefin-desktop.toml"


def flavors_py(*args: str) -> str:
    return subprocess.run(
        ["python3", str(FLAVORS), *args],
        check=True, capture_output=True, text=True).stdout.strip()


class DesktopContractFlavorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        contract = tomllib.loads(CONTRACT.read_text())
        cls.image_info = contract["branding"]["image_info"]
        cls.patterns = contract["branding"]["image_info_patterns"]
        cls.flavors = json.loads(flavors_py("list"))

    def test_the_roster_is_not_empty(self):
        self.assertTrue(self.flavors)

    def test_every_built_flavor_matches_the_image_flavor_pattern(self):
        pattern = re.compile(self.patterns["image-flavor"])
        for flavor in self.flavors:
            with self.subTest(flavor=flavor):
                self.assertRegex(flavor, pattern)

    def test_every_built_image_matches_the_image_ref_pattern(self):
        # configure-branding.sh writes
        # ostree-image-signed:docker://ghcr.io/<vendor>/<IMAGE_NAME>.
        pattern = re.compile(self.patterns["image-ref"])
        vendor = self.image_info["image-vendor"]
        for flavor in self.flavors:
            image = flavors_py("image", flavor)
            ref = f"ostree-image-signed:docker://ghcr.io/{vendor}/{image}"
            with self.subTest(flavor=flavor, ref=ref):
                self.assertRegex(ref, pattern)


if __name__ == "__main__":
    unittest.main()
