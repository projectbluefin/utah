"""Effective-ref checks must follow Flatpak's administrator override behavior."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "iso/live/src/preinstall-refs.py"
spec = importlib.util.spec_from_file_location("preinstall_refs", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PreinstallRefsTests(unittest.TestCase):
    def test_admin_groups_override_only_declared_keys_across_filenames(self):
        with tempfile.TemporaryDirectory() as tmp:
            vendor = Path(tmp) / "vendor"
            admin = Path(tmp) / "admin"
            vendor.mkdir()
            admin.mkdir()
            (vendor / "defaults.preinstall").write_text(
                "[Flatpak Preinstall org.example.App]\nBranch=stable\n"
                "[Flatpak Preinstall org.example.Theme]\nBranch=3.22\nIsRuntime=true\n"
                "[Flatpak Preinstall org.example.Disabled]\nBranch=stable\n")
            (admin / "different.preinstall").write_text(
                "[Flatpak Preinstall org.example.App]\nBranch=testing\n"
                "[Flatpak Preinstall org.example.Disabled]\nInstall=false\n")
            self.assertEqual(module.refs([vendor, admin], "x86_64"), [
                "app/org.example.App/x86_64/testing",
                "runtime/org.example.Theme/x86_64/3.22"])

    def test_same_filename_does_not_mask_unmentioned_vendor_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            vendor = Path(tmp) / "vendor"
            admin = Path(tmp) / "admin"
            vendor.mkdir()
            admin.mkdir()
            (vendor / "a.preinstall").write_text(
                "[Flatpak Preinstall org.example.App]\nBranch=stable\n"
                "[Flatpak Preinstall org.example.Other]\n")
            (admin / "a.preinstall").write_text(
                "[Flatpak Preinstall org.example.App]\nInstall=0\n")
            self.assertEqual(module.refs([vendor, admin], "aarch64"), [
                "app/org.example.Other/aarch64/master"])
