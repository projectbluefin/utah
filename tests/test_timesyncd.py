"""Time sync is systemd-timesyncd, as on GNOME OS and Dakota (#1228).

Hummingbird ships chrony with a vendor pool (2.hummingbird.pool.ntp.org) that
is NXDOMAIN on the public internet, so a host whose RTC is behind failed every
TLS handshake with "certificate is not yet valid" (#594). Utah drops chrony,
enables timesyncd and gives it GNOME OS's fallback server, which
gnome-build-meta compiles in (core-deps/systemd-base.bst
fallback-ntp-servers). Dakota's ublue-firstboot-date.path waits for
timesyncd's first sync before recording the install date.
"""
from __future__ import annotations

import configparser
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "system_files/shared/usr/lib/systemd"
DROPIN = SYSTEMD / "timesyncd.conf.d/10-gnome-os-fallback-ntp.conf"
PRESET = SYSTEMD / "system-preset/85-utah-desktop.preset"


def ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(path.read_text())
    return parser


class TimesyncdTests(unittest.TestCase):
    def test_chrony_is_removed_from_the_base(self):
        base = tomllib.loads((ROOT / "packages/base.toml").read_text())
        self.assertIn("chrony", base["excluded"]["packages"])
        containerfile = (ROOT / "Containerfile").read_text()
        self.assertNotIn("chrony.conf", containerfile)

    def test_fallback_server_matches_gnome_os(self):
        self.assertEqual(ini(DROPIN)["Time"]["FallbackNTP"], "gnome.pool.ntp.org")

    def test_timesyncd_outranks_hummingbirds_disable(self):
        # Hummingbird's 90-default.preset says "disable systemd-timesyncd.service";
        # only a line in a file that sorts before it turns the unit on.
        self.assertIn("enable systemd-timesyncd.service", PRESET.read_text().splitlines())
        services = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())["services"]
        self.assertIn("systemd-timesyncd.service", services["enabled"])
        self.assertIn("chronyd.service", services["disabled"])

    def test_install_date_waits_for_the_first_sync(self):
        # Dakota files/firstboot/ublue-firstboot-date.path, verbatim.
        path_unit = ini(SYSTEMD / "system/ublue-firstboot-date.path")
        self.assertEqual(path_unit["Path"]["PathExists"], "/run/systemd/timesync/synchronized")
        self.assertEqual(path_unit["Install"]["WantedBy"], "multi-user.target")
        service = ini(SYSTEMD / "system/ublue-firstboot-date.service")
        self.assertFalse(service.has_section("Install"))
        self.assertEqual(service["Service"]["ExecStart"], "/usr/bin/ublue-firstboot-date")
        script = ROOT / "system_files/shared/usr/bin/ublue-firstboot-date"
        self.assertTrue(script.stat().st_mode & 0o111)
        self.assertIn("record=/var/lib/ublue-os/install-date", script.read_text())


if __name__ == "__main__":
    unittest.main()
