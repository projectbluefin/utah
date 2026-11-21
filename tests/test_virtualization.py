"""Dakota's virtualization set (#1232)."""
import os
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAKOTA_DEVMODE = ROOT / "system_files/shared/usr/libexec/dakota-devmode"
PRESET = ROOT / "system_files/shared/usr/lib/systemd/system-preset/98-utah-enable-default.preset"


def manifest(name):
    return tomllib.loads((ROOT / "packages" / name).read_text())


class VirtualizationTests(unittest.TestCase):
    def test_published_pieces_are_installed(self):
        parity = manifest("utah.toml")["parity"]["packages"]
        for name in ("qemu-guest-agent", "spice-vdagent", "open-vm-tools",
                     "hyperv-daemons", "toolbox", "systemd-container", "virtiofsd"):
            self.assertIn(name, parity)

    def test_qemu_and_libvirt_come_from_the_factory(self):
        utah = manifest("utah.toml")
        for name in ("qemu-kvm", "libvirt-daemon-kvm", "libvirt-daemon-config-network",
                     "libvirt-client", "virtiofsd"):
            with self.subTest(name=name):
                self.assertIn(name, utah["parity"]["packages"])
                self.assertIn(name, utah["factory"]["parity"])
                self.assertNotIn(name, utah["unavailable"]["packages"])

    def test_containerd_and_runc_are_gone(self):
        base = manifest("base.toml")
        self.assertNotIn("containerd", base["fedora"]["packages"])
        for name in ("containerd", "runc"):
            self.assertIn(name, base["excluded"]["packages"])

    def test_libvirt_stays_off_by_preset(self):
        lines = PRESET.read_text().splitlines()
        for line in ("disable libvirtd.socket", "disable virt*d.socket",
                     "disable virt*d.service", "disable virt*d-*.socket"):
            self.assertIn(line, lines)
        self.assertLess(lines.index("disable virt*d.socket"), lines.index("enable *"))

    def test_devmode_helper_is_dakotas_executable(self):
        self.assertTrue(os.access(DAKOTA_DEVMODE, os.X_OK))
        text = DAKOTA_DEVMODE.read_text()
        self.assertTrue(text.startswith("#!/usr/bin/python3\n"))
        self.assertIn('"setup": setup_virtualization', text)


if __name__ == "__main__":
    unittest.main()
