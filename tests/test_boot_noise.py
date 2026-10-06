"""Boot and session errors that buried real ones in `ujust report` (#444).

Each fix below was verified on a booted image. The remount-fs drop-in's
composefs behavior was proven on VM utah-e2e-20261005
(testing-20261005-00765ff): systemd-remount-fs reports inactive with
ConditionResult=no. These tests pin the shipped files so a cleanup cannot
silently bring the noise back.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "system_files/shared"


def directives(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


class BootNoiseTests(unittest.TestCase):
    def test_udev_rule_groups_are_created(self):
        groups = {line.split()[1] for line in directives(
            SHARED / "usr/lib/sysusers.d/utah-udev-groups.conf")
            if line.startswith("g ")}
        self.assertEqual(groups, {"plugdev", "nintendo_switch"})

    def test_mail_spool_exists_for_useradd(self):
        rules = directives(SHARED / "usr/lib/tmpfiles.d/utah-mail.conf")
        self.assertIn("d /var/spool/mail 0775 root mail -", rules)
        self.assertIn("L /var/mail - - - - spool/mail", rules)

    def test_root_chmod_is_masked(self):
        # Same file name as systemd's /usr/lib/tmpfiles.d/root.conf, so /etc
        # wins; it must carry no directives of its own.
        mask = SHARED / "etc/tmpfiles.d/root.conf"
        self.assertTrue(mask.is_file())
        self.assertEqual(directives(mask), [])

    def test_pipewire_conf_has_an_assignment(self):
        # PipeWire 1.6 rejects a conf.d file with no assignments (EINVAL).
        conf = SHARED / ("usr/share/pipewire/pipewire-pulse.conf.d/"
                         "50-bluefin-bt-switch.conf")
        self.assertIn("pulse.cmd = [ ]", directives(conf))
        # The override must stay a no-op: nothing may be loaded by it.
        self.assertIsNone(re.search(r"^\s*\{\s*cmd", conf.read_text(), re.M))

    def test_remount_fs_skips_on_read_only_root(self):
        # systemd-remount-fs unconditionally remounts / read-write. On the
        # composefs image / is mounted ro, so the upstream unit fails with
        # "overlay: No changes allowed in reconfigure" and degrades the boot
        # even when nothing else is wrong (#585). The drop-in gates the unit
        # on / being writable so it is skipped on composefs and still runs
        # on the writable live ISO (`iso/scripts/build-iso.sh`, overlay
        # root). This is a static structural check; the runtime behavior was
        # proven on VM utah-e2e-20261005 (ConditionResult=no).
        drop_in = SHARED / (
            "usr/lib/systemd/system/systemd-remount-fs.service.d/"
            "10-utah-composefs-skip.conf"
        )
        self.assertTrue(drop_in.is_file(), f"{drop_in} is missing")
        self.assertIn("ConditionPathIsReadWrite=/", directives(drop_in))

    def test_empty_crypttab_quells_dracut_generator(self):
        # dracut's crypt generator exits 2 on crypttab-less boots: with no
        # /etc/crypttab it takes a bare top-level 'return 0', which bash
        # rejects outside a function (verified: `sh -c 'return 0'` exits 2
        # with the exact journal message). The failure was observed on VM
        # utah-e2e-20261005 (testing-20261005-00765ff, no LUKS). An empty
        # crypttab parses to zero entries and exits 0; the installer
        # overwrites it on LUKS installs. The seed must precede any dracut
        # run, so it is first in the package-transaction RUN (#590).
        containerfile = (ROOT / "Containerfile").read_text()
        seed = containerfile.index(": > /etc/crypttab")
        transaction = containerfile.index(
            "/usr/local/libexec/utah-install-packages \\")
        self.assertLess(
            seed, transaction,
            "/etc/crypttab must be seeded before the package transaction")


if __name__ == "__main__":
    unittest.main()
