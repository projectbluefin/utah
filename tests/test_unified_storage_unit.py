"""bootc-unified-storage.service must not hold up boot.

`bootc image set-unified` pulls the whole booted image from its registry. As
a oneshot wanted by multi-user.target with default dependencies,
multi-user.target and graphical.target waited for that pull (graphical.target
at 1min 32s on a booted testing-20261001-41873b5).
"""
import configparser
from pathlib import Path
import unittest

UNIT = Path(__file__).resolve().parents[1] / (
    "system_files/shared/usr/lib/systemd/system/bootc-unified-storage.service")


def load() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(UNIT.read_text())
    return parser


class UnifiedStorageUnitTests(unittest.TestCase):
    def test_targets_do_not_wait_for_it(self):
        # A target orders itself After= every unit it Wants unless the unit
        # opts out of default dependencies.
        unit = load()["Unit"]
        self.assertEqual(unit["DefaultDependencies"], "no")
        self.assertIn("multi-user.target", load()["Install"]["WantedBy"])

    def test_keeps_the_ordering_default_dependencies_gave_it(self):
        unit = load()["Unit"]
        for target in ("sysinit.target", "basic.target", "network-online.target"):
            self.assertIn(target, unit["After"].split())
        self.assertIn("shutdown.target", unit["Conflicts"].split())
        self.assertIn("shutdown.target", unit["Before"].split())
        self.assertIn("network-online.target", unit["Wants"].split())

    def test_runs_bootc_directly(self):
        # bootc's install_exec_t label is its SELinux entrypoint; a shell
        # wrapper ran it as initrc_t and its chcon calls were denied.
        service = load()["Service"]
        self.assertEqual(service["ExecStart"], "bootc image set-unified")
        self.assertEqual(service["Type"], "oneshot")

    def test_sentinel_follows_success(self):
        service = load()["Service"]
        self.assertEqual(service["ExecStartPost"], "touch /var/lib/.bootc-unified-storage")

    def test_retries_are_not_a_multi_gigabyte_pull_every_minute(self):
        self.assertEqual(load()["Service"]["RestartSec"], "15min")


if __name__ == "__main__":
    unittest.main()
