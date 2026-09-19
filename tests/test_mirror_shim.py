"""The shim mirroring must say what it could not find.

Bumping BASE_IMAGE to a kernel-7.2 Hummingbird turned this step into a silent
failure: the build printed "Utah desktop contract passed" and then exited 1,
three attempts in a row, with no indication of which of four chained
predicates had tripped. These tests drive the real script against fake
filesystems, so the diagnostic is a gate rather than a good intention.
"""
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/mirror-shim.sh"


class MirrorShimTests(unittest.TestCase):
    def run_script(self, tmp, *, shim_rc=0, shim_out="16.1-1", stage=True):
        """Run the real script with a stub rpm and a fake bootupd tree."""
        bin_dir = Path(tmp) / "bin"
        bin_dir.mkdir()
        (bin_dir / "rpm").write_text(
            f'#!/usr/bin/env bash\nprintf "%s" "{shim_out}"\nexit {shim_rc}\n'
        )
        (bin_dir / "rpm").chmod(0o755)

        bootupd = Path(tmp) / "bootupd/updates/EFI/fedora"
        if stage:
            bootupd.mkdir(parents=True)
            (bootupd / "shimx64.efi").write_text("payload")
        else:
            bootupd.parent.mkdir(parents=True)

        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["BOOTUPD_EFI"] = str(bootupd)
        env["SHIM_ROOT"] = str(Path(tmp) / "efi/shim")
        return subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True, env=env
        )

    def test_a_staged_payload_is_mirrored_under_the_shim_version(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_script(tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            mirrored = Path(tmp) / "efi/shim/16.1-1/EFI/fedora/shimx64.efi"
            self.assertTrue(mirrored.is_file(), result.stdout + result.stderr)
            self.assertEqual(mirrored.read_text(), "payload")
            self.assertIn("16.1-1", result.stdout)

    def test_a_missing_shim_package_names_the_package(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_script(tmp, shim_rc=1, shim_out="package shim-x64 is not installed")
            self.assertEqual(result.returncode, 1)
            self.assertIn("shim-x64 is not installed", result.stderr)
            # The remedy matters as much as the diagnosis: a base image that
            # dropped the package needs it added, not the step skipped.
            self.assertIn("package contract", result.stderr)

    def test_an_unstaged_payload_names_the_path_and_lists_the_parent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_script(tmp, stage=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("no EFI payload to mirror", result.stderr)
            # The two failures must be distinguishable: this one proves the
            # package IS installed, which points at bootupd rather than dnf.
            self.assertIn("16.1-1 is installed", result.stderr)

    def test_the_two_failures_do_not_share_a_message(self):
        import tempfile
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            missing = self.run_script(a, shim_rc=1, shim_out="not installed").stderr
            unstaged = self.run_script(b, stage=False).stderr
            self.assertNotEqual(missing, unstaged)


class ContainerfileWiringTests(unittest.TestCase):
    def test_the_containerfile_calls_the_script_and_not_the_old_chain(self):
        text = (ROOT / "Containerfile").read_text()
        self.assertIn("/usr/local/libexec/utah-mirror-shim", text)
        self.assertIn("mirror-shim.sh:utah-mirror-shim", text)
        self.assertIn("scripts/mirror-shim.sh \\", text)
        # The inline chain is what hid the failure; it must not come back.
        self.assertNotIn("rpm -q --qf '%{VERSION}-%{RELEASE}' shim-x64", text)


if __name__ == "__main__":
    unittest.main()
