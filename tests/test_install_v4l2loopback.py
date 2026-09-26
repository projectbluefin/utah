"""install-v4l2loopback.sh must hand the module from builder to image intact.

The module is compiled in a throwaway builder stage and registered in the image
by a second run of the same script (projectbluefin/utah#291). The failure modes
that matter are in that hand-off: the builder writing a partial modules.dep the
image would inherit, the image run picking the OGC kernel as the "base" one,
and an image that lacks the module or v4l2loopback-ctl passing anyway. These
drive the real script against a synthetic root with stub dnf/depmod/modinfo;
the compile itself needs a kernel tree and is exercised by the image build.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/install-v4l2loopback.sh"
BASE = "7.2.5-200.fc44.x86_64"
# Sorts above BASE, so picking the newest tree without excluding OGC is caught.
OGC = "7.9.0-ogc1"
MODULE = "usr/lib/modules/{}/extra/v4l2loopback/v4l2loopback.ko"


class InstallV4l2loopbackTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "root"
        for release in (BASE, OGC):
            (self.root / "usr/lib/modules" / release).mkdir(parents=True)
        (self.root / "usr/lib/utah").mkdir(parents=True)
        (self.root / "usr/lib/utah/ogc-kernel-release").write_text(OGC + "\n")
        self.calls = self.tmp / "calls"
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for tool in ("dnf", "depmod", "modinfo"):
            stub = bin_dir / tool
            stub.write_text(f'#!/bin/sh\necho "{tool} $*" >>"{self.calls}"\n')
            stub.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
                        UTAH_V4L2LOOPBACK_ROOT=str(self.root))

    def put(self, base: Path, rel: str) -> None:
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")

    def run_script(self, *args):
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True,
                              text=True, env=self.env)

    def called(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_builder_run_with_a_staged_module_never_touches_modules_dep(self):
        stage = self.tmp / "out"
        self.put(stage, MODULE.format(BASE))
        result = self.run_script("base", str(stage))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"v4l2loopback for {BASE} is already staged", result.stdout)
        self.assertEqual(self.called(), [])

    def test_image_run_registers_the_base_kernel_not_ogc(self):
        self.put(self.root, MODULE.format(BASE))
        self.put(self.root, "usr/bin/v4l2loopback-ctl")
        result = self.run_script("base")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"installed for {BASE}", result.stdout)
        self.assertIn(f"depmod -b {self.root} -a {BASE}", self.called())

    def test_image_run_fails_when_ctl_is_missing(self):
        self.put(self.root, MODULE.format(BASE))
        result = self.run_script("base")
        self.assertEqual(result.returncode, 1)
        self.assertIn("usr/bin/v4l2loopback-ctl is missing", result.stderr)

    def test_image_run_without_a_staged_module_fails_instead_of_compiling(self):
        # Compiling here would pull kernel-devel into a shipped layer.
        result = self.run_script("base")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"No staged v4l2loopback module for {BASE}", result.stderr)
        self.assertEqual(self.called(), [])

    def test_ogc_without_its_build_tree_fails_before_fetching_anything(self):
        result = self.run_script("ogc")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"No OGC kernel build tree at {self.root}/usr/lib/modules/{OGC}/build",
                      result.stderr)
        self.assertEqual(self.called(), [])

    def test_ogc_cannot_be_staged(self):
        result = self.run_script("ogc", str(self.tmp / "out"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    def test_a_missing_kernel_tree_fails(self):
        (self.root / "usr/lib/utah/ogc-kernel-release").unlink()
        (self.root / "usr/lib/modules" / OGC).rmdir()
        (self.root / "usr/lib/modules" / BASE).rmdir()
        result = self.run_script("base")
        self.assertEqual(result.returncode, 1)
        self.assertIn("No base kernel module tree", result.stderr)


if __name__ == "__main__":
    unittest.main()
