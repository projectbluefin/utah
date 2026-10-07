"""Reject an installed ISO that silently booted another flavor/kernel."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "review_installed", Path(__file__).resolve().parents[1] / "iso/scripts/verify-installed-flavor.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class FlavorProbeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.metadata = self.root / "usr/share/ublue-os/image-info.json"
        self.metadata.parent.mkdir(parents=True)
        self.receipt = self.root / "usr/lib/utah/ogc-kernel-release"
        self.receipt.parent.mkdir(parents=True)

    def configure(self, flavor, release=None):
        self.metadata.write_text(json.dumps({"image-flavor": flavor}))
        if release:
            self.receipt.write_text(release + "\n")

    def test_wrong_flavor_fails_even_when_the_desktop_booted(self):
        self.configure("main")
        with self.assertRaises(AssertionError):
            probe.verify_flavor("gaming", self.root, "7.1.8-ogc1")

    def test_gaming_fallback_to_base_kernel_fails(self):
        self.configure("gaming", "7.1.8-ogc1")
        with self.assertRaisesRegex(AssertionError, "OGC"):
            probe.verify_flavor("gaming", self.root, "7.2.8-200.fc44.x86_64")

    def test_matching_gaming_kernel_passes(self):
        self.configure("gaming", "7.1.8-ogc1")
        with patch.object(probe, "require", return_value=""):
            result = probe.verify_flavor("gaming", self.root, "7.1.8-ogc1")
        self.assertEqual(result["kernel"], "7.1.8-ogc1")

    def test_nvidia_module_built_for_another_kernel_fails(self):
        self.configure("nvidia")
        with patch.object(probe, "require", return_value="7.1.8-other SMP"):
            with self.assertRaisesRegex(AssertionError, "NVIDIA module"):
                probe.verify_flavor("nvidia", self.root, "7.2.8-200.fc44.x86_64")


if __name__ == "__main__":
    unittest.main()
