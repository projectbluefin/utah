"""bootloader-update.service must not run on the live ISO.

bootupd's own ExecCondition only skips a live /sysroot on erofs/squashfs;
Utah's live root is a dmsquash-live overlay with no /sysroot, so the unit
failed in every live session. Verified on the published live ISO: with the
drop-in, ConditionResult=no and `systemctl --failed` is empty.
"""
import configparser
from pathlib import Path
import unittest

DROPIN = Path(__file__).resolve().parents[1] / (
    "system_files/shared/usr/lib/systemd/system/bootloader-update.service.d/"
    "10-utah-ostree-only.conf")


class BootloaderUpdateLiveTests(unittest.TestCase):
    def test_runs_only_on_ostree_booted_systems(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(DROPIN.read_text())
        self.assertEqual(parser["Unit"]["ConditionPathExists"], "/run/ostree-booted")


if __name__ == "__main__":
    unittest.main()
