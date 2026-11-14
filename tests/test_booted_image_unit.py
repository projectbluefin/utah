"""The booted-image units must record the image ref without SELinux denials.

ublue-booted-image.service used to run `bootc status --json` from the
ublue-booted-image-sync script. bootc started inside that script's
unconfined_service_t domain, so its mac_admin relabelling probe (a chcon
call) was denied and logged two AVC entries every boot (#1198). bootc now runs
as the unit's own ExecStart, where its install_exec_t label is the entrypoint
into bootc's SELinux domain, and writes its JSON through StandardOutput=.

systemd opens a unit's StandardOutput= target for every command the unit
runs. An ExecStartPost= that reads the file would find it truncated, and an
ExecStartPre= that creates its directory would fail before it ran. So the
parse step lives in its own unit, and tmpfiles.d creates the directory.
"""
import configparser
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / "system_files/shared/usr/lib/systemd/system"
SNAPSHOT = UNITS / "ublue-booted-image.service"
SYNC = UNITS / "ublue-booted-image-sync.service"
SCRIPT = ROOT / "system_files/shared/usr/bin/ublue-booted-image-sync"
TMPFILES = ROOT / "system_files/shared/usr/lib/tmpfiles.d/ublue-booted-image.conf"
JSON = "/run/ublue-os/booted-image.json"
STATE_FILE = "/run/ublue-os/booted-image"


def load(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(path.read_text())
    return parser


class SnapshotUnitTests(unittest.TestCase):
    def test_runs_bootc_as_execstart(self):
        service = load(SNAPSHOT)["Service"]
        self.assertEqual(service["ExecStart"], "/usr/bin/bootc status --json")
        self.assertEqual(service["Type"], "oneshot")

    def test_writes_json_through_standard_output(self):
        self.assertEqual(load(SNAPSHOT)["Service"]["StandardOutput"], f"truncate:{JSON}")

    def test_has_no_other_commands(self):
        service = load(SNAPSHOT)["Service"]
        for key in ("ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost"):
            with self.subTest(key=key):
                self.assertNotIn(key, service)

    def test_starts_after_tmpfiles_creates_the_directory(self):
        self.assertIn("systemd-tmpfiles-setup.service", load(SNAPSHOT)["Unit"]["After"])
        rules = [line.split() for line in TMPFILES.read_text().splitlines()
                 if line.strip() and not line.startswith("#")]
        self.assertIn(["d", "/run/ublue-os", "0755", "root", "root", "-"], rules)


class SyncUnitTests(unittest.TestCase):
    def test_parses_after_the_snapshot(self):
        unit = load(SYNC)["Unit"]
        self.assertEqual(unit["Requires"], "ublue-booted-image.service")
        self.assertEqual(unit["After"], "ublue-booted-image.service")
        self.assertEqual(unit["Before"], "systemd-user-sessions.service")

    def test_reads_the_snapshot_without_redirecting_output(self):
        service = load(SYNC)["Service"]
        self.assertEqual(service["ExecStart"], f"/usr/bin/ublue-booted-image-sync {JSON}")
        self.assertNotIn("StandardOutput", service)

    def test_script_never_runs_bootc(self):
        code = [line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")]
        run = re.compile(r"\bbootc\s+(status|upgrade|switch|rollback|edit|install|container)\b")
        self.assertFalse(any(run.search(line) for line in code))

    def test_script_writes_the_state_file_the_reader_reads(self):
        script = SCRIPT.read_text()
        self.assertIn(STATE_FILE, script)
        self.assertIn(".status.booted.image.image.image", script)


class FileOutputUnitsHaveOneCommandTests(unittest.TestCase):
    """Guard every shipped unit against the truncation trap above."""

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
        self.assertEqual(offenders, [], "StandardOutput=/StandardError= to a file is reopened for "
                         "every command, so ExecStartPre=/ExecStartPost= truncate it or fail before it exists")


if __name__ == "__main__":
    unittest.main()
