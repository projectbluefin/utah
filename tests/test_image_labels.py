"""The published image must label its own commit and build time (#1400)."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTAINERFILE = (ROOT / "Containerfile").read_text()
JUSTFILE = (ROOT / "Justfile").read_text()


class ImageLabelTests(unittest.TestCase):
    def test_revision_label_comes_from_sha_head(self):
        self.assertRegex(
            CONTAINERFILE,
            r'(?m)^LABEL org\.opencontainers\.image\.revision="\$\{SHA_HEAD\}"$',
        )

    def test_created_label_comes_from_build_arg(self):
        self.assertRegex(
            CONTAINERFILE,
            r'(?m)^LABEL org\.opencontainers\.image\.created="\$\{IMAGE_CREATED\}"$',
        )
        self.assertRegex(CONTAINERFILE, r"(?m)^ARG IMAGE_CREATED$")

    def test_every_build_passes_the_label_args(self):
        for arg in ("SHA_HEAD", "IMAGE_CREATED"):
            self.assertEqual(
                len(re.findall(rf"--build-arg {arg}=", JUSTFILE)), 2, arg
            )


if __name__ == "__main__":
    unittest.main()
