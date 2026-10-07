"""ISO assembly must pair the selected initramfs with that exact kernel."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("live_kernel", ROOT / "iso/scripts/live-kernel.py")
kernel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kernel)


class LiveKernelTests(unittest.TestCase):
    def test_supported_layouts_and_rooted_symlinks(self):
        for layout in ["modules", "boot", "relative-link", "absolute-link"]:
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                modules = root / "usr/lib/modules/7.1.8-ogc1"
                modules.mkdir(parents=True)
                boot = root / "boot/vmlinuz-7.1.8-ogc1"
                boot.parent.mkdir()
                if layout == "modules":
                    expected = modules / "vmlinuz"
                    expected.write_bytes(b"selected kernel")
                else:
                    expected = boot
                    boot.write_bytes(b"selected kernel")
                    if layout.endswith("link"):
                        target = ("../../../../boot/vmlinuz-7.1.8-ogc1" if layout == "relative-link"
                                  else "/boot/vmlinuz-7.1.8-ogc1")
                        (modules / "vmlinuz").symlink_to(target)
                self.assertEqual(kernel.kernel_file(root, "7.1.8-ogc1"), expected)

    def test_missing_release_does_not_select_other_kernel(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp) / "boot"
            boot.mkdir()
            (boot / "vmlinuz-other").write_bytes(b"wrong kernel")
            (boot / "vmlinuz").symlink_to("vmlinuz-other")
            with self.assertRaises(FileNotFoundError):
                kernel.kernel_file(tmp, "7.1.8-ogc1")

    def test_links_cannot_read_host_files_or_loop_forever(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "image"
            root.mkdir()
            outside = Path(tmp) / "host-kernel"
            outside.write_bytes(b"must not enter ISO")
            link = root / "link"
            for target in [str(outside), "../host-kernel"]:
                with self.subTest(target=target):
                    link.symlink_to(target)
                    with self.assertRaises(FileNotFoundError):
                        kernel.image_file(root, "/link")
                    link.unlink()
            link.symlink_to("link")
            with self.assertRaises(ValueError):
                kernel.image_file(root, "/link")

    def test_release_cannot_be_a_path(self):
        for release in ["", ".", "..", "../boot", "/boot"]:
            with self.subTest(release=release), self.assertRaises(ValueError):
                kernel.kernel_file("/", release)


class BootcKernelSelectionTests(unittest.TestCase):
    def fixture(self, root):
        base = root / "usr/lib/modules/7.2.8-200.fc44.x86_64"
        ogc = root / "usr/lib/modules/7.1.8-ogc1"
        base.mkdir(parents=True)
        ogc.mkdir()
        (base / "vmlinuz").write_bytes(b"base kernel")
        (base / "initramfs.img").write_bytes(b"stale base initramfs")
        (base / "module.ko").write_bytes(b"retain RPM module tree")
        boot = root / "boot"
        boot.mkdir()
        (boot / "vmlinuz-7.1.8-ogc1").write_bytes(b"gaming kernel")
        receipt = root / "usr/lib/utah/ogc-kernel-release"
        receipt.parent.mkdir(parents=True)
        receipt.write_text("7.1.8-ogc1\n")
        return base, ogc, boot

    def test_ogc_must_replace_newer_base_in_bootc_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, ogc, boot = self.fixture(root)
            with self.assertRaisesRegex(ValueError, "base bootc kernel"):
                kernel.bootc_release(root)
            self.assertEqual(kernel.bootc_release(root, promote_ogc=True), "7.1.8-ogc1")
            self.assertEqual((ogc / "vmlinuz").read_bytes(), b"gaming kernel")
            self.assertFalse((base / "vmlinuz").exists())
            self.assertFalse((base / "initramfs.img").exists())
            self.assertEqual((base / "module.ko").read_bytes(), b"retain RPM module tree")
            self.assertEqual(kernel.bootc_release(root), "7.1.8-ogc1")

    def test_missing_ogc_source_preserves_base_boot_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, ogc, boot = self.fixture(root)
            (boot / "vmlinuz-7.1.8-ogc1").unlink()
            with self.assertRaises(FileNotFoundError):
                kernel.bootc_release(root, promote_ogc=True)
            self.assertEqual((base / "vmlinuz").read_bytes(), b"base kernel")
            self.assertTrue((base / "initramfs.img").is_file())

    def test_two_bootc_kernels_without_flavor_receipt_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, ogc, boot = self.fixture(root)
            (root / "usr/lib/utah/ogc-kernel-release").unlink()
            (ogc / "vmlinuz").write_bytes(b"ambiguous kernel")
            with self.assertRaisesRegex(ValueError, "Expected one"):
                kernel.bootc_release(root)

    def test_empty_ogc_source_preserves_base_boot_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, ogc, boot = self.fixture(root)
            (boot / "vmlinuz-7.1.8-ogc1").write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "Empty OGC"):
                kernel.bootc_release(root, promote_ogc=True)
            self.assertTrue((base / "vmlinuz").is_file())
