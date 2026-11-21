"""Dakota's zswap and /var/swap/swapfile swap, adapted to Utah (#1224).

Dakota fronts a disk-backed swapfile with zswap instead of zram
(elements/bluefin/zswap-config.bst, swapfile.bst). Utah copies the files and
adds the two pieces Dakota does not need: the zstd module in the initramfs
(Hummingbird builds it as a module) and the swapfile_t SELinux label.
"""
import os
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "system_files/shared"
UNITS = SHARED / "usr/lib/systemd/system"


class ZswapSwapfileTests(unittest.TestCase):
    def test_kargs_match_dakota(self):
        kargs = tomllib.loads((SHARED / "usr/lib/bootc/kargs.d/20-zswap.toml").read_text())
        self.assertEqual(kargs["kargs"], [
            "zswap.enabled=1",
            "zswap.compressor=zstd",
            "zswap.max_pool_percent=20",
            "zswap.shrinker_enabled=1",
        ])

    def test_sysctls_match_dakota(self):
        lines = [line for line in (SHARED / "usr/lib/sysctl.d/60-zswap.conf").read_text().splitlines()
                 if line and not line.startswith("#")]
        self.assertEqual(lines, [
            "vm.swappiness = 100",
            "vm.watermark_scale_factor = 200",
            "vm.watermark_boost_factor = 0",
        ])

    def test_swap_unit_is_wanted_statically(self):
        # Presets only apply on first boot; Dakota's static wants link reaches
        # machines installed before the swapfile shipped.
        link = UNITS / "swap.target.wants/var-swap-swapfile.swap"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "../var-swap-swapfile.swap")
        swap = (UNITS / "var-swap-swapfile.swap").read_text()
        self.assertIn("What=/var/swap/swapfile", swap)
        self.assertIn("Requires=bluefin-swapfile-init.service", swap)
        self.assertIn("ConditionPathExists=/var/swap/swapfile", swap)
        init = (UNITS / "bluefin-swapfile-init.service").read_text()
        self.assertIn("ExecStart=/usr/libexec/bluefin-swapfile-init", init)
        self.assertIn("DefaultDependencies=no", init)

    def test_init_script_is_executable_and_labels_the_file(self):
        script = SHARED / "usr/libexec/bluefin-swapfile-init"
        self.assertTrue(os.access(script, os.X_OK))
        text = script.read_text()
        # Label before the data lands; the inode keeps it through dd and mkswap.
        self.assertLess(text.index('restorecon "${SWAPFILE}"'), text.index("dd if=/dev/zero"))

    def test_selinux_module_labels_the_swapfile(self):
        cil = (SHARED / "usr/share/selinux/packages/utah-swapfile.cil").read_text()
        self.assertIn('(filecon "/var/swap/swapfile" file (system_u object_r swapfile_t', cil)
        services = (ROOT / "scripts/configure-services.sh").read_text()
        self.assertIn("semodule -n -i /usr/share/selinux/packages/utah-swapfile.cil", services)
        self.assertIn("matchpathcon /var/swap/swapfile", services)

    def test_zram_is_gone(self):
        for manifest in ("base.toml", "utah.toml"):
            data = tomllib.loads((ROOT / "packages" / manifest).read_text())
            names = {pkg for section in data.values() if isinstance(section, dict)
                     for pkg in section.get("packages", [])}
            self.assertNotIn("zram-generator", names, manifest)
            self.assertNotIn("zram-generator-defaults", names, manifest)

    def test_contract_asserts_the_swap_files(self):
        contract = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        files = contract["configuration"]["files"]
        for path in ("/usr/lib/bootc/kargs.d/20-zswap.toml",
                     "/usr/lib/sysctl.d/60-zswap.conf",
                     "/usr/lib/dracut/dracut.conf.d/50-utah-zswap-zstd.conf",
                     "/usr/lib/systemd/system/swap.target.wants/var-swap-swapfile.swap",
                     "/usr/share/selinux/packages/utah-swapfile.cil"):
            self.assertIn(path, files)


if __name__ == "__main__":
    unittest.main()
