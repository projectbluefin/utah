"""The kernel cache tag must move whenever the cache image's contents would.

CI builds and pushes the cache image only when `just kernel-cache-tag` names a
tag that is not already published, and the gaming, nvidia and nvidia-gaming
builds take that image as their BASE_IMAGE. So a key that misses an input does
not fail anything -- it hands those three flavors an image built from the
previous recipe, and the only symptom is a kernel build that did not happen.

These tests copy the repository's hashed inputs into a scratch tree, mutate one
at a time, and assert the tag changes. The mutations are deliberately shallow
(a trailing comment) because the contract is "any edit, comments included", not
"any semantically significant edit".
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "kernel-cache-tag.sh"

# Every file the tag is required to cover, relative to the repository root.
HASHED_INPUTS = (
    "Containerfile.kernel",
    "scripts/install-ogc-kernel.sh",
    "scripts/install-nvidia.sh",
    "packages/hummingbird.repo",
    "packages/fedora-44.repo",
)


def tag_of(root: Path) -> str:
    """Run the real script against `root` and return the tag it prints."""
    result = subprocess.run(
        ["bash", str(root / "scripts" / "kernel-cache-tag.sh")],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"kernel-cache-tag.sh failed:\n{result.stderr}")
    return result.stdout.strip()


class KernelCacheTagTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "utah"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "packages").mkdir(parents=True)
        shutil.copy2(SCRIPT, self.root / "scripts" / "kernel-cache-tag.sh")
        for rel in HASHED_INPUTS:
            shutil.copy2(ROOT / rel, self.root / rel)
        self.baseline = tag_of(self.root)

    def test_tag_is_a_short_stable_hex_digest(self):
        self.assertRegex(self.baseline, r"^[0-9a-f]{16}$")
        self.assertEqual(self.baseline, tag_of(self.root), "tag is not deterministic")

    def test_tag_matches_the_real_repository(self):
        """The scratch tree reproduces the tag the repository itself produces."""
        self.assertEqual(self.baseline, tag_of(ROOT))

    def test_every_hashed_input_moves_the_tag(self):
        for rel in HASHED_INPUTS:
            with self.subTest(input=rel):
                path = self.root / rel
                original = path.read_text()
                path.write_text(original + "\n# cache-key probe\n")
                self.addCleanup(path.write_text, original)
                self.assertNotEqual(
                    self.baseline,
                    tag_of(self.root),
                    f"editing {rel} left the kernel cache tag unchanged, so CI would "
                    f"reuse the previously published cache image",
                )
                path.write_text(original)

    def test_containerfile_recipe_body_moves_the_tag(self):
        """A recipe edit that leaves `ARG BASE_IMAGE=` alone must still rotate.

        Hashing only the `ARG BASE_IMAGE=` line was the original defect: the
        single RUN decides what lands in /cache-out and the final stage decides
        what is copied out, and neither is on that line.
        """
        path = self.root / "Containerfile.kernel"
        original = path.read_text()
        self.assertIn("/cache-out", original)
        mutated = original.replace("/cache-out", "/cache-out-probe")
        self.assertNotEqual(original, mutated)

        arg_line = "ARG BASE_IMAGE="
        before = [ln for ln in original.splitlines() if ln.startswith(arg_line)]
        after = [ln for ln in mutated.splitlines() if ln.startswith(arg_line)]
        self.assertEqual(before, after, "probe must not touch the base image pin")

        path.write_text(mutated)
        self.addCleanup(path.write_text, original)
        self.assertNotEqual(
            self.baseline,
            tag_of(self.root),
            "a Containerfile.kernel recipe change that leaves ARG BASE_IMAGE= "
            "alone did not change the tag, so the published cache image would "
            "be reused for a recipe that no longer builds it",
        )

    def test_base_image_pin_still_moves_the_tag(self):
        """Widening the key must not drop what it already covered."""
        path = self.root / "Containerfile.kernel"
        original = path.read_text()
        mutated = "\n".join(
            line.replace("sha256:", "sha256:0") if line.startswith("ARG BASE_IMAGE=") else line
            for line in original.splitlines()
        )
        self.assertNotEqual(original, mutated, "no ARG BASE_IMAGE= digest to mutate")
        path.write_text(mutated + "\n")
        self.addCleanup(path.write_text, original)
        self.assertNotEqual(self.baseline, tag_of(self.root))


if __name__ == "__main__":
    unittest.main()
