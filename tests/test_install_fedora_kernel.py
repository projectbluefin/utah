"""install-fedora-kernel.sh check: the build fails unless the patched kernel ships.

Every flavor builds FROM the kernel cache image, whose distribution kernel is
Fedora's rebuilt with the btrfs fs-verity fix. The Containerfile runs `check`
first so a build on the pristine Hummingbird base -- or a cache image whose
swap left Fedora's tree behind -- fails instead of shipping a kernel that
reports intact composefs objects on btrfs as corrupt. The OGC kernel is a
separate tree and must not trip it.
"""
import os
import platform
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install-fedora-kernel.sh"
TEXT = SCRIPT.read_text()


def default(name: str) -> str:
    match = re.search(rf'^{name}="\$\{{UTAH_{name}:-([^}}]+)\}}"', TEXT, re.MULTILINE)
    assert match, f"{name} default not found"
    return match.group(1)


ARCH = platform.machine()
VERSION = default("FEDORA_KERNEL_VERSION")
RELEASE = default("FEDORA_KERNEL_RELEASE")
SPEC, DIST = RELEASE.rsplit(".", 1)
STOCK = f"{VERSION}-{RELEASE}.{ARCH}"
PATCHED = f"{VERSION}-{SPEC}.1.utah1.{DIST}.{ARCH}"
OGC = "7.1.8-ogc1"


class CheckTests(unittest.TestCase):
    def check(self, trees, ogc=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for tree in trees:
                (root / "usr/lib/modules" / tree).mkdir(parents=True)
            if ogc:
                (root / "usr/lib/utah").mkdir(parents=True)
                (root / "usr/lib/utah/ogc-kernel-release").write_text(ogc + "\n")
            return subprocess.run(
                ["bash", str(SCRIPT), "check"], capture_output=True, text=True,
                env={**os.environ, "UTAH_FEDORA_KERNEL_ROOT": str(root)})

    def test_the_patched_kernel_passes(self):
        result = self.check([PATCHED])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_ogc_kernel_beside_it_is_not_the_distribution_kernel(self):
        result = self.check([PATCHED, OGC], ogc=OGC)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_pristine_base_kernel_fails_and_says_what_to_build_on(self):
        result = self.check([STOCK])
        self.assertEqual(result.returncode, 1)
        self.assertIn(PATCHED, result.stderr)
        self.assertIn(STOCK, result.stderr)
        self.assertIn("kernel cache", result.stderr)

    def test_a_stock_tree_left_beside_the_patched_one_fails(self):
        # bootc boots one kernel; a leftover Fedora tree means the swap did not
        # finish, and the selection could pick either.
        result = self.check([STOCK, PATCHED])
        self.assertEqual(result.returncode, 1)

    def test_no_kernel_at_all_fails(self):
        self.assertEqual(self.check([]).returncode, 1)


if __name__ == "__main__":
    unittest.main()
