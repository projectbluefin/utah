"""regenerate-initramfs.sh must rebuild the bootc kernel's initramfs and refuse a bad one.

The script runs at the image's last package step (#564). The image build only
ever exercises the path where the rebuilt initramfs is complete, so the guards
that stop an initrd without early microcode or without the LUKS unlock modules
from shipping never execute in CI. These drive the real script with stub
utah-live-kernel, dracut and lsinitrd: each guard must fail the build and name
what is missing, and a failing step must stop the steps after it.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/regenerate-initramfs.sh"
LIVE_KERNEL = "/usr/local/libexec/utah-live-kernel"
KVER = "7.1.8-ogc1"
IMAGE = f"/usr/lib/modules/{KVER}/initramfs.img"
VENDORS = ("AuthenticAMD", "GenuineIntel")
MODULES = ("bootc", "ostree", "fido2", "tpm2-tss", "pkcs11", "pcsc")


def listing(early=True, vendors=VENDORS):
    lines = []
    if early:
        lines.append("Early CPIO image")
        lines.append("=" * 40)
        lines += [f"-rw-r--r-- 1 root root 0 kernel/x86/microcode/{v}.bin" for v in vendors]
        lines.append("=" * 40)
    lines.append(f"Image: {IMAGE}: 80M")
    return "\n".join(lines) + "\n"


class RegenerateInitramfsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.calls = self.tmp / "calls"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        # The script calls the installed helper by absolute path; point a copy
        # at a stub so the test never depends on the host's /usr/local.
        self.script = self.tmp / "regenerate-initramfs.sh"
        text = SCRIPT.read_text()
        self.assertIn(LIVE_KERNEL, text)
        self.script.write_text(text.replace(LIVE_KERNEL, str(self.bin / "utah-live-kernel")))
        self.set_kernel()
        self.set_dracut()
        self.set_lsinitrd()

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(f'#!/bin/sh\necho "{name} $*" >>"{self.calls}"\n{body}\n')
        path.chmod(0o755)

    def set_kernel(self, out=KVER, rc=0):
        self.stub("utah-live-kernel", f'echo "{out}"\nexit {rc}')

    def set_dracut(self, rc=0):
        self.stub("dracut", f"exit {rc}")

    def set_lsinitrd(self, files=None, modules=MODULES):
        files_path = self.tmp / "lsinitrd.files"
        modules_path = self.tmp / "lsinitrd.modules"
        files_path.write_text(listing() if files is None else files)
        modules_path.write_text("".join(f"{m}\n" for m in modules))
        self.stub("lsinitrd",
                  f'if [ "$1" = "-m" ]; then cat "{modules_path}"; '
                  f'else cat "{files_path}"; fi')

    def run_script(self):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}")
        return subprocess.run(["bash", str(self.script)], capture_output=True,
                              text=True, env=env)

    def called(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def assert_fails_naming(self, needle):
        result = self.run_script()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("ERROR:", result.stderr)
        self.assertIn(needle, result.stderr)
        self.assertNotIn("early microcode and unlock modules present", result.stdout)
        return result

    def test_complete_initramfs_passes(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{IMAGE}: early microcode and unlock modules present", result.stdout)

    def test_selects_and_promotes_the_bootc_kernel_then_rebuilds_it_host_independently(self):
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.called(), [
            "utah-live-kernel / --select-bootc --promote-ogc",
            f"dracut --no-hostonly --reproducible --zstd --kver {KVER} --force {IMAGE}",
            f"lsinitrd {IMAGE}",
            f"lsinitrd -m {IMAGE}",
        ])

    def test_missing_early_cpio_fails(self):
        self.set_lsinitrd(files=listing(early=False))
        self.assert_fails_naming("has no early microcode cpio")

    def test_microcode_without_an_early_cpio_header_fails(self):
        body = "".join(f"kernel/x86/microcode/{v}.bin\n" for v in VENDORS)
        self.set_lsinitrd(files=body)
        self.assert_fails_naming("has no early microcode cpio")

    def test_each_missing_vendor_microcode_fails(self):
        for vendor in VENDORS:
            with self.subTest(vendor=vendor):
                others = tuple(v for v in VENDORS if v != vendor)
                self.set_lsinitrd(files=listing(vendors=others))
                self.assert_fails_naming(f"carries no {vendor} microcode")

    def test_each_missing_dracut_module_fails(self):
        for module in MODULES:
            with self.subTest(module=module):
                self.set_lsinitrd(modules=tuple(m for m in MODULES if m != module))
                self.assert_fails_naming(f"missing the {module} dracut module")

    def test_module_names_match_whole_lines_only(self):
        # A similarly named module must not satisfy the check for the real one.
        lookalikes = tuple(f"{m}-extra" if m == "fido2" else m for m in MODULES)
        self.set_lsinitrd(modules=lookalikes + ("systemd-fido2",))
        self.assert_fails_naming("missing the fido2 dracut module")

    def test_kernel_selection_failure_stops_before_dracut(self):
        self.set_kernel(out="", rc=3)
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.called(), ["utah-live-kernel / --select-bootc --promote-ogc"])

    def test_dracut_failure_stops_before_inspection(self):
        self.set_dracut(rc=1)
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c.startswith("lsinitrd") for c in self.called()), self.called())
        self.assertNotIn("present", result.stdout)


if __name__ == "__main__":
    unittest.main()
