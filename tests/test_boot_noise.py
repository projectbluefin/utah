"""Boot and session errors that buried real ones in `ujust report` (#444).

Each fix below was verified on a booted image. The remount-fs drop-in's
composefs behavior was proven on VM utah-e2e-20261005
(testing-20261005-00765ff): systemd-remount-fs reports inactive with
ConditionResult=no. These tests pin the shipped files so a cleanup cannot
silently bring the noise back.
"""
import configparser
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "system_files/shared"
NVIDIA = ROOT / "system_files/nvidia"
CDI_DROP_IN = ("usr/lib/systemd/system/nvidia-cdi-refresh.service.d/"
               "10-utah-require-gpu.conf")


def directives(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


class BootNoiseTests(unittest.TestCase):
    def test_udev_rule_groups_are_created(self):
        # Dakota's bluefin/udev-groups.bst, packaged as dakota-config-udev-groups
        # (#1265); the contract asserts both groups in the installed file.
        import tomllib
        manifest = tomllib.loads((ROOT / "packages/utah.toml").read_text())
        self.assertIn("dakota-config-udev-groups", manifest["parity"]["packages"])
        contract = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        self.assertEqual(
            contract["configuration"]["file_contains"]["/usr/lib/sysusers.d/bluefin-udev-groups.conf"],
            ["g plugdev", "g nintendo_switch"],
        )
        self.assertFalse((SHARED / "usr/lib/sysusers.d/utah-udev-groups.conf").exists())

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

    def test_pipewire_conf_assignment_comes_from_common(self):
        # PipeWire 1.6 rejects a conf.d file with no assignments (EINVAL).
        # Common's copy carries `pulse.cmd = [ ]` itself, so Utah ships no
        # override and the desktop contract pins Common's line instead (#1267).
        rel = ("usr/share/pipewire/pipewire-pulse.conf.d/"
               "50-bluefin-bt-switch.conf")
        self.assertFalse((SHARED / rel).exists())
        contract = tomllib.loads(
            (ROOT / "contracts/bluefin-desktop.toml").read_text())
        configuration = contract["configuration"]
        self.assertIn("/" + rel, configuration["files"])
        self.assertEqual(configuration["file_contains"]["/" + rel],
                         ["pulse.cmd = [ ]"])

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


class NvidiaCdiRefreshTests(unittest.TestCase):
    """nvidia-cdi-refresh.{service,path} must not fail without an NVIDIA GPU.

    Post-Testing run 45918946265 (2de36903): utah-nvidia and
    utah-nvidia-gaming listed both units as failed on first boot, after
    `bootc switch` and after rollback. The live serial log shows why:
    `nvidia-smi -L` cannot reach a driver with no device, the unit exits 127,
    restarts five times, and ends `start-limit-hit`, taking the path unit
    down with `unit-start-limit-hit`.
    """

    def exec_condition(self) -> str:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string((NVIDIA / CDI_DROP_IN).read_text())
        value = parser["Service"]["ExecCondition"]
        prefix = "/bin/sh -c '"
        self.assertTrue(value.startswith(prefix) and value.endswith("'"), value)
        # systemd turns `$$` into a literal `$` before it executes the line.
        return value[len(prefix):-1].replace("$$", "$")

    def condition_exit(self, devices: list[tuple[str, str]]) -> int:
        with tempfile.TemporaryDirectory() as temporary:
            for index, (vendor, pci_class) in enumerate(devices):
                device = Path(temporary) / f"0000:00:{index:02x}.0"
                device.mkdir()
                (device / "vendor").write_text(vendor + "\n")
                (device / "class").write_text(pci_class + "\n")
            script = self.exec_condition().replace("/sys/bus/pci/devices", temporary)
            return subprocess.run(["/bin/sh", "-c", script],
                                  capture_output=True).returncode

    def test_drop_in_ships_for_the_nvidia_flavors_only(self):
        self.assertTrue((NVIDIA / CDI_DROP_IN).is_file())
        self.assertFalse((SHARED / CDI_DROP_IN).exists())
        containerfile = (ROOT / "Containerfile").read_text()
        self.assertIn("COPY system_files/nvidia /tmp/utah-nvidia\n", containerfile)
        self.assertEqual(containerfile.count("cp -a /tmp/utah-nvidia/. /"), 1)
        # Copied in the NVIDIA arm of the flavor step, after the installer
        # that brings in nvidia-container-toolkit-base.
        self.assertRegex(
            containerfile,
            r"nvidia\|nvidia-gaming\) /usr/local/libexec/utah-install-nvidia "
            r"[^;]*cp -a /tmp/utah-nvidia/\. / ;;")

    def test_skipped_without_a_device_nvidia_ko_binds(self):
        # The E2E VM: QEMU's standard VGA and virtio devices.
        self.assertEqual(self.condition_exit(
            [("0x1234", "0x030000"), ("0x1af4", "0x010000")]), 1)
        # An NVIDIA function that is not a GPU (HDMI audio) is not enough.
        self.assertEqual(self.condition_exit([("0x10de", "0x040300")]), 1)

    def test_runs_with_every_device_class_nvidia_ko_binds(self):
        # nvidia.ko's MODULE_DEVICE_TABLE (kernel-open/nvidia/nv-pci-table.c).
        for pci_class in ("0x030000", "0x030200", "0x068000"):
            with self.subTest(pci_class=pci_class):
                self.assertEqual(self.condition_exit(
                    [("0x1234", "0x030000"), ("0x10de", pci_class)]), 0)

    def test_condition_cannot_fail_the_unit(self):
        # ExecCondition= exiting 1-254 skips the unit without failing it, so
        # Restart=on-failure never fires. A Condition*= on /dev/nvidiactl
        # would be wrong: the unit itself creates the control nodes.
        text = "\n".join(directives(NVIDIA / CDI_DROP_IN))
        self.assertNotIn("Condition", text.replace("ExecCondition=", ""))
        self.assertNotRegex(text, r"(?m)^ExecCondition=[-+!@:]")


if __name__ == "__main__":
    unittest.main()
