"""Reject altered snapshots and incomplete or downgraded replay inventories."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("package_snapshot", ROOT / "ci/package-snapshot.py")
SNAPSHOT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SNAPSHOT)


class SnapshotIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        (self.directory / "example.rpm").write_bytes(b"downloaded RPM bytes")
        (self.directory / "repodata").mkdir()
        (self.directory / "repodata/repomd.xml").write_text("metadata")
        self.files = SNAPSHOT.seal(self.directory, {"example.rpm": "hummingbird"})

    def test_sealed_snapshot_passes(self):
        SNAPSHOT.verify_seal(self.directory, self.files)

    def test_changed_rpm_is_rejected(self):
        (self.directory / "example.rpm").write_bytes(b"different RPM bytes!")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            SNAPSHOT.verify_seal(self.directory, self.files)

    def test_changed_metadata_is_rejected(self):
        (self.directory / "repodata/repomd.xml").write_text("different")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            SNAPSHOT.verify_seal(self.directory, self.files)

    def test_added_or_removed_file_is_rejected(self):
        (self.directory / "extra.rpm").write_bytes(b"extra")
        with self.assertRaisesRegex(ValueError, "file set changed"):
            SNAPSHOT.verify_seal(self.directory, self.files)
        (self.directory / "extra.rpm").unlink()
        (self.directory / "example.rpm").unlink()
        with self.assertRaisesRegex(ValueError, "file set changed"):
            SNAPSHOT.verify_seal(self.directory, self.files)

    def test_symlink_replacement_is_rejected(self):
        rpm = self.directory / "example.rpm"
        rpm.unlink()
        rpm.symlink_to("repodata/repomd.xml")
        with self.assertRaisesRegex(ValueError, "symlink"):
            SNAPSHOT.verify_seal(self.directory, self.files)


class ReplayContractTests(unittest.TestCase):
    def test_correct_inventory_passes(self):
        rows = SNAPSHOT.inventory("gnome-shell|0|51.0|1.bfin|x86_64\nbuild-tool|0|1|1.hum1|x86_64")
        SNAPSHOT.verify_inventory(rows, ["gnome-shell", "build-tool"], {"gnome-shell": "51"})

    def test_missing_build_dependency_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Missing contract packages"):
            SNAPSHOT.verify_inventory(["gnome-shell|0|51.0|1.bfin|x86_64"],
                                      ["gnome-shell", "build-tool"], {"gnome-shell": "51"})

    def test_wrong_desktop_major_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "expected major 51"):
            SNAPSHOT.verify_inventory(["gnome-shell|0|50.0|1.hum1|x86_64"],
                                      ["gnome-shell"], {"gnome-shell": "51"})

    def test_empty_or_malformed_inventory_is_rejected(self):
        for raw in ["", "warning instead of package data"]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                SNAPSHOT.inventory(raw)


if __name__ == "__main__":
    unittest.main()
