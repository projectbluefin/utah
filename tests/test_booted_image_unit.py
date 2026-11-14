"""ublue-booted-image.service must not log SELinux denials on boot.

The unit recorded the booted image ref by running `bootc status --json` from
the `ublue-booted-image-sync` script. bootc started inside that script's
`unconfined_service_t` domain, so its `mac_admin` relabelling probe (a
`chcon` call) was denied, logging two AVC entries every boot. The fix runs
bootc as the unit's own `ExecStart`, where its `install_exec_t` label is the
entrypoint into bootc's SELinux domain, and leaves the script to parse the
JSON the unit writes.

A second bug (projectbluefin/utah#1203) then split the work across two units:
`StandardOutput=truncate:` applies to *every* `Exec*` of a unit, so running the
parse step as `ExecStartPost` in the bootc unit truncated the JSON to empty
before the script read it. The parse now lives in its own unit with no
`StandardOutput` override, and `/run/ublue-os` is created early by a tmpfiles.d
rule instead of an `ExecStartPre`.
"""
import configparser
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "system_files/shared/usr/lib/systemd/system/ublue-booted-image.service"
SYNC_UNIT = ROOT / "system_files/shared/usr/lib/systemd/system/ublue-booted-image-sync.service"
SCRIPT = ROOT / "system_files/shared/usr/bin/ublue-booted-image-sync"
TMPFILES = ROOT / "system_files/shared/usr/lib/tmpfiles.d/utah-booted-image.conf"
STATE_FILE = "/run/ublue-os/booted-image"


def load(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(path.read_text())
    return parser


class BootedImageUnitTests(unittest.TestCase):
    def test_runs_bootc_directly(self):
        # bootc's install_exec_t label is its SELinux entrypoint; running it
        # from the script ran it in the unconfined service domain and its
        # chcon probe was denied mac_admin (projectbluefin/utah#1198).
        service = load(UNIT)["Service"]
        self.assertEqual(service["ExecStart"], "/usr/bin/bootc status --json")
        self.assertEqual(service["Type"], "oneshot")

    def test_writes_bootc_json_then_parses_it_in_the_sync_unit(self):
        # bootc writes its JSON here; the sync unit reads that file next. The
        # parse moved out of this unit (see #1203), so this unit has no
        # ExecStartPost.
        service = load(UNIT)["Service"]
        self.assertEqual(
            service["StandardOutput"],
            "truncate:/run/ublue-os/booted-image.json",
        )
        self.assertNotIn("ExecStartPost", service)
        # The parse step lives in its own unit, keyed to run after bootc.
        sync = load(SYNC_UNIT)["Service"]
        self.assertTrue(
            sync["ExecStart"].endswith("/run/ublue-os/booted-image.json")
        )
        self.assertEqual(
            load(SYNC_UNIT)["Unit"]["After"],
            "ublue-booted-image.service",
        )

    def test_state_dir_created_by_tmpfiles(self):
        # The dir must exist before bootc's StandardOutput truncates its JSON
        # there, so tmpfiles.d creates it early in boot instead of an
        # ExecStartPre that would truncate its own stdout first (#1203).
        self.assertTrue(TMPFILES.is_file(), "utah-booted-image.conf is missing")
        lines = [
            line.split("#", 1)[0].strip()
            for line in TMPFILES.read_text().splitlines()
        ]
        self.assertIn(
            "d /run/ublue-os 0755 root root -",
            lines,
            "utah-booted-image.conf must carry `d /run/ublue-os 0755 root root -`",
        )

    def test_sync_unit_has_no_standard_output(self):
        # The reviewer's guard: StandardOutput=truncate:/file: applies to every
        # Exec* of a unit, so a unit that also runs ExecStartPre/ExecStartPost
        # truncates the very file those steps read. The sync unit must not set
        # StandardOutput at all (projectbluefin/utah#1203).
        service = load(SYNC_UNIT)["Service"]
        self.assertNotIn("StandardOutput", service)

    def test_no_unit_truncates_what_it_also_runs(self):
        # Regression guard for #1203: any unit with StandardOutput=file:/
        # truncate: must not also run an ExecStartPre/ExecStartPost, because the
        # truncation applies to those steps too and empties the file first.
        sys_dir = ROOT / "system_files/shared/usr/lib/systemd/system"
        for path in sys_dir.glob("*.service"):
            with self.subTest(unit=path.name):
                try:
                    service = load(path)["Service"]
                except configparser.NoSectionError:
                    continue
                if "StandardOutput" not in service:
                    continue
                self.assertNotIn(
                    "ExecStartPre", service,
                    f"{path.name} truncates its stdout but also runs "
                    "ExecStartPre",
                )
                self.assertNotIn(
                    "ExecStartPost", service,
                    f"{path.name} truncates its stdout but also runs "
                    "ExecStartPost",
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
