"""avahi-daemon must be able to transition into avahi_t (#443).

With Hummingbird's NoNewPrivileges=yes the targeted policy denies the
init_t -> avahi_t nnp_transition, the daemon exits 255 and mDNS never works.
Verified on a booted testing-20261001-41873b5: with the drop-in the daemon
runs as avahi_t with no AVC denials and *.local names resolve.
"""
import configparser
from pathlib import Path
import unittest

DROPIN = Path(__file__).resolve().parents[1] / (
    "system_files/shared/usr/lib/systemd/system/avahi-daemon.service.d/"
    "10-utah-selinux.conf")


class AvahiSelinuxTests(unittest.TestCase):
    def test_drop_in_disables_no_new_privileges(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(DROPIN.read_text())
        self.assertEqual(parser["Service"]["NoNewPrivileges"], "no")

    def test_drop_in_names_its_removal_condition(self):
        self.assertIn("nnp_transition", DROPIN.read_text())


if __name__ == "__main__":
    unittest.main()
