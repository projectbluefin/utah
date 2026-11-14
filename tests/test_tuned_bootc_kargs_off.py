"""TuneD must not call bootc on every power-profile switch.

TuneD 2.28 injects a bootloader unit on bootc systems and runs
`bootc loader-entries set-options-for-source` on every profile apply. That
waits, with no timeout, for the ostree sysroot lock, which
bootc-unified-storage.service holds for its whole first-boot pull. TuneD hung
behind it and was killed with its bootc child (core dumps in smoke-b).
"""
import configparser
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "system_files/shared/usr/lib/tuned/profiles/utah-bootc-kargs-off/tuned.conf"


class TunedBootcKargsOffTests(unittest.TestCase):
    def test_profile_disables_the_bootloader_unit(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(PROFILE.read_text())
        # A [bootloader] unit in the merged profile stops TuneD injecting its
        # own; enabled=false keeps the plugin from loading at all.
        self.assertEqual(parser.sections(), ["bootloader"])
        self.assertEqual(parser["bootloader"]["enabled"], "false")

    def test_profile_is_post_loaded(self):
        script = (ROOT / "scripts/configure-services.sh").read_text()
        self.assertIn(
            "printf '%s\\n' utah-bootc-kargs-off > /etc/tuned/post_loaded_profile",
            script,
        )


if __name__ == "__main__":
    unittest.main()
