"""bootc-unified-storage.service must not run during boot.

`bootc image set-unified` pulls the whole booted image from its registry. As
a oneshot wanted by multi-user.target with default dependencies,
multi-user.target and graphical.target waited for that pull (graphical.target
at 1min 32s on a booted testing-20261001-41873b5). With
DefaultDependencies=no the targets stopped waiting, but the pull still ran
from 11s to 77s of first boot and starved the session coming up: geoclue hit
its start timeout and TuneD waited on the sysroot lock (post-testing run
42658780168). A timer now starts it well after boot.
"""
import configparser
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "system_files/shared/usr/lib/systemd"
UNIT = SYSTEMD / "system/bootc-unified-storage.service"
TIMER = SYSTEMD / "system/bootc-unified-storage.timer"
PRESET = SYSTEMD / "system-preset/85-utah-desktop.preset"


def load(path: Path = UNIT) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(path.read_text())
    return parser


def minutes(span: str) -> int:
    span = span.strip()
    for suffix, factor in (("min", 1), ("h", 60)):
        if span.endswith(suffix):
            return int(span[: -len(suffix)]) * factor
    raise AssertionError(f"unexpected time span {span!r}")


class UnifiedStorageUnitTests(unittest.TestCase):
    def test_nothing_at_boot_starts_the_service(self):
        # No [Install]: enabling it would put the multi-GB pull back into
        # first boot. The timer is the only thing that starts it.
        self.assertFalse(load().has_section("Install"))
        preset = PRESET.read_text().splitlines()
        self.assertNotIn("enable bootc-unified-storage.service", preset)
        self.assertIn("enable bootc-unified-storage.timer", preset)

    def test_timer_starts_it_well_after_boot(self):
        timer = load(TIMER)
        self.assertEqual(timer["Timer"]["Unit"], "bootc-unified-storage.service")
        self.assertGreaterEqual(minutes(timer["Timer"]["OnBootSec"]), 10)
        self.assertNotIn("OnStartupSec", timer["Timer"])
        self.assertIn("timers.target", timer["Install"]["WantedBy"])
        # Once onboarded, the timer has nothing to start.
        self.assertIn("ConditionPathExists=!/var/lib/.bootc-unified-storage",
                      TIMER.read_text().splitlines())

    def test_targets_do_not_wait_for_it(self):
        # A target orders itself After= every unit it Wants unless the unit
        # opts out of default dependencies.
        self.assertEqual(load()["Unit"]["DefaultDependencies"], "no")

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
