"""Layer normalization must preserve RPM payload times and symlink targets."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


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


MOUNTINFO = (
    "22 1 0:21 / / rw,relatime - overlay overlay rw\n"
    "36 22 98:0 / /tmp/utah-build-tools ro,relatime - ext4 /dev/root ro\n"
    "37 22 98:0 / /run/my\\040inputs rw,relatime - tmpfs tmpfs rw\n"
)


class MainTests(unittest.TestCase):
    """main() is what every image layer runs: rpm query -> mounts -> normalize."""

    def run_main(self, rpm_stdout, env=None):
        result = subprocess.CompletedProcess([], 0, stdout=rpm_stdout, stderr="")
        with mock.patch.dict(os.environ, env or {}, clear=False), \
                mock.patch.object(pin.subprocess, "run", return_value=result) as run, \
                mock.patch.object(pin.Path, "read_text", return_value=MOUNTINFO), \
                mock.patch.object(pin, "normalize") as normalize:
            for name in ("SOURCE_DATE_EPOCH", "UTAH_REWRITE_TIMESTAMPS"):
                if name not in (env or {}):
                    os.environ.pop(name, None)
            pin.main()
        return run, normalize

    def test_rpm_payload_times_and_bind_mounts_reach_normalize(self):
        run, normalize = self.run_main(
            "/usr/bin/bash\t1700000000\n/usr/share/a\tb\t1600000000\n")
        args, kwargs = run.call_args
        self.assertEqual(args[0][:2], ["rpm", "-qa"])
        self.assertTrue(kwargs["check"], "a failed rpm query must abort the layer")
        root, packaged, epoch, mounts = normalize.call_args.args
        self.assertEqual(root, Path("/"))
        self.assertEqual(packaged, {"/usr/bin/bash": 1700000000, "/usr/share/a\tb": 1600000000})
        self.assertEqual(epoch, 1704067200)
        self.assertEqual(mounts, {Path("/"), Path("/tmp/utah-build-tools"), Path("/run/my inputs")})

    def test_source_date_epoch_overrides_the_default(self):
        _, normalize = self.run_main("/usr/bin/bash\t1700000000\n",
                                     {"SOURCE_DATE_EPOCH": "1710000000"})
        self.assertEqual(normalize.call_args.args[2], 1710000000)

    def test_empty_rpm_database_refuses_before_touching_the_tree(self):
        with self.assertRaisesRegex(RuntimeError, "no payload mtimes"):
            self.run_main("")

    def test_rewrite_mode_refuses_an_epoch_older_than_a_payload(self):
        with self.assertRaisesRegex(RuntimeError, "/usr/bin/new"):
            self.run_main("/usr/bin/new\t1800000000\n", {
                "SOURCE_DATE_EPOCH": "1704067200", "UTAH_REWRITE_TIMESTAMPS": "1"})

    def test_epoch_check_only_runs_in_rewrite_mode(self):
        _, normalize = self.run_main("/usr/bin/new\t1800000000\n",
                                     {"SOURCE_DATE_EPOCH": "1704067200"})
        normalize.assert_called_once()
