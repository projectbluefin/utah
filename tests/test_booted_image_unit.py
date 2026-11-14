"""ublue-booted-image.service must record the booted image ref without bootc.

The unit used to run `bootc status --json` at boot. A boot service runs as
unconfined_service_t, where bootc's relabelling chcon probe is denied
mac_admin: two AVC denials every boot (#1198). Running bootc as the unit's own
ExecStart did not change the domain (review_vm run 43600763520 still logged
both denials). So ublue-booted-image-sync reads the booted deployment's origin
file instead, and these tests run it against fixture trees for the ostree and
composefs layouts.
"""
import configparser
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "system_files/shared/usr/lib/systemd/system/ublue-booted-image.service"
SCRIPT = ROOT / "system_files/shared/usr/bin/ublue-booted-image-sync"


def load(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(path.read_text())
    return parser


class UnitTests(unittest.TestCase):
    def test_runs_the_sync_script(self):
        service = load(UNIT)["Service"]
        self.assertEqual(service["ExecStart"], "/usr/bin/ublue-booted-image-sync")
        self.assertEqual(service["Type"], "oneshot")

    def test_never_runs_bootc(self):
        unit = UNIT.read_text()
        code = [line for line in SCRIPT.read_text().splitlines()
                if not line.lstrip().startswith("#")]
        run = re.compile(r"\bbootc\s+(status|upgrade|switch|rollback|edit|install|container)\b")
        self.assertFalse(any(run.search(line) for line in code))
        self.assertNotIn("/usr/bin/bootc", unit)


class SyncScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "proc").mkdir()
        (self.root / "run").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_sync(self):
        env = dict(os.environ, UBLUE_BOOTED_IMAGE_ROOT=str(self.root))
        result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.root / "run/ublue-os/booted-image"
        return state.read_text().strip() if state.exists() else None

    def ostree(self, reference, booted="abc123.0", other="def456.0"):
        deploy = self.root / "sysroot/ostree/deploy/default/deploy"
        deploy.mkdir(parents=True)
        for name, ref in ((booted, reference), (other, "ostree-image-signed:docker://ghcr.io/wrong/image:old")):
            (deploy / name).mkdir()
            (deploy / f"{name}.origin").write_text(f"[origin]\ncontainer-image-reference={ref}\n")
        boot = self.root / "sysroot/ostree/boot.1/default/bootcsum"
        boot.mkdir(parents=True)
        (boot / "0").symlink_to(f"../../../deploy/default/deploy/{booted}")
        (self.root / "proc/cmdline").write_text(
            "BOOT_IMAGE=(hd0,gpt2)/vmlinuz root=UUID=x rw ostree=/ostree/boot.1/default/bootcsum/0 quiet\n")

    def test_ostree_signed_registry(self):
        self.ostree("ostree-image-signed:docker://ghcr.io/projectbluefin/utah:testing")
        self.assertEqual(self.run_sync(), "ghcr.io/projectbluefin/utah:testing")

    def test_ostree_unverified_registry(self):
        self.ostree("ostree-unverified-registry:ghcr.io/projectbluefin/utah:review-vm-0db1983e")
        self.assertEqual(self.run_sync(), "ghcr.io/projectbluefin/utah:review-vm-0db1983e")

    def test_ostree_remote_registry(self):
        self.ostree("ostree-remote-registry:fedora:quay.io/fedora/fedora-bootc:44")
        self.assertEqual(self.run_sync(), "quay.io/fedora/fedora-bootc:44")

    def test_ostree_reads_only_the_booted_deployment(self):
        self.ostree("ostree-image-signed:docker://ghcr.io/projectbluefin/utah:testing",
                    booted="def456.0", other="abc123.0")
        self.assertEqual(self.run_sync(), "ghcr.io/projectbluefin/utah:testing")

    def test_composefs_digest(self):
        deploy = self.root / "sysroot/state/deploy/ae77"
        deploy.mkdir(parents=True)
        (deploy / "ae77.origin").write_text(
            "[origin]\ncontainer-image-reference = docker://ghcr.io/projectbluefin/dakota:testing\n\n"
            "[image]\nmanifest_digest = sha256:4a06d9\n")
        (self.root / "proc/cmdline").write_text("root=LABEL=root rw composefs.digest=v1-sha512-12:ae77\n")
        self.assertEqual(self.run_sync(), "ghcr.io/projectbluefin/dakota:testing")

    def test_no_origin_leaves_no_state_file(self):
        (self.root / "proc/cmdline").write_text("root=UUID=x rw quiet\n")
        self.assertIsNone(self.run_sync())


class FileOutputUnitsHaveOneCommandTests(unittest.TestCase):
    """systemd reopens a StandardOutput=/StandardError= file for every command a
    unit runs: ExecStartPost= reads it truncated, ExecStartPre= fails before the
    directory exists. Guard every shipped unit against that."""

    def test_no_file_output_unit_runs_pre_or_post_commands(self):
        offenders = []
        for path in sorted((ROOT / "system_files").rglob("*.service")):
            try:
                service = load(path)["Service"]
            except (KeyError, configparser.Error):
                continue
            redirected = any(
                re.match(r"(file|truncate|append):", service.get(key, ""))
                for key in ("StandardOutput", "StandardError")
            )
            if redirected and any(k in service for k in ("ExecStartPre", "ExecStartPost")):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
