"""ublue-booted-image.service must not log SELinux denials on boot.

The unit recorded the booted image ref by running `bootc status --json` from
the `ublue-booted-image-sync` script. bootc started inside that script's
`unconfined_service_t` domain, so its `mac_admin` relabelling probe (a
`chcon` call) was denied, logging two AVC entries every boot. The fix runs
bootc as the unit's own `ExecStart`, where its `install_exec_t` label is the
entrypoint into bootc's SELinux domain, and leaves the script to parse the
JSON the unit writes.
"""
import configparser
from pathlib import Path
import unittest

UNIT = Path(__file__).resolve().parents[1] / (
    "system_files/shared/usr/lib/systemd/system/ublue-booted-image.service")
SCRIPT = Path(__file__).resolve().parents[1] / (
    "system_files/shared/usr/bin/ublue-booted-image-sync")
STATE_FILE = "/run/ublue-os/booted-image"


def load() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(UNIT.read_text())
    return parser


class BootedImageUnitTests(unittest.TestCase):
    def test_runs_bootc_directly(self):
        # bootc's install_exec_t label is its SELinux entrypoint; running it
        # from the script ran it in the unconfined service domain and its
        # chcon probe was denied mac_admin (projectbluefin/utah#1198).
        service = load()["Service"]
        self.assertEqual(service["ExecStart"], "/usr/bin/bootc status --json")
        self.assertEqual(service["Type"], "oneshot")

    def test_captures_bootc_json_then_parses_it(self):
        service = load()["Service"]
        # bootc writes its JSON here; the sync script reads that file next.
        self.assertEqual(
            service["StandardOutput"],
            "truncate:/run/ublue-os/booted-image.json",
        )
        self.assertTrue(
            service["ExecStartPost"].endswith(
                "/run/ublue-os/booted-image.json"
            )
        )

    def test_execstart_pre_makes_the_state_dir(self):
        # StandardOutput=truncate writes before any script runs, so the dir
        # must exist first or the unit fails to start.
        self.assertEqual(
            load()["Service"]["ExecStartPre"],
            "/usr/bin/install -d -m 0755 /run/ublue-os",
        )

    def test_script_does_not_run_bootc(self):
        # The bug was the script running bootc itself. The bare binary must be
        # the only place bootc status runs.
        script = SCRIPT.read_text()
        self.assertNotIn("bootc status --json", script)

    def test_script_writes_the_state_file_the_reader_reads(self):
        script = SCRIPT.read_text()
        self.assertIn(STATE_FILE, script)
        self.assertIn(".status.booted.image.image.image", script)


if __name__ == "__main__":
    unittest.main()
