"""The shim mirroring must work on both base layouts, and say what it cannot find.

Two Hummingbird base layouts exist and both are correct. Verified by listing
the layers of each image directly from the registry:

  old base sha256:c5539f9e...
    usr/lib/bootupd/updates/EFI/fedora/{shimx64,shim,mmx64,grubx64}.efi
    (no usr/lib/efi/shim at all)

  new base sha256:db1007fd...
    usr/lib/efi/shim/16.1-5/EFI/fedora/{shimx64,shim,mmx64}.efi
    usr/lib/bootupd/updates/ holds only BIOS.json and EFI.json

The step was four predicates chained with && that hardcoded the old layout, so
the base bump turned it into a silent failure: "Utah desktop contract passed",
then exit 1, three attempts out of three, with nothing said about which of four
things went wrong. These tests drive the real script against fake filesystems
for both layouts and for the two genuine failures.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/mirror-shim.sh"


class MirrorShimTests(unittest.TestCase):
    def run_script(self, tmp, *, shim_rc=0, shim_out="16.1-5",
                   staged=True, preinstalled=None):
        """Run the real script with a stub rpm and a synthetic filesystem.

        staged:       the old layout -- payload under the bootupd update tree.
        preinstalled: the new layout -- a version directory already under
                      SHIM_ROOT carrying EFI/fedora/shimx64.efi.
        """
        tmp = Path(tmp)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "rpm").write_text(
            f'#!/usr/bin/env bash\nprintf "%s" "{shim_out}"\nexit {shim_rc}\n'
        )
        (bin_dir / "rpm").chmod(0o755)

        bootupd = tmp / "bootupd/updates/EFI/fedora"
        if staged:
            bootupd.mkdir(parents=True)
            (bootupd / "shimx64.efi").write_text("staged-payload")
        else:
            bootupd.parent.mkdir(parents=True)

        shim_root = tmp / "efi/shim"
        if preinstalled:
            base = shim_root / preinstalled / "EFI/fedora"
            base.mkdir(parents=True)
            (base / "shimx64.efi").write_text("base-payload")

        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["BOOTUPD_EFI"] = str(bootupd)
        env["SHIM_ROOT"] = str(shim_root)
        return subprocess.run(["bash", str(SCRIPT)], capture_output=True,
                              text=True, env=env), shim_root

    def test_the_old_layout_is_mirrored_under_the_shim_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, shim_root = self.run_script(tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            mirrored = shim_root / "16.1-5/EFI/fedora/shimx64.efi"
            self.assertTrue(mirrored.is_file(), result.stdout + result.stderr)
            self.assertEqual(mirrored.read_text(), "staged-payload")

    def test_a_base_that_already_ships_the_payload_is_a_no_op(self):
        """The new base does the mirroring itself; that must not be a failure."""
        with tempfile.TemporaryDirectory() as tmp:
            result, shim_root = self.run_script(
                tmp, staged=False, preinstalled="16.1-5")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already provides", result.stdout)
            # And it must not have disturbed what the base shipped.
            self.assertEqual(
                (shim_root / "16.1-5/EFI/fedora/shimx64.efi").read_text(),
                "base-payload")

    def test_the_existing_payload_is_found_whatever_rpm_reports(self):
        """The base names its directory for the shim version with no dist tag.

        rpm may report something else, so the check must find the file rather
        than assume the directory name matches.
        """
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self.run_script(
                tmp, staged=False, preinstalled="16.1-5",
                shim_out="16.1-5.fc44", shim_rc=0)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already provides", result.stdout)

    def test_a_missing_shim_package_and_no_payload_names_the_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self.run_script(
                tmp, staged=False, shim_rc=1,
                shim_out="package shim-x64 is not installed")
            self.assertEqual(result.returncode, 1)
            self.assertIn("shim-x64 is not installed", result.stderr)
            # The remedy matters as much as the diagnosis.
            self.assertIn("package contract", result.stderr)

    def test_an_installed_package_with_no_payload_anywhere_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self.run_script(tmp, staged=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("no EFI payload to mirror", result.stderr)
            # Distinguishable from the missing-package case: this one proves
            # the RPM side is fine, so the problem is bootupd, not dnf.
            self.assertIn("16.1-5 is installed", result.stderr)

    def test_the_two_failures_do_not_share_a_message(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            missing, _ = self.run_script(a, staged=False, shim_rc=1,
                                         shim_out="not installed")
            absent, _ = self.run_script(b, staged=False)
            self.assertNotEqual(missing.stderr, absent.stderr)


class ContainerfileWiringTests(unittest.TestCase):
    def test_the_containerfile_calls_the_script_and_not_the_old_chain(self):
        text = (ROOT / "Containerfile").read_text()
        self.assertIn("/usr/local/libexec/utah-mirror-shim", text)
        self.assertIn("mirror-shim.sh:utah-mirror-shim", text)
        self.assertIn("scripts/mirror-shim.sh \\", text)
        # The inline chain hardcoded the old layout and hid the failure.
        self.assertNotIn("rpm -q --qf '%{VERSION}-%{RELEASE}' shim-x64", text)


if __name__ == "__main__":
    unittest.main()
