"""An image must not ship a shim without its GRUB beside it.

Utah's post-testing install e2e went red on every flavor from 2026-09-21 with
"Failed to open \\EFI\\fedora\\grubx64.efi - Not Found": shim came from Fedora
(EFI/fedora) while the factory's GRUB was built with Hummingbird's efi_vendor
(EFI/hummingbird, prefix /EFI/hummingbird compiled in). A stale unowned Fedora
grubx64.efi mirrored beside shim had hidden it until the base moved to shim
16.1. These tests drive the real guard against synthetic /usr/lib/efi trees
with a stub rpm, for the fixed layout and each way the chain has broken.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/verify-efi-chain.sh"


class VerifyEfiChainTests(unittest.TestCase):
    def run_guard(self, tmp, files, owned=()):
        """files: {relative path under EFI_ROOT: content}; owned: paths rpm claims."""
        tmp = Path(tmp)
        efi = tmp / "efi"
        for rel, content in files.items():
            p = efi / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        owned_abs = " ".join(str(efi / o) for o in owned)
        (bin_dir / "rpm").write_text(
            "#!/usr/bin/env bash\n"
            f'for p in {owned_abs}; do [[ "$2" == "$p" ]] && {{ echo grub2-efi-x64-2.12-78; exit 0; }}; done\n'
            'echo "file $2 is not owned by any package"; exit 1\n')
        (bin_dir / "rpm").chmod(0o755)
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", EFI_ROOT=str(efi))
        return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)

    SHIM = {"shim/16.1-5/EFI/fedora/shimx64.efi": b"shim"}

    def test_matching_vendor_owned_grub_with_prefix_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            grub = "grub2/1:2.12-78.bfin.1/EFI/fedora/grubx64.efi"
            r = self.run_guard(tmp, {**self.SHIM, grub: b"\x00-p /EFI/fedora\x00"}, owned=[grub])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("EFI boot chain OK", r.stdout)

    def test_grub_in_another_vendor_dir_fails(self):
        """The 2026-09-21 regression: GRUB packaged for EFI/hummingbird."""
        with tempfile.TemporaryDirectory() as tmp:
            grub = "grub2/1:2.12-78.hum1.bfin/EFI/hummingbird/grubx64.efi"
            r = self.run_guard(tmp, {**self.SHIM, grub: b"-p /EFI/hummingbird"}, owned=[grub])
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("no GRUB is packaged there", r.stderr)
            self.assertIn("EFI/hummingbird/grubx64.efi", r.stderr)

    def test_an_unowned_copy_beside_shim_does_not_count(self):
        """What masked the regression: a stale Fedora GRUB mirrored next to shim."""
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_guard(tmp, {
                **self.SHIM,
                "shim/16.1-5/EFI/fedora/grubx64.efi": b"-p /EFI/fedora",
                "grub2/1:2.12-78.hum1.bfin/EFI/hummingbird/grubx64.efi": b"-p /EFI/hummingbird",
            }, owned=["grub2/1:2.12-78.hum1.bfin/EFI/hummingbird/grubx64.efi"])
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("unowned copy", r.stderr)

    def test_right_directory_wrong_compiled_prefix_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            grub = "grub2/1:2.12-78/EFI/fedora/grubx64.efi"
            r = self.run_guard(tmp, {**self.SHIM, grub: b"-p /EFI/hummingbird"}, owned=[grub])
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("does not carry the prefix /EFI/fedora", r.stderr)

    def test_no_shim_at_all_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_guard(tmp, {"grub2/1/EFI/fedora/grubx64.efi": b"-p /EFI/fedora"})
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("no shimx64.efi", r.stderr)


if __name__ == "__main__":
    unittest.main()
