"""Layer normalization must preserve RPM payload times and symlink targets."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest


spec = importlib.util.spec_from_file_location(
    "pin_build_mtimes", Path(__file__).resolve().parents[1] / "scripts/pin-build-mtimes.py")
pin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pin)


class PinBuildMtimesTests(unittest.TestCase):
    def test_generated_paths_match_across_runs_without_restamping_rpm_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshots = []
            for index in range(2):
                root = Path(temporary) / str(index)
                directory = root / "usr/lib"
                directory.mkdir(parents=True)
                payload = directory / "payload.py"
                payload.write_text("payload")
                os.utime(payload, (2000, 2000))
                generated = directory / "generated"
                generated.write_text("generated")
                os.utime(generated, (3000 + index, 3000 + index))
                pin.normalize(root, {"/usr/lib/payload.py": 2000}, 1000)
                snapshots.append({str(p.relative_to(root)): (p.stat().st_mtime_ns, p.read_bytes())
                                  for p in root.rglob("*") if p.is_file()})
                self.assertEqual(directory.stat().st_mtime_ns, 1000 * 1_000_000_000)
                self.assertEqual(payload.stat().st_mtime_ns, 2000 * 1_000_000_000)
            self.assertEqual(*snapshots)

    def test_symlink_target_outside_the_root_is_not_modified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            directory = root / "usr"
            directory.mkdir(parents=True)
            target = Path(temporary) / "target"
            target.write_text("outside")
            os.utime(target, (2000, 2000))
            link = directory / "link"
            link.symlink_to(target)
            (directory / "broken").symlink_to("missing")
            pin.normalize(root, {}, 1000)
            self.assertEqual(target.stat().st_mtime_ns, 2000 * 1_000_000_000)
            self.assertEqual(link.lstat().st_mtime_ns, 1000 * 1_000_000_000)

    def test_bind_mount_subtree_is_never_normalized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mounted = root / "tmp/inputs"
            mounted.mkdir(parents=True)
            payload = mounted / "source"
            payload.write_text("read-only input")
            os.utime(payload, (3000, 3000))
            pin.normalize(root, {}, 1000, [mounted])
            self.assertEqual(payload.stat().st_mtime_ns, 3000 * 1_000_000_000)

    def test_rewrite_rejects_epoch_older_than_any_rpm_payload(self):
        with self.assertRaisesRegex(RuntimeError, "payload.py"):
            pin.check_rewrite_epoch({"/usr/lib/payload.py": 2000}, 1000)
        pin.check_rewrite_epoch({"/usr/lib/payload.py": 2000}, 2000)
