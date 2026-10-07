"""Exercise the actual guest checker against disposable ESP/XBOOTLDR trees."""

import subprocess
import tempfile
import unittest
from pathlib import Path


CHECKER = Path(__file__).resolve().parents[1] / "iso/scripts/verify-boot-files.sh"


class BootFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "esp"
        self.entries = self.root / "loader/entries"
        self.entries.mkdir(parents=True)

    def entry(self, text=None, root=None):
        root = root or self.root
        directory = root / "loader/entries"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "utah.conf").write_text(
            text if text is not None else
            "linux /ostree/vmlinuz\ninitrd /ostree/initramfs\n"
            "options quiet ostree=/ostree/boot.1/utah/hash/0\n"
        )

    def file(self, name, root=None):
        path = (root or self.root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"boot data")

    def run_checker(self, *roots):
        return subprocess.run(
            ["bash", str(CHECKER), *map(str, roots or (self.root,))],
            capture_output=True, text=True, check=False,
        )

    def test_complete_set(self):
        self.entry()
        self.file("ostree/vmlinuz")
        self.file("ostree/initramfs")
        result = self.run_checker()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("PASS:", result.stdout)

    def test_missing_kernel_or_initrd(self):
        for missing in ("ostree/vmlinuz", "ostree/initramfs"):
            with self.subTest(missing=missing):
                self.entry()
                self.file("ostree/vmlinuz")
                self.file("ostree/initramfs")
                (self.root / missing).unlink()
                result = self.run_checker()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(str(self.root / missing), result.stdout)

    def test_every_initrd_is_required(self):
        self.entry("linux /vmlinuz\ninitrd /microcode\ninitrd /initramfs\noptions ostree=/x\n")
        self.file("vmlinuz")
        self.file("initramfs")
        self.assertNotEqual(self.run_checker().returncode, 0)
        self.file("microcode")
        self.assertEqual(self.run_checker().returncode, 0)

    def test_rollback_entry_files_are_also_checked(self):
        self.entry()
        self.file("ostree/vmlinuz")
        self.file("ostree/initramfs")
        (self.entries / "rollback.conf").write_text(
            "linux /old/vmlinuz\ninitrd /old/initramfs\noptions ostree=/old\n"
        )
        result = self.run_checker()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback.conf", result.stdout)
        self.assertIn("/old/initramfs", result.stdout)

    def test_empty_file_and_directory_fail(self):
        self.entry()
        self.file("ostree/vmlinuz")
        self.file("ostree/initramfs")
        (self.root / "ostree/initramfs").write_bytes(b"")
        self.assertNotEqual(self.run_checker().returncode, 0)
        (self.root / "ostree/initramfs").unlink()
        (self.root / "ostree/initramfs").mkdir()
        self.assertNotEqual(self.run_checker().returncode, 0)

    def test_missing_directive_not_satisfied_by_options(self):
        for directive in ("linux /vmlinuz", "initrd /initramfs"):
            self.entry(f"{directive}\noptions ostree=/x\n")
            self.file("vmlinuz")
            self.file("initramfs")
            self.assertIn("missing linux or initrd", self.run_checker().stdout)

    def test_no_entries_fails(self):
        self.assertNotEqual(self.run_checker().returncode, 0)

    def test_unrelated_entry_does_not_satisfy_gate(self):
        self.entry("efi /EFI/Linux/other.efi\noptions quiet\n")
        self.assertNotEqual(self.run_checker().returncode, 0)

    def test_paths_resolve_on_entry_partition_not_other_root(self):
        other = Path(self.temp.name) / "xbootldr"
        self.entry(root=other)
        self.file("ostree/vmlinuz")
        self.file("ostree/initramfs")
        self.assertNotEqual(self.run_checker(self.root, other).returncode, 0)
        self.file("ostree/vmlinuz", other)
        self.file("ostree/initramfs", other)
        self.assertEqual(self.run_checker(self.root, other).returncode, 0)

    def test_invalid_paths_fail(self):
        for path in ("", "relative", "/../vmlinuz", "/a/../../vmlinuz"):
            with self.subTest(path=path):
                self.entry(f"linux {path}\ninitrd /initramfs\noptions ostree=/x\n")
                self.file("initramfs")
                self.assertIn("invalid boot path", self.run_checker().stdout)

    def test_harness_checks_all_phases_and_uses_sudo(self):
        harness = CHECKER.with_name("lifecycle-e2e.sh").read_text()
        self.assertIn('ssh_target_sudo bash -c "${checker}"', harness)
        for phase in ("baseline", "staged", "upgraded", "rollback"):
            self.assertIn(f"verify_boot_files {phase}\n", harness)


if __name__ == "__main__":
    unittest.main()
